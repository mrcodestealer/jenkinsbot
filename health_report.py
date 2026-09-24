"""Daily health report: once a day the bot posts a status card to a Lark group.

Wire it up once at boot, in the production start path:

    import health_report
    health_report.start(
        "AlertBot",
        send_card=lambda chat_id, card: lark.send_card(chat_id, card) or False,  # None on failure -> False
        checks=[("Lark API", _check_lark), ("Ollama", _check_ollama)],
        expect_threads=["watcher", "refresher"],
    )

and, optionally, count activity from the hot paths (both are cheap and thread-safe):

    health_report.bump("Lark events")        # counter, shown per report period
    health_report.mark("Last Lark event")    # "last seen" time

A check is a zero-argument callable. It may return
    True / False                    -> ok / fail
    "detail"                        -> ok, with that detail
    (status, "detail")              -> status is True/False/None or "ok"/"warn"/"fail"/"skip"
    {"status": ..., "detail": ...}
or raise (-> fail, with the exception text). None means "skip". Every check runs on
its own daemon thread and is abandoned after HEALTH_REPORT_CHECK_TIMEOUT_SECONDS, so a
hung dependency shows up as a failed row instead of stalling the report.

``send_card(chat_id, card)`` gets the card as a dict. It must raise, return False, return
``(False, ...)``, or return a Lark-style ``{"code": non-zero}`` on failure; anything else
counts as sent, so wrap a sender that returns None on failure (``... or False``). When the
bot has no sender that surfaces errors, use ``make_lark_sender(app_id, app_secret, base_url)``.

One process per bot name and host sends: the state file and its lock live in
/var/lib/health_report/<bot>.json (next to this module when that is not writable), so a
restart or a second checkout never posts the same report twice, and a waiting process
takes over when the holder exits. A missed report is sent when the bot is back the same
day; failed sends are retried until the next slot or midnight, and a send that is slow
to answer is waited for rather than repeated.

Under systemd the "Logs" block comes from the service's own journal for the last 24h
(error lines, tracebacks, crash restarts), so it also covers bots that print() instead
of logging and survives restarts. Elsewhere it falls back to counting this process's
WARNING+ log records.

Env (all optional, read when the report runs):
    HEALTH_REPORT_ENABLE                 (auto)  unset: only when running as a systemd service;
                                                 1: from anywhere; 0: never
    HEALTH_REPORT_CHAT_ID                oc_ad9b5bdbb2826ba2ee9730920ef25432
    HEALTH_REPORT_TIME                   09:00   local HH:MM; comma-separated for several a day
    HEALTH_REPORT_TZ                     UTC+8   IANA name (Asia/Kuala_Lumpur) or offset (+08:00)
    HEALTH_REPORT_CATCHUP                1       send a missed report later the same day
    HEALTH_REPORT_ON_START               0       1 also sends one report shortly after boot
    HEALTH_REPORT_ON_START_MIN_HOURS     6       ...but at most one startup report per this many hours
    HEALTH_REPORT_START_DELAY_SECONDS    120     boot grace before the first report may go out
    HEALTH_REPORT_CHECK_TIMEOUT_SECONDS  20
    HEALTH_REPORT_SEND_TIMEOUT_SECONDS   90
    HEALTH_REPORT_ERRORS_WARN            200     error lines in 24h that turn the card amber (0 = never)
    HEALTH_REPORT_RSS_WARN_MB            2048
    HEALTH_REPORT_STATE_DIR              /var/lib/health_report
    HEALTH_REPORT_STATE_FILE             (explicit state file path; overrides the directory)
    HEALTH_REPORT_BOT_NAME               (the name passed to start())
    HEALTH_REPORT_JOURNAL                1       scan this service's journald output for errors / crashes
    HEALTH_REPORT_SYSTEMD_UNIT           (detected from /proc/self/cgroup)

Preview the card without sending anything:  python health_report.py --preview
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("health_report")

DEFAULT_CHAT_ID = "oc_ad9b5bdbb2826ba2ee9730920ef25432"
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_T0 = time.time()
_WINDOW = 24 * 3600

_lock = threading.RLock()
_cfg = {
    "bot": "",
    "send": None,
    "checks": [],
    "expect_threads": [],
    "details": None,
    "started": False,
    "version_at_boot": None,
    "lockf": None,
    "send_key": None,     # Lark uuid for the send in flight (make_lark_sender dedupes on it)
    "boots": [],          # recent boot times (from the state file), for the restart-loop row
    "state_error": "",    # why the state file could not be written, if it could not
}
_inflight = {"thread": None, "box": None, "key": None}  # a send that outlived its timeout
_counters = collections.OrderedDict()   # name -> count since the last report
_counters_since = time.time()
_marks = collections.OrderedDict()      # name -> epoch of the last occurrence
_last_report = {"at": 0.0, "status": "", "error": ""}

_STATUS_ALIASES = {
    "ok": "ok", "pass": "ok", "up": "ok", "healthy": "ok", "good": "ok", "true": "ok",
    "warn": "warn", "warning": "warn", "degraded": "warn", "slow": "warn",
    "fail": "fail", "error": "fail", "down": "fail", "dead": "fail", "false": "fail", "critical": "fail",
    "skip": "skip", "skipped": "skip", "disabled": "skip", "off": "skip", "n/a": "skip", "na": "skip",
}
_RANK = {"skip": 0, "ok": 1, "warn": 2, "fail": 3}
_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}
_HEADER = {
    "ok": ("green", "🟢", "Healthy"),
    "warn": ("orange", "🟡", "Degraded"),
    "fail": ("red", "🔴", "Unhealthy"),
}
_LARK_HINTS = {
    230002: "the bot is not a member of that group; add it to the group",
    99991663: "tenant token invalid or expired",
    99991672: "the app lacks the im:message send permission",
}
_PERMANENT_CODES = (230002, 99991672)  # retrying these all day would not help
_TOKEN_CODES = (99991661, 99991663, 99991664, 99991668)


class LarkError(RuntimeError):
    def __init__(self, code, msg=""):
        self.code = code
        try:
            hint = _LARK_HINTS.get(int(code), "")
        except (TypeError, ValueError):
            hint = ""
        super().__init__("Lark code %s: %s%s" % (code, msg, " (%s)" % hint if hint else ""))


# ---------------------------------------------------------------- env helpers

_INLINE_COMMENT_RE = re.compile(r"\s+#(?:\s.*)?$")


def _env(name: str, default: str = "") -> str:
    """An env value, stripped. A trailing "  # comment" is dropped: systemd's EnvironmentFile
    keeps it as part of the value, python-dotenv does not."""
    v = os.getenv(name)
    v = _INLINE_COMMENT_RE.sub("", v).strip() if v is not None else ""
    return v or default


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name, "")
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on", "y")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


_FIXED_TZ_HOURS = {
    "asia/kuala_lumpur": 8, "asia/singapore": 8, "asia/shanghai": 8, "asia/hong_kong": 8,
    "asia/manila": 8, "asia/taipei": 8, "asia/jakarta": 7, "asia/bangkok": 7,
    "asia/tokyo": 9, "utc": 0, "etc/utc": 0, "gmt": 0,
}


def _tz():
    name = _env("HEALTH_REPORT_TZ", "UTC+8")
    m = re.fullmatch(r"(?i)(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", name)
    if m:
        hours, minutes = int(m.group(2)), int(m.group(3) or 0)
        if hours < 24 and minutes < 60:
            sign = 1 if m.group(1) == "+" else -1
            label = "UTC%s%d" % (m.group(1), hours) + (":%02d" % minutes if minutes else "")
            return timezone(sign * timedelta(hours=hours, minutes=minutes), label)
        logger.warning("health report: HEALTH_REPORT_TZ %r is out of range, using UTC+8", name)
        return timezone(timedelta(hours=8), "UTC+8")
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        return ZoneInfo(name)
    except Exception:
        pass
    hours = _FIXED_TZ_HOURS.get(name.lower())
    if hours is not None:
        return timezone(timedelta(hours=hours), name)
    logger.warning("health report: unknown HEALTH_REPORT_TZ %r, using UTC+8", name)
    return timezone(timedelta(hours=8), "UTC+8")


def _tz_label(dt: datetime) -> str:
    return dt.tzname() or "local"


def _slots():
    raw = _env("HEALTH_REPORT_TIME", "09:00")
    out = set()
    for part in re.split(r"[,;\s]+", raw):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", part)
        if m and int(m.group(1)) < 24 and int(m.group(2) or 0) < 60:
            out.add((int(m.group(1)), int(m.group(2) or 0)))
        else:
            logger.warning("health report: ignoring bad HEALTH_REPORT_TIME entry %r", part)
    return sorted(out) or [(9, 0)]


# ---------------------------------------------------------------- scheduling (pure)

def _slot_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _due_slot(now: datetime, slots, done_key: str | None, catchup: bool, grace_minutes: float = 10.0):
    """The latest of today's slots at or before ``now`` that is still unsent, else None.

    ``done_key`` is the slot key of the last report already handled. Without catch-up a
    slot only counts while ``now`` is within ``grace_minutes`` of it, so a late boot
    skips it instead of posting hours afterwards.
    """
    past = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in slots]
    past = [s for s in past if s <= now]
    if not past:
        return None
    latest = past[-1]
    if done_key and done_key >= _slot_key(latest):
        return None
    if not catchup and (now - latest) > timedelta(minutes=grace_minutes):
        return None
    return latest


def _next_slot(now: datetime, slots) -> datetime:
    for days in (0, 1):
        base = now + timedelta(days=days)
        for h, m in slots:
            cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand > now:
                return cand
    return now + timedelta(days=1)


# ---------------------------------------------------------------- state + singleton

def _bot_slug() -> str:
    name = _env("HEALTH_REPORT_BOT_NAME", "") or _cfg["bot"] or os.path.basename(_HERE)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "bot"


def _state_path() -> str:
    """State (and, next to it, the lock) is keyed by bot name in a host-wide directory,
    so two checkouts of the same bot on one host share one report."""
    p = _env("HEALTH_REPORT_STATE_FILE", "")
    if p:
        return p if os.path.isabs(p) else os.path.join(_HERE, p)
    if os.name == "posix":
        shared = _env("HEALTH_REPORT_STATE_DIR", "/var/lib/health_report")
        try:
            os.makedirs(shared, exist_ok=True)
            if os.access(shared, os.W_OK):
                return os.path.join(shared, _bot_slug() + ".json")
        except OSError:
            pass
    return os.path.join(_HERE, ".health_report_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("health report: state file unreadable, starting fresh", exc_info=True)
        return {}


def _save_state(data: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _cfg["state_error"] = ""
    except Exception as e:
        _cfg["state_error"] = _one_line("%s: %s" % (type(e).__name__, e), 160)
        logger.warning("health report: could not write %s", path, exc_info=True)


_KEY_RE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d")


def _valid_key(key, now: datetime):
    """A stored slot key, or None when it is malformed or implausibly in the future."""
    if isinstance(key, str) and _KEY_RE.fullmatch(key) and key <= _slot_key(now + timedelta(days=1)):
        return key
    if key is not None:
        logger.warning("health report: ignoring bad last_slot %r in the state file", key)
    return None


def _acquire_singleton() -> bool:
    """Hold an exclusive lock for the process lifetime; False if another process has it."""
    if _cfg["lockf"] is not None:
        return True
    path = os.path.splitext(_state_path())[0] + ".lock"
    try:
        f = open(path, "a+")
    except OSError:
        return True  # cannot create a lock file: better to report than to stay silent
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, ImportError):
        f.close()
        return False
    _cfg["lockf"] = f
    return True


# ---------------------------------------------------------------- log + crash tally

_SECRET_RE = re.compile(
    r"(?i)(\b\w*(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|private[_-]?key|encrypt[_-]?key|access[_-]?key"
    r"|authorization|cookie|session)\w*\\?[\"']?\s*[:=]\s*\\?[\"']?(?:(?:bearer|basic|token|bot)\s+)?"
    r"|\b(?:bearer|basic)\s+)(\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)"
)
_SECRET_EXTRA = (
    (re.compile(r"(?<=://)[^/\s:@]*:[^/\s@]+@"), "***@"),                            # https://user:pass@host
    (re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}"), "***"),                     # JWT
    (re.compile(r"\b([tu])-[A-Za-z0-9._-]{20,}"), r"\1-***"),                         # Lark tenant/user tokens
    (re.compile(r"(?i)((?:passw(?:or)?d|secret|token)%3D)[^&\s]+"), r"\1***"),        # url-encoded
)
_NUM_RE = re.compile(r"\b(?:0x)?[0-9a-f]*\d[0-9a-f]*\b", re.I)


def _redact(text: str) -> str:
    text = _SECRET_RE.sub(lambda m: m.group(1) + "***", text)
    for rx, repl in _SECRET_EXTRA:
        text = rx.sub(repl, text)
    return text


def _one_line(text: str, limit: int = 160) -> str:
    text = _redact(" ".join(str(text).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _LogTally(logging.Handler):
    """Counts WARNING+ records per rolling 24h and keeps the most frequent error lines."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.events = collections.deque(maxlen=50000)  # (epoch, levelno, key)
        self.samples = {}                              # key -> (last epoch, text)
        self.crashes = collections.deque(maxlen=200)   # (epoch, text)

    @staticmethod
    def _only_handler(record) -> bool:
        # A plain dict read: logging.getLogger() would take logging's module lock while
        # this handler's lock is held, the reverse of the order dictConfig() uses.
        lg = logging.Logger.manager.loggerDict.get(record.name) if record.name != "root" else logging.root
        if not isinstance(lg, logging.Logger):
            lg = logging.root
        while lg is not None:
            if any(h is not _tally for h in lg.handlers):
                return False
            if not lg.propagate:
                break
            lg = lg.parent
        return True

    def emit(self, record):
        try:
            if getattr(record, "_health_counted", False):
                return
            record._health_counted = True
            last = logging.lastResort
            if last is not None and record.levelno >= last.level and self._only_handler(record):
                last.handle(record)  # we are the only handler: keep Python's stderr fallback
            if record.name == logger.name:
                return
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)
            first = (msg.strip().splitlines() or [""])[0]
            if record.exc_info and record.exc_info[0] is not None:
                first = "%s (%s)" % (first, record.exc_info[0].__name__) if first else record.exc_info[0].__name__
            text = _one_line("[%s] %s" % (record.name, first))
            key = (record.name, _NUM_RE.sub("#", first)[:120])
            now = time.time()
            with _lock:
                self.events.append((now, record.levelno, key))
                if record.levelno >= logging.ERROR:
                    self.samples[key] = (now, text)
                    if len(self.samples) > 500:
                        for k, _ in sorted(self.samples.items(), key=lambda kv: kv[1][0])[:100]:
                            self.samples.pop(k, None)
        except Exception:
            pass

    def crash(self, text: str) -> None:
        with _lock:
            self.crashes.append((time.time(), _one_line(text)))

    def summary(self, now: float):
        cutoff = now - _WINDOW
        with _lock:
            recent = [e for e in self.events if e[0] >= cutoff]
            crashes = [c for c in self.crashes if c[0] >= cutoff]
            samples = dict(self.samples)
        errors = collections.Counter(k for _, lvl, k in recent if lvl >= logging.ERROR)
        n_warn = sum(1 for _, lvl, _k in recent if lvl == logging.WARNING)
        top = []
        for key, n in errors.most_common(5):
            at, text = samples.get(key, (0.0, _one_line("[%s] %s" % key)))
            top.append((n, at, text))
        return sum(errors.values()), n_warn, top, crashes


_tally = _LogTally()


_tally_loggers = []
_hook_installed = False


def _install_tally(logger_names=None, force: bool = False) -> None:
    """Attach the tally handler.

    The root logger only gets it once it already has a handler of its own: adding one
    earlier would turn a later ``logging.basicConfig()`` into a no-op. ``force`` (used
    after the boot grace) attaches anyway; ``emit`` then stands in for Python's
    last-resort stderr handler so nothing the bot logs goes missing.
    """
    with _lock:
        for n in logger_names or []:
            if n not in _tally_loggers:
                _tally_loggers.append(n)
        names = list(_tally_loggers)
    root = logging.getLogger()
    if _tally not in root.handlers and (root.handlers or force):
        root.addHandler(_tally)
    for n in names:
        lg = logging.getLogger(n)
        if _tally not in lg.handlers:
            lg.addHandler(_tally)
    global _hook_installed
    prev = getattr(threading, "excepthook", None)
    if prev is None or _hook_installed:
        return
    _hook_installed = True

    def hook(args):
        try:
            name = getattr(args.thread, "name", "?") if args.thread is not None else "?"
            _tally.crash("%s: %s: %s" % (name, getattr(args.exc_type, "__name__", args.exc_type), args.exc_value))
        except Exception:
            pass
        prev(args)

    hook._health_wrapped = True
    threading.excepthook = hook


# ---------------------------------------------------------------- activity counters

def bump(name: str, n: int = 1) -> None:
    """Add ``n`` to an activity counter; the card shows counts per report period."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def mark(name: str) -> None:
    """Record that ``name`` just happened; the card shows how long ago it last did."""
    with _lock:
        _marks[name] = time.time()


# ---------------------------------------------------------------- process + host metrics

def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _proc_status() -> dict:
    out = {}
    try:
        for line in _read("/proc/self/status").splitlines():
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def _kb_field(status: dict, key: str):
    try:
        return int(status[key].split()[0]) * 1024
    except Exception:
        return None


def _process_start_epoch() -> float:
    try:
        import psutil
        return float(psutil.Process().create_time())
    except Exception:
        pass
    try:
        stat = _read("/proc/self/stat")
        start_ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        btime = next(int(l.split()[1]) for l in _read("/proc/stat").splitlines() if l.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return _MODULE_T0


def _descendants():
    """(count, total RSS bytes, zombies) of this process's descendant processes (Linux)."""
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        children = collections.defaultdict(list)
        info = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                s = _read("/proc/%s/stat" % d)
            except Exception:
                continue
            rest = s[s.rindex(")") + 2:].split()
            pid = int(d)
            children[int(rest[1])].append(pid)
            info[pid] = (rest[0], int(rest[21]) * page)
        count = rss = zombies = 0
        stack = list(children.get(os.getpid(), []))
        while stack:
            pid = stack.pop()
            state, r = info.get(pid, ("?", 0))
            count += 1
            rss += r
            zombies += state == "Z"
            stack.extend(children.get(pid, []))
        return count, rss, zombies
    except Exception:
        return None


def _meminfo():
    try:
        vals = {}
        for line in _read("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            vals[k] = int(v.split()[0]) * 1024
        return vals.get("MemTotal"), vals.get("MemAvailable")
    except Exception:
        return None, None


def _open_fds():
    try:
        n = len(os.listdir("/proc/self/fd"))
    except Exception:
        return None, None
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        soft = None
    return n, (soft if soft and soft > 0 else None)


def _git_version(path: str = _HERE):
    try:
        r = subprocess.run(
            ["git", "-C", path, "log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        git = os.path.join(path, ".git")
        head = _read(os.path.join(git, "HEAD")).strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head[4:].strip()
        p = os.path.join(git, *ref.split("/"))
        if os.path.exists(p):
            return _read(p).strip()[:7]
        for line in _read(os.path.join(git, "packed-refs")).splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:7]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- systemd unit + journal

def _proc_ancestors(pid: int):
    chain = []
    for _ in range(64):
        chain.append(pid)
        if pid <= 1:
            break
        try:
            st = _read("/proc/%d/stat" % pid)
            pid = int(st[st.rindex(")") + 2:].split()[1])
        except Exception:
            break
    return chain


def _systemd_main_pid(unit: str):
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", "MainPID"], capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip().partition("=")[2])
    except Exception:
        return None  # cannot ask systemd: trust the cgroup


def _systemd_unit():
    """The .service this process runs in, or None.

    The cgroup names the unit; it only counts when that unit's main process is this
    process or one of its ancestors, so a bot started from cron, sshd or a CI job
    does not report that service's journal as its own. User-manager units are skipped
    (their journal field differs)."""
    u = _env("HEALTH_REPORT_SYSTEMD_UNIT", "")
    if u:
        return u if "." in u else u + ".service"
    try:
        text = _read("/proc/self/cgroup")
    except Exception:
        return None
    if re.search(r"/user@\d+\.service", text):
        return None
    unit = None
    for line in text.splitlines():
        units = re.findall(r"/([^/]+\.service)", line)
        if units:
            unit = units[-1]
            break
    if unit is None:
        return None
    main_pid = _systemd_main_pid(unit)
    if main_pid is None:
        return unit
    return unit if main_pid in _proc_ancestors(os.getpid()) else None


def _systemd_props(unit: str) -> dict:
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", "MemoryCurrent", "-p", "NRestarts", "-p", "TasksCurrent"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        k, _, v = line.partition("=")
        if v.isdigit() and int(v) < 2 ** 63:  # "[not set]" / UINT64_MAX when accounting is off
            out[k] = int(v)
    return out


_TS_PREFIX_RE = re.compile(r"^\W{0,3}\d{2,4}[-/:]\d{2}[-/:]\d{2}[T ,.:\d]*\s*")
_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|FATAL)\b")
_STRICT_ERR_RE = re.compile(r"\b(?:ERROR|CRITICAL|FATAL)\b|❌|\[error\]")
_LOOSE_ERR_RE = re.compile(r"(?i)\berror:|\bexception\b|\bfailed\b")
_ZERO_FAIL_RE = re.compile(r"(?i)\bfailed\s*[:=]\s*0\b|\b0\s+failed\b")   # "Failed: 0", "0 failed"
_WARN_LINE_RE = re.compile(r"\bWARN(?:ING)?\b|⚠")
_HINTS = ("error", "exception", "failed", "fatal", "critical", "warn", "❌", "⚠")  # cheap pre-filter
# Lines that echo what people wrote to the bot: never counted, never quoted.
_ECHO_RE = re.compile(r"(?i)raw_preview=|original[ _]text|raw_text=|clean_text=|text_preview=|\bcontent=|\bprompt=|user said")
# Lines about a person or a DM: counted, but their text is not shown in the group.
_PRIVATE_RE = re.compile(r"(?i)\bp2p\b|\bsender\b|\bou_[0-9a-f]{6,}|\bopen_id\b|\buser_id\b")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_TB_HEADER = "Traceback (most recent call last)"
_TB_CHAIN = ("During handling of the above exception", "The above exception was the direct cause")


def _classify_line(s: str):
    """'error', 'warning' or None for one line of a service's own output.

    Only the part before the first quote counts, so text quoted from a chat message
    can neither raise the count nor end up on the card. An explicit INFO/DEBUG level
    wins over words like "failed" further along the line."""
    low = s.lower()
    if not any(h in low for h in _HINTS):
        return None
    cut = min([i for i in (s.find("'"), s.find('"')) if i >= 0] or [len(s)])
    head = s[:cut]
    kind = None
    m = _LEVEL_RE.search(head[:100])
    if m:
        lvl = m.group(1)
        if lvl in ("ERROR", "CRITICAL", "FATAL"):
            kind = "error"
        elif lvl in ("WARNING", "WARN"):
            kind = "warning"
    elif _STRICT_ERR_RE.search(head) or (_LOOSE_ERR_RE.search(head) and not _ZERO_FAIL_RE.search(head)):
        kind = "error"
    elif _WARN_LINE_RE.search(head):
        kind = "warning"
    if kind and _ECHO_RE.search(s):
        return None
    return kind


def _scan_journal_lines(lines, deadline=None, max_lines: int = 3_000_000, max_keys: int = 2000) -> dict:
    """Tally error / warning lines and tracebacks in a service's own output.

    ``lines`` come NEWEST FIRST (journalctl -r), so a scan cut short by the deadline
    loses the oldest hours, not the ones just before the report. Read that way, a
    traceback shows its exception line first, then the indented frames, then the
    "Traceback" header; the exception line is taken when the header confirms it."""
    counts = collections.Counter()
    samples = {}
    res = {"lines": 0, "errors": 0, "warnings": 0, "tracebacks": 0, "top": [], "truncated": False}
    cand, cand_frames = None, False

    def add(text):
        if _PRIVATE_RE.search(text) or _ECHO_RE.search(text):
            return
        body = _QUOTED_RE.sub("'…'", _TS_PREFIX_RE.sub("", text.strip()))[:300]
        key = _NUM_RE.sub("#", body)[:100]
        if key in counts or len(counts) < max_keys:
            counts[key] += 1
            samples[key] = body

    for line in lines:
        res["lines"] += 1
        if res["lines"] > max_lines or (deadline is not None and res["lines"] % 2000 == 0 and time.monotonic() > deadline):
            res["truncated"] = True
            break
        s = line.rstrip("\r\n")
        if not s.strip():
            continue
        if s.startswith(_TB_HEADER):
            res["tracebacks"] += 1
            if cand is not None and cand_frames:
                add(cand)  # the exception line that ended this traceback
            cand, cand_frames = None, False
            continue
        if s[0] in " \t":
            if cand is not None:
                cand_frames = True
            continue
        if s.startswith(_TB_CHAIN):
            cand, cand_frames = None, False
            continue
        cand, cand_frames = s, False
        kind = _classify_line(s)
        if kind == "error":
            res["errors"] += 1
            add(s)
        elif kind == "warning":
            res["warnings"] += 1
    res["top"] = [(n, _one_line(samples[k])) for k, n in counts.most_common(5)]
    return res


def _journal_stream(args, timeout: float):
    p = subprocess.Popen(["journalctl", "--no-pager", "-q", "-o", "cat"] + args,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace")
    flag = {"killed": False}

    def kill():
        flag["killed"] = True
        p.kill()

    killer = threading.Timer(timeout, kill)  # a stuck journalctl must not stall the report
    killer.daemon = True
    killer.start()
    return p, killer, flag


def _stream_done(p, killer) -> int:
    """Stop a journalctl stream; its exit code (None when it had to be killed)."""
    killer.cancel()
    code = p.poll()
    if code is None:
        p.kill()
    p.stdout.close()
    p.wait(timeout=5)
    return code


def _journal_scan(unit: str, timeout: float = 40.0) -> dict:
    """The scan itself; runs in a short-lived child process (see _journal)."""
    since = "24 hours ago"
    p, killer, flag = _journal_stream(["-r", "_SYSTEMD_UNIT=" + unit, "--since", since], timeout + 5)
    try:
        res = _scan_journal_lines(p.stdout, deadline=time.monotonic() + timeout)
    finally:
        code = _stream_done(p, killer)
    if flag["killed"]:
        res["truncated"] = True
    elif code not in (None, 0) and not res["truncated"]:
        res["error"] = "journalctl exited %s" % code
    starts = crashes = sd_oom_msgs = sd_oom_results = 0
    last_crash = ""
    p, killer, _flag = _journal_stream(["_PID=1", "UNIT=" + unit, "--since", since], 20.0)
    try:
        for line in p.stdout:
            if line.startswith("Started "):
                starts += 1
            elif "Failed with result" in line:
                crashes += 1
                last_crash = line.strip()
                if "'oom-kill'" in line:
                    sd_oom_results += 1
            if "killed by the OOM killer" in line:
                sd_oom_msgs += 1
    finally:
        _stream_done(p, killer)
    # The kernel log names the unit's cgroup on every OOM kill, also on systemd 239 / cgroup v1.
    k_oom = 0
    p, killer, _flag = _journal_stream(["-k", "--since", since], 20.0)
    try:
        needle = "/" + unit
        for line in p.stdout:
            if "oom-kill:" in line and needle in line:
                k_oom += 1
    finally:
        _stream_done(p, killer)
    res.update(unit=unit, starts=starts, crashes=crashes, oom=max(sd_oom_msgs, sd_oom_results, k_oom),
               last_crash=_one_line(last_crash, 120))
    return res


def _journal(unit: str, timeout: float = 40.0):
    """24h view of this service in journald: its own error lines, plus systemd's start /
    failure records. The scan runs in a child Python process so a big journal costs the
    bot neither CPU time under its GIL nor memory it would keep afterwards."""
    if not unit or not shutil.which("journalctl"):
        return None
    script = os.path.join(_HERE, "health_report.py")
    if not os.path.exists(script):
        script = os.path.abspath(__file__)
    try:
        r = subprocess.run([sys.executable, script, "--journal-json", unit, "--timeout", str(timeout)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout + 90)
        out = (r.stdout or "").strip().splitlines()
        data = json.loads(out[-1]) if r.returncode == 0 and out else None
    except Exception:
        logger.debug("health report: journal scan failed", exc_info=True)
        return None
    if not isinstance(data, dict) or data.get("error") or not data.get("lines"):
        return {"unavailable": (data or {}).get("error") or "no journal lines for %s" % unit}
    data["top"] = [tuple(x) for x in data.get("top", [])]
    return data


def _fmt_bytes(n) -> str:
    if n is None:
        return "n/a"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def _fmt_span(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm" % m
    return "%ds" % s


# ---------------------------------------------------------------- checks

def _normalize(res):
    if isinstance(res, dict):
        status, detail = res.get("status", res.get("ok")), res.get("detail", res.get("message", ""))
    elif isinstance(res, (tuple, list)) and len(res) >= 2:
        status, detail = res[0], res[1]
    elif isinstance(res, str):
        status, detail = "ok", res
    elif res is None or isinstance(res, bool):
        status, detail = res, ""
    elif isinstance(res, int):  # e.g. an HTTP status code
        status, detail = ("ok" if 200 <= res < 400 else "fail"), "HTTP %d" % res
    else:
        status, detail = "warn", "unexpected check result: %s" % (res,)
    if status is True:
        s = "ok"
    elif status is False:
        s = "fail"
    elif status is None:
        s = "skip"
    else:
        s = _STATUS_ALIASES.get(str(status).strip().lower(), "warn")
    return s, _one_line(detail if detail is not None else "", 200)


def _run_checks(checks, timeout: float):
    results = [None] * len(checks)

    def run(i, fn):
        t0 = time.monotonic()
        try:
            status, detail = _normalize(fn())
        except BaseException as e:  # SystemExit from a helper must not look like a hang
            status, detail = "fail", _one_line("%s: %s" % (type(e).__name__, e), 200)
        results[i] = (status, detail, time.monotonic() - t0)

    threads = []
    for i, (_name, fn) in enumerate(checks):
        t = threading.Thread(target=run, args=(i, fn), daemon=True, name="health-check-%d" % i)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    out = []
    for (name, _fn), r in zip(checks, results):
        out.append((name,) + (r if r else ("fail", "no answer within %.0fs" % timeout, timeout)))
    return out


def register_check(name: str, fn) -> None:
    """Add a check after start(); same contract as the ``checks`` argument."""
    with _lock:
        _cfg["checks"].append((name, fn))


# ---------------------------------------------------------------- report

def _md(text) -> str:
    """Make untrusted text inert inside a Lark lark_md / markdown block."""
    return (str(text).replace("<", "‹").replace(">", "›").replace("*", "∗")
            .replace("~", "∼").replace("`", "'").replace("](", "] ("))


# Other bots in the report group read the text of every message, cards included, and
# act on it: P0 / P1 incident prompts, deploy phrases ("git pull ... restart"), slash
# commands anywhere in the text, @_user_N mention placeholders, Jenkins email commands.
# Some of them strip zero-width characters and fold look-alike slashes first, so the
# card text is rewritten visibly: readable for people, unmatchable for those detectors.
_DEFANG_RULES = (
    (re.compile(r"(?i)(?<![0-9a-z])(p)([0-4])(?![0-9a-z])"), r"\1-\2"),          # P0 -> P-0
    (re.compile(r"(?i)\b(git)\s+(pull)\b"), r"\1-\2"),                            # git pull -> git-pull
    (re.compile(r"(?i)\b(pull)\s+(origin|code|repo|latest)\b"), r"\1-\2"),
    (re.compile("\u91cd\u542f"), "\u91cd \u542f"),                                  # 重启
    (re.compile(r"@_user_"), "@user_"),
    (re.compile(r"(?i)(reply)(updateemail)|(testreply)(email)|(pick)(email)"),
     lambda m: "-".join(g for g in m.groups() if g)),
    (re.compile(r"/(?=[A-Za-z])"), "\u2571"),                                          # /cmd -> ╱cmd
)


def _defang_text(text: str) -> str:
    for rx, repl in _DEFANG_RULES:
        text = rx.sub(repl, text)
    return text


def _defang(obj):
    if isinstance(obj, str):
        return _defang_text(obj)
    if isinstance(obj, dict):
        return {k: (v if k in ("tag", "template") else _defang(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_defang(v) for v in obj]
    return obj


def _call_with_timeout(fn, timeout: float, what: str):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised in the caller's thread, as an Exception
            box["error"] = e if isinstance(e, Exception) else RuntimeError("%s raised %r" % (what, e))

    t = threading.Thread(target=run, daemon=True, name="health-call")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("%s did not finish within %.0fs" % (what, timeout))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def collect(reason: str = "manual") -> dict:
    """Gather everything the card shows. Safe to call from any thread."""
    tz = _tz()
    now_ts = time.time()
    now = datetime.fromtimestamp(now_ts, tz)
    with _lock:
        checks = list(_cfg["checks"])
        expect = list(_cfg["expect_threads"])
        counters = list(_counters.items())
        collected_at = time.time()
        since = _counters_since
        marks = list(_marks.items())
        details_fn = _cfg["details"]
    rows = []  # (name, status, detail, seconds)
    rows.extend(_run_checks(checks, max(1.0, _env_float("HEALTH_REPORT_CHECK_TIMEOUT_SECONDS", 20.0))))

    if expect:
        alive = [t.name for t in threading.enumerate() if t.is_alive()]
        dead = [n for n in expect if not any(a == n or a.startswith(n) for a in alive)]
        if dead:
            rows.append(("Background threads", "fail", "not running: " + ", ".join(dead), 0.0))
        else:
            rows.append(("Background threads", "ok", "all %d running" % len(expect), 0.0))

    status = _proc_status()
    rss, peak = _kb_field(status, "VmRSS"), _kb_field(status, "VmHWM")
    if rss is None:
        try:
            import psutil
            rss = psutil.Process().memory_info().rss
        except Exception:
            pass
    mem_total, mem_avail = _meminfo()
    try:
        disk = shutil.disk_usage(_HERE)
    except Exception:
        disk = None
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    fds, fd_limit = _open_fds()
    desc = _descendants()
    cpus = os.cpu_count() or 1
    started = _process_start_epoch()
    cpu = os.times()

    rss_warn = _env_float("HEALTH_REPORT_RSS_WARN_MB", 2048.0) * 1024 * 1024
    if rss is not None and rss > rss_warn:
        rows.append(("Memory (bot)", "warn", "RSS %s is above %s" % (_fmt_bytes(rss), _fmt_bytes(rss_warn)), 0.0))
    if disk is not None and disk.total:
        free = disk.free / disk.total
        if free < 0.10:
            rows.append(("Disk", "fail" if free < 0.05 else "warn",
                         "%.0f%% free (%s left)" % (free * 100, _fmt_bytes(disk.free)), 0.0))
    if mem_total and mem_avail is not None:
        avail = mem_avail / mem_total
        if avail < 0.10:
            rows.append(("Memory (host)", "fail" if avail < 0.05 else "warn",
                         "%.0f%% available (%s)" % (avail * 100, _fmt_bytes(mem_avail)), 0.0))
    if load is not None and load[1] / cpus > 2.0:
        rows.append(("Host load", "warn", "5-min load %.1f on %d CPUs" % (load[1], cpus), 0.0))
    if fds is not None and fd_limit and fds > 0.8 * fd_limit:
        rows.append(("Open files", "warn", "%d of limit %d" % (fds, fd_limit), 0.0))
    if desc is not None and desc[2] >= 5:
        rows.append(("Child processes", "warn", "%d zombie processes" % desc[2], 0.0))

    unit = _systemd_unit()
    svc = _systemd_props(unit) if unit else {}
    journal = None
    if unit and _env_bool("HEALTH_REPORT_JOURNAL", True):
        try:
            journal = _call_with_timeout(lambda: _journal(unit), 150.0, "journal scan")
        except Exception:
            journal = None
    journal_note = ""
    if journal is not None and "unavailable" in journal:
        journal_note, journal = journal["unavailable"], None
    n_err, n_warn, top, crashes = _tally.summary(now_ts)
    if journal is not None:
        n_err, n_warn = journal["errors"], journal["warnings"]
        top = [(n, 0.0, text) for n, text in journal["top"]]
        if journal["crashes"]:
            rows.append(("Service failures", "warn", "systemd recorded %d in the last 24h; last: %s" % (
                journal["crashes"], journal["last_crash"] or "?"), 0.0))
        if journal["oom"]:
            rows.append(("OOM killer", "warn", "killed a process of this service %d time(s) in 24h" % journal["oom"], 0.0))
    err_warn = int(_env_float("HEALTH_REPORT_ERRORS_WARN", 200))
    if crashes:
        rows.append(("Thread crashes", "warn", "%d in the last 24h" % len(crashes), 0.0))
    if err_warn > 0 and n_err >= err_warn:
        rows.append(("Log errors", "warn", "%d error lines in the last 24h" % n_err, 0.0))
    recent_boots = sum(1 for b in _cfg["boots"] if 0 <= now_ts - b <= 1800)
    if recent_boots >= 3:
        rows.append(("Restarts", "warn", "%d boots in the last 30 min" % recent_boots, 0.0))
    if _cfg["state_error"]:
        rows.append(("Report state", "warn", "not saved, a restart may resend: %s" % _cfg["state_error"], 0.0))

    overall = "ok"
    for _n, s, _d, _t in rows:
        if _RANK[s] > _RANK[overall]:
            overall = s

    version_now = _git_version()
    version = _cfg["version_at_boot"] or version_now or "n/a"
    if version_now and _cfg["version_at_boot"] and version_now.split()[0] != _cfg["version_at_boot"].split()[0]:
        version += " (on disk %s, not running yet)" % version_now.split()[0]

    details = []
    if details_fn is not None:
        try:
            d = _call_with_timeout(details_fn, max(1.0, _env_float("HEALTH_REPORT_CHECK_TIMEOUT_SECONDS", 20.0)), "details")
            items = list(d.items()) if isinstance(d, dict) else list(d or [])
            for item in items[:20]:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    details.append((_one_line(item[0], 60), _one_line(item[1], 200)))
        except Exception as e:
            details = [("details", "unavailable: %s" % _one_line(e, 120))]

    slots = _slots()
    return {
        "bot": _env("HEALTH_REPORT_BOT_NAME", "") or _cfg["bot"] or os.path.basename(_HERE),
        "reason": reason,
        "status": overall,
        "now": now,
        "tz": _tz_label(now),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "started": datetime.fromtimestamp(started, tz),
        "uptime": now_ts - started,
        "version": version,
        "rows": rows,
        "rss": rss, "peak": peak,
        "cpu_seconds": cpu.user + cpu.system,
        "threads": threading.active_count(),
        "os_threads": status.get("Threads"),
        "fds": fds, "fd_limit": fd_limit,
        "descendants": desc,
        "load": load, "cpus": cpus,
        "mem_total": mem_total, "mem_avail": mem_avail,
        "disk": disk,
        "counters": counters, "counters_since": datetime.fromtimestamp(since, tz), "collected_at": collected_at,
        "marks": [(n, now_ts - t) for n, t in marks],
        "errors": n_err, "warnings": n_warn, "top_errors": top, "crashes": crashes,
        "unit": unit, "service": svc, "journal": journal, "journal_note": journal_note,
        "details": details,
        "next": _next_slot(now, slots),
        "schedule": ", ".join("%02d:%02d" % s for s in slots),
    }


def build_card(reason: str = "manual", data: dict | None = None) -> dict:
    """The interactive card (Lark message card v1 JSON) for the current health."""
    d = data or collect(reason)
    color, dot, label = _HEADER[d["status"]]
    tz = d["tz"]

    def field(title, value):
        return {"is_short": True, "text": {"tag": "lark_md", "content": "**%s**\n%s" % (title, _md(value))}}

    bad = [r for r in d["rows"] if r[1] in ("warn", "fail")]
    status_text = label if not bad else "%s (%d issue%s)" % (label, len(bad), "" if len(bad) == 1 else "s")
    elements = [{
        "tag": "div",
        "fields": [
            field("Status", status_text),
            field("Report time", "%s %s" % (d["now"].strftime("%Y-%m-%d %H:%M"), tz)),
            field("Host", d["host"]),
            field("Uptime", "%s (since %s)" % (_fmt_span(d["uptime"]), d["started"].strftime("%m-%d %H:%M"))),
            field("Version", d["version"]),
            field("Process", "PID %s · Python %s" % (d["pid"], d["python"])),
        ],
    }, {"tag": "hr"}]

    if d["rows"]:
        lines = []
        for name, s, detail, secs in sorted(d["rows"], key=lambda r: -_RANK[r[1]]):
            extra = " (%.1fs)" % secs if secs >= 2 else ""
            lines.append("%s **%s**%s%s" % (_ICON[s], _md(name), (" · " + _md(detail)) if detail else "", extra))
        elements.append({"tag": "markdown", "content": "**Checks**\n" + "\n".join(lines)})
    else:
        elements.append({"tag": "markdown", "content": "**Checks**\n✅ Process is running (no dependency checks registered)"})

    res = ["Memory: %s RSS%s" % (_fmt_bytes(d["rss"]), " · peak %s" % _fmt_bytes(d["peak"]) if d["peak"] else "")]
    res.append("CPU time: %s · threads: %s%s" % (
        _fmt_span(d["cpu_seconds"]), d["threads"],
        " (%s OS)" % d["os_threads"] if d["os_threads"] else ""))
    if d["descendants"] is not None and d["descendants"][0]:
        c, r, z = d["descendants"]
        res.append("Child processes: %d using %s%s" % (c, _fmt_bytes(r), " · %d zombie" % z if z else ""))
    if d["fds"] is not None:
        res.append("Open files: %d%s" % (d["fds"], " / %d" % d["fd_limit"] if d["fd_limit"] else ""))
    host = []
    if d["load"] is not None:
        host.append("load %.2f / %.2f / %.2f on %d CPUs" % (d["load"] + (d["cpus"],)))
    if d["mem_total"]:
        host.append("memory %s free of %s" % (_fmt_bytes(d["mem_avail"]), _fmt_bytes(d["mem_total"])))
    if d["disk"] is not None and d["disk"].total:
        host.append("disk %s free (%.0f%%)" % (_fmt_bytes(d["disk"].free), 100.0 * d["disk"].free / d["disk"].total))
    if host:
        res.append("Host: " + " · ".join(host))
    if d["unit"]:
        svc_bits = [d["unit"]]
        if d["service"].get("MemoryCurrent"):
            svc_bits.append("memory %s incl. children" % _fmt_bytes(d["service"]["MemoryCurrent"]))
        j = d["journal"]
        if j is not None:
            svc_bits.append("%d start%s, %d failure%s in 24h" % (
                j["starts"], "" if j["starts"] == 1 else "s", j["crashes"], "" if j["crashes"] == 1 else "s"))
        res.append("Service: " + " · ".join(svc_bits))
    elements.append({"tag": "markdown", "content": "**Resources**\n" + "\n".join(_md(x) for x in res)})

    act = ["%s: %s" % (_md(n), v) for n, v in d["counters"]]
    act += ["%s: %s ago" % (_md(n), _fmt_span(ago)) for n, ago in d["marks"]]
    act += ["%s: %s" % (_md(k), _md(v)) for k, v in d["details"]]
    if act:
        elements.append({"tag": "markdown", "content": "**Activity** (since %s)\n%s" % (
            d["counters_since"].strftime("%m-%d %H:%M"), "\n".join(act))})

    j = d["journal"]
    if j is not None:
        log_title = "**Logs, last 24h** (journal)"
        log_lines = ["%d error lines · %d warnings · %d tracebacks%s" % (
            d["errors"], d["warnings"], j["tracebacks"], " (scan cut short)" if j["truncated"] else "")]
    else:
        since = "last 24h" if d["uptime"] >= _WINDOW else "since boot"
        log_title = "**Logs, %s** (this process%s)" % (since, "; journal: " + d["journal_note"] if d.get("journal_note") else "")
        log_lines = ["%d errors · %d warnings" % (d["errors"], d["warnings"])]
    if d["crashes"]:
        log_lines[0] += " · %d thread crash%s" % (len(d["crashes"]), "" if len(d["crashes"]) == 1 else "es")
    for n, at, text in d["top_errors"]:
        when = ", last %s" % datetime.fromtimestamp(at, d["now"].tzinfo).strftime("%H:%M") if at else ""
        log_lines.append("×%d%s: %s" % (n, when, text))
    for at, text in d["crashes"][-3:]:
        log_lines.append("crash %s: %s" % (datetime.fromtimestamp(at, d["now"].tzinfo).strftime("%H:%M"), text))
    elements.append({"tag": "markdown", "content": log_title + "\n" + "\n".join(_md(x) for x in log_lines)})

    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "%s · daily at %s %s · next %s" % (
        d["reason"], d["schedule"], tz, d["next"].strftime("%m-%d %H:%M"))}]})

    return _defang({
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": "%s %s · Health report" % (dot, d["bot"])}},
        "elements": elements,
    })


def render_text(card: dict) -> str:
    """Plain-text view of a card, for logs and --preview."""
    out = [card.get("header", {}).get("title", {}).get("content", "")]
    for el in card.get("elements", []):
        tag = el.get("tag")
        if tag == "div":
            for f in el.get("fields", []):
                out.append(f["text"]["content"].replace("**", "").replace("\n", ": ", 1))
        elif tag == "markdown":
            out.append(el["content"].replace("**", ""))
        elif tag == "note":
            out.append(" ".join(e.get("content", "") for e in el.get("elements", [])))
        elif tag == "hr":
            out.append("-" * 40)
    return "\n".join(out).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _raise_if_failed(res) -> None:
    if res is False:
        raise RuntimeError("sender returned False")
    if isinstance(res, (tuple, list)) and res and res[0] is False:  # e.g. (ok, message_id)
        raise RuntimeError("sender reported failure: %s" % _one_line(res[1:], 200))
    code, msg = None, ""
    if isinstance(res, dict):
        code, msg = res.get("code"), res.get("msg", "")
    elif callable(getattr(res, "success", None)):  # lark-oapi response object
        if res.success():
            return
        code, msg = getattr(res, "code", "?"), getattr(res, "msg", "")
    if code not in (None, 0, "0"):
        raise LarkError(code, msg)


def _outcome(box):
    if "error" in box:
        return "error", box["error"]
    try:
        _raise_if_failed(box.get("value"))
    except Exception as e:
        return "error", e
    return "ok", box.get("value")


def _deliver(sender, chat: str, card: dict, key: str, timeout: float):
    """One send attempt: ("ok", result), ("error", exc) or ("pending", None).

    A send that outlives ``timeout`` is not abandoned: the next attempt for the same
    report waits for it and takes its result instead of posting a second card."""
    t = _inflight["thread"]
    if t is not None and _inflight["key"] == key:
        t.join(timeout)
        if t.is_alive():
            return "pending", None
        box = _inflight["box"]
        _inflight.update(thread=None, box=None, key=None)
        return _outcome(box)
    box = {}

    def run():
        try:
            box["value"] = sender(chat, card)
        except BaseException as e:
            box["error"] = e if isinstance(e, Exception) else RuntimeError("send_card raised %r" % (e,))

    t = threading.Thread(target=run, daemon=True, name="health-send")
    t.start()
    t.join(timeout)
    if t.is_alive():
        _inflight.update(thread=t, box=box, key=key)
        return "pending", None
    return _outcome(box)


def _send_key(key: str, chat: str) -> str:
    return hashlib.sha1(("%s|%s|%s" % (_bot_slug(), key, chat)).encode("utf-8")).hexdigest()[:32]


def _finish(data: dict, chat: str) -> None:
    """Book a delivered report: counters restart from the snapshot the card showed."""
    global _counters_since
    with _lock:
        for k, v in data["counters"]:
            _counters[k] = max(0, _counters.get(k, 0) - v)
        _counters_since = data["collected_at"]
        _last_report.update(at=time.time(), status=data["status"], error="")
    bad = ["%s=%s" % (r[0], r[1]) for r in data["rows"] if r[1] in ("warn", "fail")]
    logger.info("health report sent to %s: %s%s", chat, data["status"], (" (" + ", ".join(bad) + ")") if bad else "")


def _send_timeout() -> float:
    return max(1.0, _env_float("HEALTH_REPORT_SEND_TIMEOUT_SECONDS", 90.0))


def send_now(reason: str = "manual", chat_id: str | None = None):
    """Build and send one report right now (no retries). Raises when the send fails."""
    sender = _cfg["send"]
    if sender is None:
        raise RuntimeError("health report: no sender configured (call start() first)")
    chat = chat_id or _env("HEALTH_REPORT_CHAT_ID", DEFAULT_CHAT_ID)
    data = collect(reason)
    card = build_card(reason, data)
    key = "manual-%d" % int(time.time())
    _cfg["send_key"] = _send_key(key, chat)
    outcome, val = _deliver(sender, chat, card, key, _send_timeout())
    if outcome != "ok":
        err = val if outcome == "error" else TimeoutError("send_card still running after %.0fs" % _send_timeout())
        with _lock:
            _last_report.update(error=_one_line(err, 300))
        raise err
    _finish(data, chat)
    return val


def _send_with_retries(reason: str, key: str, until: datetime | None = None) -> bool:
    """Send one report, retrying until ``until``. The card is built once and reused, so
    every attempt carries the same content and the same Lark uuid."""
    sender = _cfg["send"]
    chat = _env("HEALTH_REPORT_CHAT_ID", DEFAULT_CHAT_ID)
    data = collect(reason)
    card = build_card(reason, data)
    _cfg["send_key"] = _send_key(key, chat)
    fast = (0, 30, 120, 300)
    attempt = 0
    while True:
        delay = fast[attempt] if attempt < len(fast) else 1200
        if attempt and until is not None and datetime.now(until.tzinfo) + timedelta(seconds=delay) >= until:
            break
        if delay:
            time.sleep(delay)
        attempt += 1
        outcome, val = _deliver(sender, chat, card, key, _send_timeout())
        if outcome == "ok":
            _finish(data, chat)
            return True
        if outcome == "pending":
            logger.warning("health report: send still running after %.0fs; waiting for it rather than sending twice",
                           _send_timeout())
            continue
        err = _one_line(val, 300)
        with _lock:
            _last_report.update(error=err)
        logger.warning("health report: send attempt %d failed: %s", attempt, err)
        if getattr(val, "code", None) in _PERMANENT_CODES and attempt >= len(fast):
            break
    logger.error("health report: giving up on this report after %d attempts", attempt)
    return False


def _mark_sent(state: dict, key: str, tz) -> None:
    state.update(last_slot=key, last_sent_at=datetime.now(tz).isoformat(timespec="seconds"),
                 last_status=_last_report["status"], last_error="")
    _save_state(state)


def _slot_deadline(due: datetime, slots) -> datetime:
    """Retries for a slot stop at the next slot or at midnight, whichever comes first."""
    midnight = (due + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return min(_next_slot(due, slots), midnight)


def _loop() -> None:
    try:
        _run_scheduler()
    except BaseException:  # should be unreachable; never die silently
        logger.exception("health report: scheduler stopped")


def _run_scheduler() -> None:
    boot = time.time()
    delay = max(0.0, _env_float("HEALTH_REPORT_START_DELAY_SECONDS", 120.0))
    if _acquire_singleton():
        state = _load_state()
        boots = [b for b in state.get("boots", []) if isinstance(b, (int, float)) and boot - 1800 <= b <= boot]
        boots.append(boot)
        state["boots"] = boots[-10:]
        _save_state(state)
        _cfg["boots"] = list(boots)
        if len(boots) >= 3:  # restarting repeatedly: report before the next crash, not after
            delay = min(delay, 15.0)
    time.sleep(delay)
    _install_tally(force=True)
    startup_pending = _env_bool("HEALTH_REPORT_ON_START", False)
    done_key = None
    waiting_logged = False
    while True:
        wait = 300.0
        try:
            if not _acquire_singleton():
                if not waiting_logged:
                    logger.info("health report: another process owns the %s report; waiting", _bot_slug())
                    waiting_logged = True
                wait = 60.0
            else:
                tz = _tz()
                slots = _slots()
                now = datetime.now(tz)
                state = _load_state()
                disk_key = _valid_key(state.get("last_slot"), now)
                if disk_key and (done_key is None or disk_key > done_key):
                    done_key = disk_key
                if startup_pending:
                    startup_pending = False
                    last = state.get("last_startup_at")
                    gap = _env_float("HEALTH_REPORT_ON_START_MIN_HOURS", 6.0) * 3600
                    if isinstance(last, (int, float)) and 0 <= time.time() - last < gap:
                        logger.info("health report: startup report skipped, one went out %s ago",
                                    _fmt_span(time.time() - last))
                    elif _send_with_retries("Startup report", "startup-%d" % int(boot), now + timedelta(minutes=10)):
                        state = _load_state()
                        state["last_startup_at"] = time.time()
                        due = _due_slot(datetime.now(tz), slots, done_key, True)
                        if due is not None:  # the startup report stands in for today's missed one
                            done_key = _slot_key(due)
                            _mark_sent(state, done_key, tz)
                        else:
                            _save_state(state)
                    now = datetime.now(tz)
                due = _due_slot(now, slots, done_key, _env_bool("HEALTH_REPORT_CATCHUP", True))
                if due is not None:
                    key = _slot_key(due)
                    if boot > due.timestamp():
                        reason = "Catch-up for the %s report (bot was not running then)"
                    elif now - due > timedelta(minutes=10):
                        reason = "Delayed %s report"
                    else:
                        reason = "Scheduled %s report"
                    ok = _send_with_retries(reason % due.strftime("%H:%M"), key, _slot_deadline(due, slots))
                    done_key = key  # never start this slot again in this process
                    state = _load_state()
                    if ok:
                        _mark_sent(state, key, tz)
                    else:
                        state.update(last_failed_slot=key, last_error=_last_report["error"])
                        _save_state(state)
                    now = datetime.now(tz)
                wait = min(300.0, max(5.0, (_next_slot(now, slots) - now).total_seconds()))
        except BaseException:  # nothing may end the scheduler thread
            logger.exception("health report: scheduler iteration failed")
            wait = 300.0
        time.sleep(wait)


def start(bot_name: str, send_card=None, checks=None, expect_threads=None, loggers=None, details=None) -> bool:
    """Start the daily report thread once per process. Returns True when it started here.
    Never raises: a problem here is logged and the bot boots as usual.

    bot_name        shown in the card title; also keys the host-wide state and lock
    send_card       callable(chat_id, card_dict) -> result; see the module docstring
    checks          list of (name, zero-arg callable)
    expect_threads  thread names (or name prefixes) that must be alive
    loggers         extra logger names to tally when they do not propagate to root
    details         zero-arg callable returning [(label, value)] or a dict for the Activity block
    """
    try:
        version = _git_version()
        with _lock:
            if _cfg["started"]:
                return False
            _cfg.update(bot=bot_name, send=send_card, details=details,
                        expect_threads=list(expect_threads or []), version_at_boot=version)
            _cfg["checks"] = list(checks or []) + _cfg["checks"]
            _cfg["started"] = True
        _install_tally(loggers)
        enable = _env("HEALTH_REPORT_ENABLE", "").lower()
        if enable in ("0", "false", "no", "off", "n"):
            logger.info("health report: disabled (HEALTH_REPORT_ENABLE=0)")
            return False
        if not enable and _systemd_unit() is None:
            # A copy of the bot run by hand (a PC with the server's .env, a test in a shell)
            # cannot see the service's "already sent" state and would post a second card.
            logger.info("health report: off here, not running as a systemd service "
                        "(HEALTH_REPORT_ENABLE=1 sends from here too)")
            return False
        if send_card is None:
            logger.warning("health report: no sender given, daily report not started")
            return False
        schedule = ", ".join("%02d:%02d" % s for s in _slots())
        tz_label = _tz_label(datetime.now(_tz()))
        if not _acquire_singleton():
            logger.info("health report: another process owns the %s report; this one waits", _bot_slug())
        threading.Thread(target=_loop, daemon=True, name="health-report").start()
        logger.info("health report: daily at %s %s to %s", schedule, tz_label,
                    _env("HEALTH_REPORT_CHAT_ID", DEFAULT_CHAT_ID))
        return True
    except Exception:
        logger.exception("health report: could not start")
        return False


def _after_fork_in_child() -> None:
    global _lock
    _lock = threading.RLock()  # a lock held by another thread at fork time would never be released
    _cfg["lockf"] = None       # the child is never the sender
    _inflight.update(thread=None, box=None, key=None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def make_lark_sender(app_id: str, app_secret: str, base_url: str = "https://open.larksuite.com", timeout: float = 15.0):
    """A stdlib-only card sender with its own tenant-token cache. Raises on any failure.
    Each report carries a Lark uuid, so a retry of a send that did arrive is dropped by Lark."""
    import urllib.error
    import urllib.request

    base = base_url.rstrip("/")
    cache = {"token": "", "exp": 0.0}
    token_lock = threading.Lock()

    def post(url, payload, token=""):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, body = getattr(r, "status", 200), r.read()
        except urllib.error.HTTPError as e:  # Lark puts the reason in a JSON error body
            status, body = e.code, e.read()
        try:
            j = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise RuntimeError("Lark HTTP %s returned non-JSON: %s" % (status, _one_line(body[:200])))
        if not isinstance(j, dict) or "code" not in j:
            raise RuntimeError("Lark HTTP %s without a result code: %s" % (status, _one_line(str(j)[:200])))
        return j

    def token():
        with token_lock:
            if cache["token"] and time.time() < cache["exp"]:
                return cache["token"]
            j = post(base + "/open-apis/auth/v3/tenant_access_token/internal",
                     {"app_id": app_id, "app_secret": app_secret})
            if j.get("code") != 0 or not j.get("tenant_access_token"):
                raise LarkError(j.get("code") or -1, j.get("msg") or "no tenant_access_token")
            cache["token"] = j["tenant_access_token"]
            cache["exp"] = time.time() + max(60, int(j.get("expire", 7200)) - 300)
            return cache["token"]

    def send(chat_id, card):
        if not app_id or not app_secret:
            raise RuntimeError("Lark app id / secret not configured")
        payload = {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}
        if _cfg.get("send_key"):
            payload["uuid"] = _cfg["send_key"]
        url = base + "/open-apis/im/v1/messages?receive_id_type=chat_id"
        j = post(url, payload, token())
        if j.get("code") in _TOKEN_CODES:  # token revoked or expired early: refresh once
            with token_lock:
                cache["token"] = ""
            j = post(url, payload, token())
        if j.get("code") != 0:
            raise LarkError(j.get("code"), j.get("msg", ""))
        return (j.get("data") or {}).get("message_id") or True

    return send


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Preview this bot's health report card (nothing is sent).")
    ap.add_argument("--preview", action="store_true", help="print the card as text")
    ap.add_argument("--json", action="store_true", help="print the card JSON")
    ap.add_argument("--bot", default=os.path.basename(_HERE))
    ap.add_argument("--journal-json", metavar="UNIT", help=argparse.SUPPRESS)  # used by _journal()
    ap.add_argument("--timeout", type=float, default=40.0, help=argparse.SUPPRESS)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if a.journal_json:
        try:
            out = _journal_scan(a.journal_json, a.timeout)
        except Exception as e:
            out = {"error": "%s: %s" % (type(e).__name__, e)}
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
    _cfg["bot"] = a.bot
    _cfg["version_at_boot"] = _git_version()
    c = build_card("Preview (not sent)")
    print(json.dumps(c, ensure_ascii=False, indent=2) if a.json else render_text(c))
