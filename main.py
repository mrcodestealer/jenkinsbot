import gzip
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, jsonify, request


def _load_dotenv(env_path: Path) -> None:
    """Load KEY=VALUE lines from .env (no python-dotenv dependency)."""
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jenkinsbot")

_env_file = Path(__file__).resolve().parent / ".env"
if not _env_file.is_file():
    logger.warning(".env not found at %s", _env_file)
_load_dotenv(_env_file)

_CLI_TESTACCESS = "--testaccess" in sys.argv

app = Flask(__name__)


def _env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value and not _CLI_TESTACCESS:
        raise RuntimeError(f"Missing {name} in .env")
    return value


# 国内飞书 | Lark intl — 全部从 .env 读取
LARK_HOST = _env("LARK_HOST").rstrip("/")
_LARK_RUNTIME_HOST: Optional[str] = None
VERIFICATION_TOKEN = _env("VERIFICATION_TOKEN")
APP_ID = _env("APP_ID")
APP_SECRET = _env("APP_SECRET")
PORT = int(_env("PORT") or "5000")

JENKINS_USER = _env("JENKINS_USER")
JENKINS_PASSWORD = _env("JENKINS_PASSWORD")

VPN_JENKINS_USER = (os.getenv("createvpnid") or "").strip()
VPN_JENKINS_PASSWORD = (os.getenv("createvpnpass") or "").strip()
# Jenkins REST API (consoleText / api/json / artifact) often rejects the web-login password and
# requires the user's **API token** for Basic Auth. Set ``createvpntoken`` to junchen's API token;
# it's preferred over the password for jenkinsbot's REST calls (Playwright web login still uses the
# password on the duty bot side). Falls back to the password when unset.
VPN_JENKINS_TOKEN = (os.getenv("createvpntoken") or "").strip()
_VPN_JENKINS_SECRET = VPN_JENKINS_TOKEN or VPN_JENKINS_PASSWORD


# VPN 任务文件夹（DEVOPS_CP / VPN_CONFIGURATION / VPN_CREATION）——这些 Job 需要
# 用 createvpnid/createvpnpass 这套账号才有 Read 权限，与所在 Jenkins 主机无关。
_VPN_JOB_PATH_RE = re.compile(
    r"(?i)/job/(?:DEVOPS_CP|VPN_CONFIGURATION|VPN_CREATION)\b"
)


_VPN_JENKINS_HOSTS = frozenset(
    {"ose-jenkins.bewen.me", "ose-jenkinsaliyun.bewen.me"}
)
VPN_CREATION_JOB_FOLDER_URL = (
    "https://ose-jenkinsaliyun.bewen.me/job/DEVOPS_CP/job/VPN_CONFIGURATION/job/VPN_CREATION/"
)


def _lark_open_base() -> str:
    """Open Platform API base (feishu.cn or larksuite.com). May switch after WS auto-detect."""
    if _LARK_RUNTIME_HOST:
        return _LARK_RUNTIME_HOST
    return LARK_HOST


def _lark_ws_domain_candidates(lark_mod) -> List[Tuple[str, str]]:
    """(label, https://open.*) try order for WebSocket — fixes 1000040351 domain mismatch."""
    feishu = getattr(lark_mod, "FEISHU_DOMAIN", "https://open.feishu.cn").rstrip("/")
    lark_intl = getattr(lark_mod, "LARK_DOMAIN", "https://open.larksuite.com").rstrip("/")

    explicit = (
        (os.getenv("LARK_WS_DOMAIN") or os.getenv("LARK_OPEN_BASE_URL") or "").strip().rstrip("/")
    )
    if explicit and not explicit.startswith("http"):
        explicit = "https://" + explicit

    ordered: List[str] = []
    if explicit:
        ordered.append(explicit)
    host = (LARK_HOST or "").rstrip("/")
    if host and host not in ordered:
        ordered.append(host)

    domain_name = (os.getenv("LARK_DOMAIN") or "").strip().lower()
    if domain_name in ("lark", "larksuite", "intl", "international"):
        tail = (lark_intl, feishu)
    elif domain_name in ("feishu", "cn", "china"):
        tail = (feishu, lark_intl)
    elif "larksuite.com" in host or "larkoffice.com" in host:
        tail = (lark_intl, feishu)
    elif "feishu.cn" in host:
        tail = (feishu, lark_intl)
    else:
        tail = (feishu, lark_intl)

    for u in tail:
        if u not in ordered:
            ordered.append(u)

    out: List[Tuple[str, str]] = []
    for u in ordered:
        label = "feishu" if "feishu.cn" in u else "lark"
        out.append((label, u))
    return out


def _is_lark_domain_mismatch(exc: BaseException) -> bool:
    s = str(exc)
    return "1000040351" in s or "Incorrect domain name" in s


def _is_vpn_job(job_base: str) -> bool:
    try:
        path = urlparse(job_base).path or ""
    except Exception:
        return False
    return bool(_VPN_JOB_PATH_RE.search(path))


def _normalize_vpn_job_base(job_base: str) -> str:
    """VPN monitoring always targets the Aliyun VPN_CREATION job folder."""
    if _is_vpn_job(job_base):
        return VPN_CREATION_JOB_FOLDER_URL
    return (job_base or "").strip()


def _auth_for(job_base: str):
    """选择 Jenkins 凭据。

    VPN 凭据（createvpnid/createvpnpass）适用于：
      1) 旧主机 ``ose-jenkins.bewen.me``（历史规则），以及
      2) 任意主机上的 VPN 文件夹任务（DEVOPS_CP/VPN_CONFIGURATION/VPN_CREATION）——
         例如已迁到 ``ose-jenkinsaliyun.bewen.me`` 的 VPN_CREATION。
    未配置 VPN 凭据时回退到默认 JENKINS_USER。
    """
    host = (urlparse(job_base).hostname or "").lower()
    if VPN_JENKINS_USER and _VPN_JENKINS_SECRET and (
        host in _VPN_JENKINS_HOSTS or _is_vpn_job(job_base)
    ):
        # Prefer createvpntoken (API token) over createvpnpass for REST Basic Auth.
        return (VPN_JENKINS_USER, _VPN_JENKINS_SECRET)
    return (JENKINS_USER, JENKINS_PASSWORD)


def _auth_candidates_for(job_base: str) -> List[Tuple[str, str]]:
    """Ordered Jenkins REST credentials to try (token before password for VPN jobs)."""
    host = (urlparse(job_base).hostname or "").lower()
    use_vpn = bool(
        VPN_JENKINS_USER
        and (host in _VPN_JENKINS_HOSTS or _is_vpn_job(job_base))
    )
    if not use_vpn:
        return [(JENKINS_USER, JENKINS_PASSWORD)]
    out: List[Tuple[str, str]] = []
    if VPN_JENKINS_USER and VPN_JENKINS_TOKEN:
        out.append((VPN_JENKINS_USER, VPN_JENKINS_TOKEN))
    if VPN_JENKINS_USER and VPN_JENKINS_PASSWORD:
        pair = (VPN_JENKINS_USER, VPN_JENKINS_PASSWORD)
        if pair not in out:
            out.append(pair)
    if not out and VPN_JENKINS_USER and _VPN_JENKINS_SECRET:
        out.append((VPN_JENKINS_USER, _VPN_JENKINS_SECRET))
    return out or [(JENKINS_USER, JENKINS_PASSWORD)]

NOTIFY_CHAT_ID = _env("NOTIFY_CHAT_ID")
# Sole @-mention target for finished / stuck / follow-up messages (OM duty — same default as
# osedutybot ``ose_Duty.TARGET_USER_OPEN_ID``). Override with ``JENKINS_TAG_OPEN_ID`` or ``omduty``.
# @mention target (OM duty). ``NOTIFY_USER_OPEN_ID`` is legacy alias — still supported.
TAG_USER_OPEN_ID = (
    (os.getenv("JENKINS_TAG_OPEN_ID") or "").strip()
    or (os.getenv("omduty") or "").strip()
    or (os.getenv("OMDUTY") or "").strip()
    or (os.getenv("NOTIFY_USER_OPEN_ID") or "").strip()
    or "ou_d7bc33724e2d6ced4050c944c2ca5650"
).strip()
# Duty Bot app id. Used for the HTTP callbacks to osedutybot AND as the @ mention on any
# message carrying a slash command — those are addressed to the bot, never to a person.
DUTY_BOT_OPEN_ID = (os.getenv("DUTY_BOT_OPEN_ID") or "ou_1f6596a9923a2a835918e7e2513595d5").strip()


def _at_mention_card(open_id: str) -> str:
    """``lark_md`` in interactive cards — ``<at id=ou_…></at>``."""
    oid = (open_id or "").strip()
    return f"<at id={oid}></at>" if oid else ""


def _at_mention_text(open_id: str, display: str = "") -> str:
    """Plain ``msg_type=text`` @ mention."""
    oid = (open_id or "").strip()
    if not oid:
        return ""
    label = (display or "").strip() or "duty"
    return f'<at user_id="{oid}">{label}</at>'


def _tag_user_at_card() -> str:
    return _at_mention_card(TAG_USER_OPEN_ID)


def _tag_user_at_text() -> str:
    return _at_mention_text(TAG_USER_OPEN_ID)


def _duty_bot_at_text() -> str:
    """@ mention of the duty **bot** — the recipient of every slash command.

    Distinct from :func:`_tag_user_at_text`, which mentions the OM duty *person* on the
    human-facing done and stuck cards. ``/SuccessProceedNext`` and friends are instructions to
    osedutybot; addressing them to a human read as the bot shouting commands at OM duty, and
    meant the duty bot was never the addressee of its own command."""
    return _at_mention_text(DUTY_BOT_OPEN_ID, "duty bot")

_POLL_RAW = _env("JENKINS_POLL_SECONDS")
try:
    POLL_SECONDS = max(0.3, float(_POLL_RAW))
except ValueError:
    POLL_SECONDS = 1.0

STUCK_SECONDS = int(_env("JENKINS_STUCK_SECONDS") or "600")

# ---- console-log delivery (done-card expand/collapse panel + .log attachment) ------------
# Lark rejects a card whose whole REQUEST BODY exceeds 30 KB. The card JSON is escaped TWICE
# on the way out — ``json.dumps(content_obj)`` in _send_chat_message_result /
# _reply_in_thread_result, and then ``requests(json=body)`` escapes that string again — so a
# newline costs 4 bytes and a CJK char 7. Never size the card dict; size the finished body.
# 28000 is safely under both readings of "30 KB" (30000 and 30720).
_CARD_BODY_LIMIT_BYTES = 28000

# Sanitising a 190 MB console to embed 24 KB of it measured ~3s wall and one extra full copy of
# the log (432 MB peak RSS against a 245 MB floor). The wall time is not the problem — the GIL
# hold is: a single _LOG_FENCE_RE/_LOG_CTRL_RE .sub() over the whole log ran ~1.8s
# UNINTERRUPTIBLE, and Flask serves on threads in this same process (app.run(threaded=True)), so
# it stalled every webhook response and websocket heartbeat for that long. The card path
# therefore takes a window off the END first; measured 0.03s after. Far more than any embed
# budget can use, even before ANSI stripping shrinks it.
_LOG_CARD_SCAN_CHARS = 400_000

# Jenkins consoles carry ANSI colour from shell steps. Feishu renders the escapes as visible
# garbage and they eat the byte budget, so they come out before the log is embedded.
_ANSI_RE = re.compile(
    # CSI — colours and cursor moves. ``:`` is in the parameter class because true colour is
    # sometimes written ESC[38:2:R:G:Bm rather than with semicolons.
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"
    # OSC — window titles, hyperlinks.
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    # Charset designation. ``tput sgr0`` is ESC ( B ESC [ m on every xterm* TERM; without this
    # the ESC is stripped by _LOG_CTRL_RE below and a stray "(B" survives as visible text.
    r"|\x1b[()*+#][0-9A-Za-z]"
    # Two-byte escapes carrying no payload (RIS, NEL, keypad modes, save/restore cursor).
    r"|\x1b[=>78cDEHMZ]"
)
_LOG_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# A log line containing ``` would close our fence early and corrupt the rest of the card.
_LOG_FENCE_RE = re.compile(r"`{3,}")
_LOG_NAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WIN_RESERVED_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_FINISHED_RE = re.compile(
    r"Finished:\s*(SUCCESS|UNSTABLE|FAILURE|ABORTED)\s*$", re.MULTILINE
)
_URL_RE = re.compile(r"https?://[^\s<>'\"{}|\\^`\[\]]+", re.IGNORECASE)
_SUCCESS_INFORM_TIME_RE = re.compile(r"/SuccessInformMeTime\b", re.I)
_SUCCESS_INFORM_RE = re.compile(r"/SuccessInformMe\b", re.I)
_SUCCESS_SEND_VPN_CONF_RE = re.compile(r"/SuccessSendVpnConf\b", re.I)
_VPN_TRAILING_NUM_RE = re.compile(r"(\d+)\s*$")

_watch_meta_lock = threading.Lock()
_watch_meta: Dict[Tuple[str, int], Dict[str, Any]] = {}

# Watchers currently running, keyed ``(job_base, build, mode)``. Guarded by ``_watch_meta_lock``.
#
# Two watchers on one build finish together and each fires the duty notification, so the duty bot
# gets the same ``/SuccessProceedNext`` twice and advances its queue past a segment that has not
# built yet — the failure this file already describes at the websocket dedupe. That dedupe only
# stops a REDELIVERY of one event; two genuinely distinct inform messages for the same build (a
# re-send, a retry, the same link pasted twice) still started two threads, because
# ``_start_jenkins_watch_from_url`` had no such check at all.
#
# ``mode`` is part of the key on purpose. Deduping on ``(job_base, build)`` alone would silently
# drop a watch whose mode differs from the one already running — and dropping an ``inform_time``
# watch means the customer's done-reply email never goes out. Same build AND same mode is a
# genuine duplicate; a different mode is a different job to do.
_active_watches: set = set()

_vpn_find_sessions_lock = threading.Lock()
_vpn_find_sessions: Dict[str, Dict[str, Any]] = {}
_vpn_find_picker_sids: Dict[str, str] = {}

# 最近一次构建探测失败的可读原因（_start_jenkins_watch_from_url 写入，回复时拼接）。
_last_probe_detail: str = ""

# Dedupe Lark event re-deliveries (retries reuse the same event_id) so a retried push
# does not start a second watch / double-reply.
_SEEN_EVENTS_MAX = 2000
_seen_events_lock = threading.Lock()
_seen_events = set()
_seen_events_order = deque()


def _event_seen_already(event_key: str) -> bool:
    if not event_key:
        return False
    with _seen_events_lock:
        if event_key in _seen_events:
            return True
        _seen_events.add(event_key)
        _seen_events_order.append(event_key)
        if len(_seen_events_order) > _SEEN_EVENTS_MAX:
            old = _seen_events_order.popleft()
            _seen_events.discard(old)
        return False


def _get_tenant_access_token() -> Optional[str]:
    if not APP_ID or not APP_SECRET:
        logger.error("APP_ID or APP_SECRET is missing.")
        return None

    token_url = f"{_lark_open_base()}/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": APP_ID, "app_secret": APP_SECRET}

    try:
        resp = requests.post(token_url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Failed to get tenant_access_token: %s", exc)
        return None

    if data.get("code") != 0:
        logger.error("Token API returned error: %s", data)
        return None

    return data.get("tenant_access_token")


def _reply_text(message_id: str, text: str) -> bool:
    token = _get_tenant_access_token()
    if not token:
        return False

    reply_url = f"{_lark_open_base()}/open-apis/im/v1/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"content": json.dumps({"text": text}), "msg_type": "text"}

    try:
        resp = requests.post(reply_url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Failed to reply message: %s", exc)
        return False

    if data.get("code") != 0:
        logger.error("Reply API returned error: %s", data)
        return False

    return True


def _sent_message_id(data: Dict[str, Any]) -> str:
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    return str(payload.get("message_id") or "").strip()


def _send_chat_message_result(
    chat_id: str, msg_type: str, content_obj: Dict[str, Any]
) -> Tuple[bool, str]:
    """Send to ``chat_id``; return ``(ok, new_message_id)``."""
    token = _get_tenant_access_token()
    if not token:
        return False, ""

    url = f"{_lark_open_base()}/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "receive_id": chat_id,
        "msg_type": msg_type,
        "content": json.dumps(content_obj),
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("send_chat_message failed: %s", exc)
        return False, ""
    if data.get("code") != 0:
        logger.error("send_chat_message API error: %s", data)
        return False, ""
    return True, _sent_message_id(data)


def _send_chat_message(
    chat_id: str, msg_type: str, content_obj: Dict[str, Any]
) -> bool:
    return _send_chat_message_result(chat_id, msg_type, content_obj)[0]


def _reply_in_thread_result(
    message_id: str, msg_type: str, content_obj: Dict[str, Any]
) -> Tuple[bool, str]:
    """Reply to ``message_id`` **inside its thread** (reply_in_thread=true). Used for VPN so all
    jenkinsbot output lands under the user's original ``create vpn`` message thread.
    Returns ``(ok, new_message_id)``."""
    mid = (message_id or "").strip()
    if not mid:
        return False, ""
    token = _get_tenant_access_token()
    if not token:
        return False, ""
    url = f"{_lark_open_base()}/open-apis/im/v1/messages/{mid}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "content": json.dumps(content_obj),
        "msg_type": msg_type,
        "reply_in_thread": True,
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("reply_in_thread failed: %s", exc)
        return False, ""
    if data.get("code") != 0:
        logger.error("reply_in_thread API error: %s", data)
        return False, ""
    return True, _sent_message_id(data)


def _reply_in_thread_message(
    message_id: str, msg_type: str, content_obj: Dict[str, Any]
) -> bool:
    return _reply_in_thread_result(message_id, msg_type, content_obj)[0]


def _patch_card_message(message_id: str, card: Dict[str, Any]) -> bool:
    """Update an already-sent interactive card in place (needs ``config.update_multi``)."""
    mid = (message_id or "").strip()
    if not mid:
        return False
    token = _get_tenant_access_token()
    if not token:
        return False
    url = f"{_lark_open_base()}/open-apis/im/v1/messages/{mid}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        resp = requests.patch(
            url, headers=headers, json={"content": json.dumps(card)}, timeout=15
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.warning("patch card failed mid=%s err=%s", mid, exc)
        return False
    if resp.status_code != 200 or data.get("code") != 0:
        logger.warning(
            "patch card API error mid=%s status=%s body=%s",
            mid,
            resp.status_code,
            str(data)[:300],
        )
        return False
    return True


def _add_message_reaction(message_id: str, emoji_type: str = "OK") -> bool:
    mid = (message_id or "").strip()
    if not mid:
        return False
    token = _get_tenant_access_token()
    if not token:
        return False
    url = f"{_lark_open_base()}/open-apis/im/v1/messages/{mid}/reactions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    for et in (emoji_type, "Get", "CheckMark"):
        body = {"reaction_type": {"emoji_type": et}}
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json() if resp.content else {}
        except Exception as exc:
            logger.warning("reaction %s failed: %s", et, exc)
            continue
        if resp.status_code == 200 and data.get("code") == 0:
            return True
    return False


def _event_sender_open_id(event: Dict[str, Any]) -> str:
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sid = sender.get("sender_id")
    if isinstance(sid, dict):
        return (sid.get("open_id") or "").strip()
    return (sender.get("open_id") or "").strip()


def _strip_lark_mentions(text: str) -> str:
    s = re.sub(r"<at[^>]*>.*?</at>", " ", text or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def _vpn_find_session_key(chat_id: str, sender_id: str) -> str:
    cid = (chat_id or "").strip()
    sid = (sender_id or "").strip()
    return f"{cid}:{sid}" if sid else cid


def _emit_message(
    msg_type: str,
    content_obj: Dict[str, Any],
    *,
    chat_id: str,
    reply_message_id: Optional[str] = None,
) -> bool:
    """Thread-reply under ``reply_message_id`` when given, else plain send to ``chat_id``."""
    return _emit_message_result(
        msg_type, content_obj, chat_id=chat_id, reply_message_id=reply_message_id
    )[0]


def _emit_message_result(
    msg_type: str,
    content_obj: Dict[str, Any],
    *,
    chat_id: str,
    reply_message_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """Same as :func:`_emit_message` but also returns the new ``message_id`` (for card patches)."""
    rmid = (reply_message_id or "").strip()
    if rmid:
        ok, mid = _reply_in_thread_result(rmid, msg_type, content_obj)
        if ok:
            return True, mid
    return _send_chat_message_result(chat_id, msg_type, content_obj)


def _jenkins_console_text_url(job_base: str, build: int) -> str:
    return f"{job_base.rstrip('/')}/{build}/consoleText"


def _env_flag(name: str, default: str = "1") -> bool:
    """Optional boolean tunable. ``os.getenv``, never :func:`_env` — ``_env`` raises on a
    missing key outside ``--testaccess``, and a new required key would break every caller
    (including the test files, which seed only a fixed placeholder list)."""
    raw = (os.getenv(name) or default).strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _log_card_max_bytes() -> int:
    """Raw console bytes we are willing to *try* embedding in the done card.

    Only a starting point — the real gate is the measured request-body size, because JSON
    escaping inflates this by ~1.06x for a plain log and up to 2x for a log full of Windows
    paths or embedded JSON."""
    try:
        return max(0, min(24000, int(os.getenv("JENKINS_LOG_CARD_MAX_BYTES", "24000"))))
    except ValueError:
        return 24000


def _log_file_max_bytes() -> int:
    """Lark's per-upload ceiling. Never a truncation point — nothing is ever omitted.

    ``POST /open-apis/im/v1/files`` documents "文件大小不得超过 30 MB" and rejects anything larger
    with HTTP 400 / code 234006. The docs never say whether that MB is decimal or binary, so the
    cap here is 30,000,000 rather than 30 MiB (31,457,280): a 30 MiB payload would otherwise be
    1.4 MB over a decimal ceiling and get rejected at the exact moment the code believed it was
    safe. There is no chunked IM upload and ``msg_type:"file"`` takes nothing but a ``file_key``,
    so this ceiling cannot be worked around — see :func:`_log_single_file_only`."""
    try:
        return max(
            4096, min(30_000_000, int(os.getenv("JENKINS_LOG_FILE_MAX_BYTES", "30000000")))
        )
    except ValueError:
        return 30_000_000


def _log_file_enabled() -> bool:
    return _env_flag("JENKINS_LOG_FILE_ENABLED", "1")


def _log_panel_expanded() -> bool:
    """Open on arrival, so the log is visible without a click. ``0`` starts it collapsed —
    worth trying if a very long panel makes the Feishu client add its own "…more"."""
    return _env_flag("JENKINS_LOG_PANEL_EXPANDED", "1")


def _log_single_file_only() -> bool:
    """ONE attachment or none — never a pile of numbered parts.

    Feishu caps a chat attachment at 30 MB (``im/v1/files``, error 234006) with no chunked
    variant and no URL field on ``msg_type:"file"``, so a 200 MB console can NEVER arrive as one
    plain attachment. Rather than fragment it, the card then states the size and points at
    Jenkins' own ``consoleText`` URL — which is already one link to one complete, unsplit,
    uncompressed log.

    Set ``0`` to allow the multi-part fallback, or ``JENKINS_LOG_GZIP=1`` to get one small
    ``.log.gz`` instead. All three deliver every byte; they differ only in how many things you
    have to click."""
    return _env_flag("JENKINS_LOG_SINGLE_FILE_ONLY", "1")


def _log_gzip_enabled() -> bool:
    """Plain ``.log`` by default — a ``.gz`` has to be unpacked before you can read anything.

    Lark refuses any single upload over 30 MB, so a console bigger than that cannot be one
    plain ``.log`` no matter what: it arrives as ``{pipeline}.partNofM.log``, each part opening
    directly. Set ``JENKINS_LOG_GZIP=1`` to trade that for ONE small ``{pipeline}.log.gz``
    instead (~190 MB compresses to well under a megabyte, so it is far quicker to upload and to
    download). Neither setting omits a byte."""
    return _env_flag("JENKINS_LOG_GZIP", "0")


def _log_panel_code_block() -> bool:
    """Kill switch: no official doc example nests a fenced code block inside a collapsible
    panel. Set ``0`` if it misrenders and the log falls back to plain markdown text."""
    return _env_flag("JENKINS_LOG_PANEL_CODE_BLOCK", "1")


def _console_wanted_for(result: str, *, vpn_mode: bool) -> bool:
    """A **successful** ``vpn_conf`` build wants its ``.conf``, not a build log — its done
    card has deliberately never carried console text, and a second file card in that thread is
    noise. Every other outcome wants the log, including a FAILED vpn build (whose message today
    only says 请检查 console)."""
    return not (vpn_mode and (result or "").upper() == "SUCCESS")


def _console_text_for_card(console_text: str) -> str:
    """Make console output safe to embed: LF-only, no ANSI escapes, no stray control
    characters, and no ``` run that could close our fence early."""
    s = (console_text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _ANSI_RE.sub("", s)
    s = _LOG_CTRL_RE.sub("", s)
    return _LOG_FENCE_RE.sub(lambda m: "'" * len(m.group(0)), s)


def _console_tail_bytes(text: str, max_bytes: int) -> Tuple[str, int]:
    """Last ``max_bytes`` UTF-8 bytes of ``text``, re-aligned to a line boundary.

    Tail rather than head: on a failed build the reason is at the end. Returns
    ``(tail, dropped_bytes)``, where ``dropped_bytes`` is ``0`` when nothing was cut."""
    raw = (text or "").encode("utf-8", errors="replace")
    if max_bytes <= 0:
        return "", len(raw)
    if len(raw) <= max_bytes:
        return text or "", 0
    cut = raw[-max_bytes:].decode("utf-8", errors="ignore")
    # Re-align to a line boundary, but only when it is cheap. When the window opens inside one
    # very long line — a JSON dump, a base64 blob, a minified bundle — skipping to the first
    # newline throws away almost the whole budget and leaves a two-line stub. Past a tenth of
    # the window, keep the mid-line cut instead; the "earlier bytes omitted" marker the callers
    # prepend already tells the reader the first line is partial.
    nl = cut.find("\n")
    if 0 <= nl < max(1, len(cut) // 10) and nl < len(cut) - 1:
        cut = cut[nl + 1 :]
    return cut, len(raw) - len(cut.encode("utf-8"))


def _human_bytes(n: int) -> str:
    """``202590375`` -> ``193.2 MB``. A raw byte count is unreadable at a glance, and whether
    the number is plausible for a given job is the first thing worth noticing about it."""
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _utf8_len(text: str, *, chunk: int = 1 << 20) -> int:
    """UTF-8 byte length of ``text`` without materialising a second copy of a huge log.

    ``str`` slices on code points, so summing the chunks gives the exact same total as
    encoding the whole thing at once — just without the transient 185 MB buffer."""
    s = text or ""
    if len(s) <= chunk:
        return len(s.encode("utf-8", errors="replace"))
    return sum(
        len(s[i : i + chunk].encode("utf-8", errors="replace"))
        for i in range(0, len(s), chunk)
    )


def _safe_log_filename(pipeline: str, build: int) -> str:
    """``{pipeline}.log``, safe for Windows, POSIX and Lark's ``file_name`` field.

    ``ctx["pipeline"]`` is the raw, never-percent-decoded last ``/job/<seg>`` segment
    (:func:`_job_path_segments` -> :func:`_pipeline_and_env_from_segments`) and is the literal
    string ``"unknown"`` when the URL carried no ``/job/`` segment — so decode, scrub, and
    always keep a fallback."""
    stem = unquote(str(pipeline or "")).strip()
    stem = _LOG_NAME_BAD_RE.sub("_", stem)  # after unquote, so %2F -> / -> _
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"_{2,}", "_", stem).strip("._ ")
    if len(stem) > 80:
        stem = stem[:80].rstrip("._ ")
    if stem.upper() in _WIN_RESERVED_NAMES:
        stem = f"{stem}_job"
    if not stem or stem.lower() == "unknown":
        stem = f"console-{build}"
    return f"{stem}.log"


def _card_request_bytes(
    card: Dict[str, Any], *, chat_id: str, reply_message_id: Optional[str] = None
) -> int:
    """Size of the finished HTTP body, built exactly the way the senders build it.

    :func:`_emit_message_result` may use the reply endpoint or the chat endpoint (it falls
    back), so take the larger of the two shapes. Leaving ``ensure_ascii`` at its default on
    both dumps is deliberate — that is what the real senders do, and it is what makes a CJK
    character cost 7 bytes."""
    inner = json.dumps(card)
    sizes = [
        len(
            json.dumps(
                {
                    "receive_id": chat_id or "",
                    "msg_type": "interactive",
                    "content": inner,
                }
            ).encode("utf-8")
        )
    ]
    if (reply_message_id or "").strip():
        sizes.append(
            len(
                json.dumps(
                    {
                        "content": inner,
                        "msg_type": "interactive",
                        "reply_in_thread": True,
                    }
                ).encode("utf-8")
            )
        )
    return max(sizes)


def _console_panel_element(log_body: str, *, title: str) -> Dict[str, Any]:
    """Card JSON 1.0 ``collapsible_panel`` holding the console log — the expand/hide block.

    Stays on card JSON **1.0** on purpose: ``collapsible_panel`` is documented for 1.0 and
    needs client V7.9, a *lower* floor than card JSON 2.0 itself (V7.20), so this is the
    conservative option rather than the risky one.

    The log sits in a ``markdown`` component, **not** a ``div``/``lark_md`` text element —
    fenced code blocks only render in the former."""
    # rstrip so a log ending in a newline does not leave a blank line inside the fence.
    fenced = (log_body or "").rstrip()
    content = f"```\n{fenced}\n```" if _log_panel_code_block() else log_body
    return {
        "tag": "collapsible_panel",
        "expanded": _log_panel_expanded(),
        "header": {
            "title": {"tag": "markdown", "content": title},
            "vertical_align": "center",
            "padding": "4px 0px 4px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        # 1.0 documents the default as 8px and 2.0 as 12px — pin it so it cannot drift.
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": content}],
    }


def _console_last_lines(console_text: str, *, max_lines: int = 10) -> str:
    lines = (console_text or "").replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _send_done_card(
    result: str,
    console_tail: str,
    *,
    pipeline: str,
    environment: str,
    build: int,
    build_url: str,
    console_text_url: str,
    chat_id: Optional[str] = None,
    reply_message_id: Optional[str] = None,
    vpn_mode: bool = False,
    log_file_name: Optional[str] = None,
    log_too_big_bytes: Optional[int] = None,
) -> None:
    """Post the finish card. ``console_tail`` is the FULL console text.

    The console goes into a ``collapsible_panel`` — an expand/hide block — rather than the old
    fixed last-10-lines snippet, so the whole log is reachable inside the card. The card is
    then shrunk down a ladder until the finished request body fits Lark's 30 KB cap, and the
    complete log always also goes out as ``log_file_name`` (see :func:`_send_console_log_file`),
    so a truncated embed is never a lost log."""
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    template = "green"
    if result == "FAILURE":
        template = "red"
    elif result == "UNSTABLE":
        template = "orange"
    elif result == "ABORTED":
        template = "grey"

    at = _tag_user_at_card()
    show_console = _console_wanted_for(result, vpn_mode=vpn_mode)
    if vpn_mode:
        summary = (
            "**done created vpn**" if result == "SUCCESS" else f"**VPN build {result}**"
        )
    else:
        summary = "**done update kindly check**"

    lines = [
        f"{at}\n{summary}\n",
        f"- **Environment：** {environment}",
        f"- **Pipeline：** {pipeline}",
        f"- **Build：** #{build}",
        f"- **状态：** {result}",
        f"- **链接：** {build_url}",
        f"- **Logs :** {console_text_url}",
    ]
    if log_file_name:
        lines.append(f"- **Full log :** {log_file_name} (attached below)")
    elif log_too_big_bytes:
        # Feishu refuses any attachment over 30 MB and offers no chunked upload, so this log
        # cannot be one file here. Say so — but do NOT repeat console_text_url, which the
        # "Logs :" line directly above already carries; two identical links read as a bug.
        # Bytes AND a human size: "202,590,375" is hard to sanity-check at a glance, and
        # whether 193 MB is plausible for this job is the first thing worth noticing.
        lines.append(
            f"- **Full log :** {log_too_big_bytes:,} bytes"
            f"（{_human_bytes(log_too_big_bytes)}）— 超过飞书附件上限 30 MB，无法作为附件发送；"
            f"请用上面的 **Logs** 链接下载完整日志（一个文件，一个字节都没少）。"
        )
    body = "\n".join(lines)

    # Window BEFORE sanitising — see _LOG_CARD_SCAN_CHARS. The panel can only ever hold ~24 KB
    # anyway, and the complete log ships as the attachment.
    raw_console = console_tail or ""
    total_bytes = _utf8_len(raw_console) if show_console else 0
    window = raw_console[-_LOG_CARD_SCAN_CHARS:] if show_console else ""
    clean = _console_text_for_card(window) if show_console else ""
    windowed = len(window) < len(raw_console)

    def _card_for(embed_bytes: int) -> Tuple[Dict[str, Any], int, int]:
        elements: List[Dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}}
        ]
        embedded = 0
        if clean.strip() and embed_bytes > 0:
            try:
                tail, dropped = _console_tail_bytes(clean, embed_bytes)
                if tail.strip():
                    # Only the sanitised-and-sliced text is in hand here, so a byte count for
                    # what is SHOWN cannot be reconciled with the raw total. Report the raw
                    # total (the real size of the log) and say plainly whether this is all of
                    # it — no subtraction the reader could check and find wrong.
                    if windowed or dropped:
                        note = (
                            f" — full log attached as {log_file_name}"
                            if log_file_name
                            else " — see the consoleText link above"
                        )
                        title = f"**Console log** — tail only, {total_bytes} bytes total"
                        tail = f"[… earlier output omitted{note} …]\n{tail}"
                    else:
                        title = f"**Console log** — complete ({total_bytes} bytes)"
                    elements.append(_console_panel_element(tail, title=title))
                    embedded = len(tail.encode("utf-8"))
            except Exception as exc:
                # A panel-building bug must never cost us the done card itself.
                logger.exception("console panel build failed: %s", exc)
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {
                    "tag": "plain_text",
                    "content": f"Jenkins Finished: {result} | {environment} / {pipeline}",
                },
            },
            "elements": elements,
        }
        size = _card_request_bytes(
            card, chat_id=target_chat, reply_message_id=reply_message_id
        )
        return card, size, embedded

    # Shrink ladder. A 500 KB console NEVER produces a failed send: the last rung embeds
    # nothing at all, and the full log goes out as the .log attachment regardless.
    budget = _log_card_max_bytes()
    ladder = sorted(
        {b for b in (budget, 12000, 6000, 3000, 1000, 0) if b <= budget}, reverse=True
    ) or [0]
    for step in ladder:
        card, size, embedded = _card_for(step)
        if size <= _CARD_BODY_LIMIT_BYTES:
            break
    else:
        # Oversized even with no log embedded — the summary itself is pathological (a
        # multi-KB pipeline or environment string). Nothing left to shrink, so send it and
        # let the API answer. The .log is a separate message and is unaffected.
        logger.error(
            "done card is %s bytes with no log embed — sending anyway result=%s pipeline=%s",
            size,
            result,
            pipeline,
        )

    ok = _emit_message(
        "interactive", card, chat_id=target_chat, reply_message_id=reply_message_id
    )
    logger.info(
        "send_done_card interactive ok=%s bytes=%s log_embed=%s/%s result=%s env=%s "
        "pipeline=%s build=%s chat=%s thread=%s",
        ok,
        size,
        embedded,
        total_bytes,
        result,
        environment,
        pipeline,
        build,
        target_chat,
        bool((reply_message_id or "").strip()),
    )


def _send_done_notify(
    result: str,
    *,
    pipeline: str,
    environment: str,
    build: int,
    chat_id: Optional[str] = None,
    reply_message_id: Optional[str] = None,
) -> None:
    """Plain-text follow-up after the done card — no @mention."""
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    when = _format_local_time_hhmm()
    if result == "SUCCESS":
        line = f"Done update at {when}. Kindly check thank you."
    else:
        line = f"Update {result.lower()} at {when}. Kindly check thank you."
    ok = _emit_message(
        "text", {"text": line}, chat_id=target_chat, reply_message_id=reply_message_id
    )
    logger.info(
        "send_done_notify text ok=%s result=%s env=%s pipeline=%s build=%s when=%s",
        ok,
        result,
        environment,
        pipeline,
        build,
        when,
    )


def _send_duty_bot_finish_tag(
    result: str,
    *,
    pipeline: str,
    environment: str,
    build: int,
    build_url: str,
    chat_id: Optional[str] = None,
    reply_message_id: Optional[str] = None,
) -> None:
    """After a Jenkins build finishes, post a message **@-tagging the duty bot** (in the same
    thread/chat as the done card). Informational only — carries no slash command."""
    if not TAG_USER_OPEN_ID:
        logger.warning("TAG_USER_OPEN_ID missing — skip finish @ tag")
        return
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    at = _tag_user_at_text()
    text = (
        f"{at} Jenkins Finished: {result} | {environment} / {pipeline} #{build}\n{build_url}"
    )
    ok = _emit_message(
        "text", {"text": text}, chat_id=target_chat, reply_message_id=reply_message_id
    )
    logger.info(
        "duty-bot finish tag ok=%s result=%s env=%s pipeline=%s build=%s",
        ok, result, environment, pipeline, build,
    )


def _send_stuck_card(
    last_snippet: str,
    *,
    chat_id: Optional[str] = None,
    reply_message_id: Optional[str] = None,
) -> None:
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    at = _tag_user_at_card()
    snippet = (last_snippet or "").strip()[-1200:]
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "Jenkins 日志可能卡住"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"{at}\n日志 **{STUCK_SECONDS}s** 内无变化，可能卡住。\n\n"
                        f"末尾内容：\n```\n{snippet}\n```"
                    ),
                },
            }
        ],
    }
    ok = _emit_message(
        "interactive", card, chat_id=target_chat, reply_message_id=reply_message_id
    )
    logger.info("send_stuck_card ok=%s chat=%s", ok, target_chat)


def _extract_urls(text: str) -> List[str]:
    return _URL_RE.findall(text or "")


def _is_jenkins_job_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return "/job/" in (p.path or "")
    except Exception:
        return False


_BUILD_VIEW_SUFFIX_RE = re.compile(
    r"/(?:console(?:Text|Full)?|pipeline-console|flowGraphTable|"
    r"api/(?:json|xml|python)|display/redirect|"
    r"artifact(?:/.*)?|changes|testReport|parameters|allure)$",
    re.IGNORECASE,
)


def _parse_job_base_and_build(url: str) -> Tuple[str, Optional[int]]:
    """返回 (job_base_url 以 / 结尾, 可选 build 号)。若 URL 未带 build，则 build 为 None。

    支持链接末尾带视图/接口后缀（``/console``、``/consoleText``、``/consoleFull``、
    ``/pipeline-console``、``/api/json``、``/display/redirect`` 等）——会先剥掉再取构建号，
    所以直接粘贴 ``.../1066/console`` 也能解析出 #1066。
    """
    raw = url.strip().rstrip("/")
    # 先去掉构建号后面的视图/接口后缀（可能不止一层，例如 /1066/console）。
    while True:
        stripped = _BUILD_VIEW_SUFFIX_RE.sub("", raw).rstrip("/")
        if stripped == raw:
            break
        raw = stripped
    m = re.search(r"/(\d+)$", raw)
    if m:
        build = int(m.group(1))
        base = raw[: m.start()].rstrip("/") + "/"
        return base, build
    return raw.rstrip("/") + "/", None


def _job_path_segments(job_base: str) -> List[str]:
    """从 job URL 解析 /job/A/job/B/... 中的各段名称。"""
    try:
        path = urlparse(job_base).path or ""
    except Exception:
        return []
    return re.findall(r"/job/([^/]+)", path, re.IGNORECASE)


_ENV_SEGMENT_RE = re.compile(
    r"^(uat|prod|dev|staging|test|sit|preprod|pre-prod|qa)(?:[-_]?\d+)?$",
    re.IGNORECASE,
)
_CONSOLE_ENV_RE = re.compile(
    r"(?:^|\n)\s*Environment\s*[:=]\s*['\"]?([^\s'\"`,]+)",
    re.IGNORECASE | re.MULTILINE,
)


def _pipeline_and_env_from_segments(segments: List[str]) -> Tuple[str, Optional[str]]:
    """pipeline=末级 job 名；environment=路径中像 UAT/uat-2 的文件夹段（若有）。"""
    if not segments:
        return "unknown", None
    pipeline = segments[-1]
    env: Optional[str] = None
    for seg in reversed(segments[:-1]):
        if _ENV_SEGMENT_RE.match(seg) or re.search(r"uat[-_]?\d+", seg, re.I):
            env = seg
            break
    if env is None and len(segments) >= 2:
        env = segments[-2]
    return pipeline, env


def _extract_environment_from_console(text: str) -> Optional[str]:
    m = _CONSOLE_ENV_RE.search(text or "")
    return m.group(1).strip() if m else None


def _fetch_build_environment(
    job_base: str, build: int, auth: Tuple[str, str]
) -> Optional[str]:
    """从构建 api/json 的 ParametersAction 读取 Environment 参数。"""
    url = f"{job_base.rstrip('/')}/{build}/api/json"
    try:
        r = requests.get(url, auth=auth, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as exc:
        logger.warning("build params api failed: %s", exc)
        return None

    for action in data.get("actions") or []:
        if not isinstance(action, dict):
            continue
        params = action.get("parameters")
        if not isinstance(params, list):
            continue
        for p in params:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").strip()
            if name.lower() == "environment":
                val = p.get("value")
                if val is not None and str(val).strip():
                    return str(val).strip()
    return None


def _resolve_job_context(
    job_base: str, build: int, console_text: str, auth: Tuple[str, str]
) -> Dict[str, str]:
    segments = _job_path_segments(job_base)
    pipeline, path_env = _pipeline_and_env_from_segments(segments)
    env = (
        _fetch_build_environment(job_base, build, auth)
        or _extract_environment_from_console(console_text)
        or path_env
        or "—"
    )
    build_url = f"{job_base.rstrip('/')}/{build}/"
    return {
        "pipeline": pipeline,
        "environment": env,
        "build": str(build),
        "build_url": build_url,
        "console_text_url": _jenkins_console_text_url(job_base, build),
    }


def _explicit_build_after_url(full_text: str, url: str) -> Optional[int]:
    """
    支持：URL 后面单独写构建号，例如
    @bot https://jenkins/.../job/Foo/ 680
    """
    if not full_text or not url:
        return None
    candidates = [url]
    if url.endswith("/"):
        candidates.append(url.rstrip("/"))
    else:
        candidates.append(url + "/")

    pos = -1
    matched_len = 0
    for c in candidates:
        p = full_text.find(c)
        if p >= 0:
            pos = p
            matched_len = len(c)
            break
    if pos < 0:
        return None

    tail = full_text[pos + matched_len :].strip()
    m = re.match(r"^(\d{1,8})\b", tail)
    return int(m.group(1)) if m else None


def _jenkins_build_probe(
    job_base: str, build: int, auth: Tuple[str, str]
) -> Tuple[bool, int]:
    """探测历史构建号是否可访问，返回 ``(ok, http_status)``。

    ``http_status`` 为实际 HTTP 状态码；网络异常时为 ``0``。调用方据此区分
    404（确实不存在）与 401/403（鉴权/权限问题），给出可操作的提示。
    """
    url = f"{job_base.rstrip('/')}/{build}/api/json"
    try:
        r = requests.get(url, auth=auth, timeout=20)
        if r.status_code != 200:
            logger.warning(
                "build api/json HTTP %s for #%s url=%s user=%s",
                r.status_code, build, url, (auth[0] if auth else "?"),
            )
        return r.status_code == 200, r.status_code
    except Exception as exc:
        logger.warning("build exists check failed: %s", exc)
        return False, 0


def _resolve_jenkins_auth(
    job_base: str, build: int
) -> Tuple[Tuple[str, str], bool, int]:
    """Try VPN/default credential candidates; return ``(auth, ok, http_status)``."""
    job_base = _normalize_vpn_job_base(job_base)
    last_auth = _auth_for(job_base)
    last_status = 0
    for auth in _auth_candidates_for(job_base):
        ok, status = _jenkins_build_probe(job_base, build, auth)
        last_auth, last_status = auth, status
        if ok:
            return auth, True, status
        if status not in (401, 403):
            break
    return last_auth, False, last_status


def _jenkins_build_exists(
    job_base: str, build: int, auth: Tuple[str, str]
) -> bool:
    """确认历史构建号存在（对应 Build History 里那一次）。"""
    ok, _status = _jenkins_build_probe(job_base, build, auth)
    return ok


def _fetch_console_text(console_url: str, auth: Tuple[str, str]) -> Optional[str]:
    try:
        r = requests.get(console_url, auth=auth, timeout=60)
        if r.status_code != 200:
            logger.warning("consoleText HTTP %s", r.status_code)
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as exc:
        logger.exception("consoleText failed: %s", exc)
        return None


def _format_local_time_hhmm() -> str:
    """Local time as ``HH:MM`` (no date, no AM/PM) — Asia/Singapore when available."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Singapore"))
    except Exception:
        now = datetime.now()
    return now.strftime("%H:%M")


def _format_local_time_pm() -> str:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Singapore"))
    except Exception:
        now = datetime.now()
    hour = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{hour}:{now.minute:02d}{ampm}"


def _send_duty_text(text: str, chat_id: Optional[str] = None) -> bool:
    """Post a duty-bot command into ``chat_id`` (the chat the build was informed from).

    The duty bot resolves a ``/updatemore`` queue **by chat id**, so this must land in the chat
    that started the update, not in ``NOTIFY_CHAT_ID``. Sending it to the duty chat instead left
    the real chat's queue parked at ``waiting_jenkins`` and put the "no active queue" warning in
    front of the wrong people.
    """
    plain = (text or "").strip()
    if not plain:
        logger.warning("empty duty notify text — skip")
        return False
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    # The @ goes to the duty BOT. This used to tag TAG_USER_OPEN_ID — the OM duty person — so the
    # chat showed "@CP OM Duty /SuccessProceedNext": a slash command aimed at a human, while the
    # bot that actually handles it was never mentioned at all.
    if DUTY_BOT_OPEN_ID:
        at = _duty_bot_at_text()
        if _send_chat_message(target_chat, "text", {"text": f"{at} {plain}".strip()}):
            logger.info("duty notify sent (@duty bot) chat=%s: %r", target_chat, plain[:120])
            return True
        logger.warning("duty @tag send failed — retrying plain")
    else:
        logger.warning("DUTY_BOT_OPEN_ID missing — sending duty command without @tag")
    if _send_chat_message(target_chat, "text", {"text": plain}):
        logger.info("duty notify sent (plain) chat=%s: %r", target_chat, plain[:120])
        return True
    return False


def _parse_success_inform_command(text: str) -> Optional[Dict[str, Any]]:
    if not (_SUCCESS_INFORM_RE.search(text or "") or _SUCCESS_INFORM_TIME_RE.search(text or "")):
        return None
    time_mode = bool(_SUCCESS_INFORM_TIME_RE.search(text or ""))
    urls = [u for u in _extract_urls(text) if _is_jenkins_job_url(u)]
    if not urls:
        return None
    url = urls[0]
    job_base, build = _parse_job_base_and_build(url)
    if build is None:
        build = _explicit_build_after_url(text, url)
    if build is None:
        return None
    email_title = ""
    if "|" in text:
        email_title = text.split("|", 1)[1].strip()
    return {
        "mode": "inform_time" if time_mode else "inform",
        "job_base": job_base,
        "build": build,
        "email_title": email_title,
    }


def _duty_reply_update_url() -> str:
    """Duty bot internal endpoint — default same-host when both run on OSE-Tools."""
    url = (os.getenv("DUTY_REPLY_UPDATE_URL") or "").strip()
    if url:
        return url
    host = (os.getenv("DUTY_BOT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = (os.getenv("DUTY_BOT_PORT") or os.getenv("LARKBOT_PORT") or "5000").strip()
    return f"http://{host}:{port}/internal/reply-update-email"


def _duty_updatemore_callback_url() -> str:
    """Duty bot ``/FailedStop`` / ``/SuccessProceedNext`` — HTTP before Lark bot→bot."""
    url = (os.getenv("DUTY_UPDATEMORE_CALLBACK_URL") or "").strip()
    if url:
        return url
    host = (os.getenv("DUTY_BOT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = (os.getenv("DUTY_BOT_PORT") or os.getenv("LARKBOT_PORT") or "5000").strip()
    return f"http://{host}:{port}/internal/updatemore-jenkins-callback"


def _notify_duty_updatemore_callback_http(command: str, chat_id: Optional[str] = None) -> bool:
    """POST to duty bot — reliable when Lark skips bot→bot group delivery.

    ``chat_id`` must be the chat the build was informed from: the duty bot looks its
    ``/updatemore`` queue up by chat id, so a hard-coded ``NOTIFY_CHAT_ID`` made every run
    started in any other chat unresolvable there.
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    url = _duty_updatemore_callback_url()
    token = (os.getenv("DUTY_INTERNAL_TOKEN") or "").strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["X-Duty-Internal-Token"] = token
    payload = {"chat_id": (chat_id or "").strip() or NOTIFY_CHAT_ID, "command": cmd}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=90)
        if resp.status_code in (200, 202):
            try:
                body = resp.json()
            except Exception:
                body = {}
            if body.get("ok"):
                logger.info("duty HTTP updatemore-callback OK url=%s cmd=%r", url, cmd)
                return True
        logger.warning(
            "duty HTTP updatemore-callback failed url=%s status=%s body=%s",
            url,
            resp.status_code,
            (resp.text or "")[:300],
        )
    except Exception as exc:
        logger.warning("duty HTTP updatemore-callback error url=%s err=%s", url, exc)
    return False


def _notify_duty_reply_update_email_http(
    title: str, pipeline: str, when: str, chat_id: Optional[str] = None
) -> bool:
    """POST to duty bot — reliable when Lark skips bot→bot group delivery.

    See :func:`_notify_duty_updatemore_callback_http` for why ``chat_id`` must be the informing
    chat. For this endpoint the stakes are higher: with the wrong chat the duty bot cannot find
    the e-mail batch and the customer reply is never sent.
    """
    url = _duty_reply_update_url()
    token = (os.getenv("DUTY_INTERNAL_TOKEN") or "").strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["X-Duty-Internal-Token"] = token
    payload = {
        "chat_id": (chat_id or "").strip() or NOTIFY_CHAT_ID,
        "email_title": (title or "").strip(),
        "environment": (pipeline or "").strip(),
        "when": (when or "").strip(),
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=90)
        if resp.status_code in (200, 202):
            try:
                body = resp.json()
            except Exception:
                body = {}
            if body.get("ok"):
                logger.info(
                    "duty HTTP reply-update OK url=%s title=%r env=%r when=%r",
                    url,
                    title,
                    pipeline,
                    when,
                )
                return True
        logger.warning(
            "duty HTTP reply-update failed url=%s status=%s body=%s",
            url,
            resp.status_code,
            (resp.text or "")[:300],
        )
    except Exception as exc:
        logger.warning("duty HTTP reply-update error url=%s err=%s", url, exc)
    return False


def _notify_duty_after_inform_watch(
    result: str,
    meta: Dict[str, Any],
    ctx: Dict[str, str],
) -> None:
    # The chat the update was started in — NOT NOTIFY_CHAT_ID. The duty bot indexes its
    # /updatemore queue by chat id, so every channel below has to target this one.
    duty_chat = str((meta or {}).get("chat_id") or "").strip() or NOTIFY_CHAT_ID
    if result != "SUCCESS":
        if _notify_duty_updatemore_callback_http("/FailedStop", duty_chat):
            logger.info("duty bot notified via HTTP for /FailedStop chat=%s", duty_chat)
            return
        if not _send_duty_text("/FailedStop", duty_chat):
            # A dropped /FailedStop is worse than a dropped proceed: the queue stays parked
            # waiting for a build that already failed, so the run neither advances nor stops.
            logger.error(
                "duty notify FAILED for /FailedStop chat=%s — a failed build was not reported, "
                "so an /updatemore queue may sit waiting on it",
                duty_chat,
            )
            _send_chat_message(
                duty_chat,
                "text",
                {"text": "⚠️ Build FAILED and I could not reach the duty bot for `/FailedStop`"},
            )
        return
    if meta.get("mode") == "inform_time":
        title = (meta.get("email_title") or "").strip()
        pipeline = (ctx.get("pipeline") or "").strip()
        when = _format_local_time_pm()
        cmd = f"/replyupdateemail | {title} | {pipeline} | {when}".strip()
        if _notify_duty_reply_update_email_http(title, pipeline, when, duty_chat):
            logger.info("duty bot notified via HTTP for email=%r", title)
            return
        if _send_duty_text(cmd, duty_chat):
            logger.info("duty bot notified via Lark for email=%r", title)
            return
        warn = (
            f"⚠️ Jenkins build SUCCESS but **could not reach duty bot** for email reply.\n"
            f"Run manually: `{cmd}`"
        )
        logger.error("duty notify failed — %s", cmd)
        _send_chat_message(duty_chat, "text", {"text": warn})
    elif meta.get("mode") == "inform":
        if _notify_duty_updatemore_callback_http("/SuccessProceedNext", duty_chat):
            logger.info("duty bot notified via HTTP for /SuccessProceedNext chat=%s", duty_chat)
            return
        # A 409 here is the duty bot saying "no queue of mine claimed this", which is normal for a
        # run that was never part of an /updatemore. Say so at INFO so the Lark attempt below is
        # not read as a malfunction.
        logger.info(
            "duty HTTP did not claim /SuccessProceedNext (chat=%s) — falling back to Lark",
            duty_chat,
        )
        if not _send_duty_text("/SuccessProceedNext", duty_chat):
            # Both sends inside _send_duty_text failed. That used to end here: the only trace was
            # whatever _send_chat_message logged one frame down, and if the warning below ALSO
            # failed to send, the whole notification vanished with no record that it was even
            # attempted. An unreachable duty bot must never be silent.
            logger.error(
                "duty notify FAILED for /SuccessProceedNext chat=%s — Lark send did not go "
                "through; the update queue (if any) will NOT advance on its own",
                duty_chat,
            )
            if not _send_chat_message(
                duty_chat,
                "text",
                {"text": "⚠️ Could not reach duty bot for `/SuccessProceedNext`"},
            ):
                logger.error(
                    "duty notify warning could not be posted either chat=%s — this build's "
                    "completion reached nobody",
                    duty_chat,
                )


def _vpn_trailing_number(location: str) -> str:
    m = _VPN_TRAILING_NUM_RE.search((location or "").strip())
    return m.group(1) if m else ""


def _parse_send_vpn_conf_command(text: str) -> Optional[Dict[str, Any]]:
    """Parse ``/SuccessSendVpnConf <job_url> <build> | <vpn_users> | <vpn_location>``."""
    if not _SUCCESS_SEND_VPN_CONF_RE.search(text or ""):
        return None
    parts = (text or "").split("|")
    head = parts[0]
    vpn_users = parts[1].strip() if len(parts) > 1 else ""
    vpn_location = parts[2].strip() if len(parts) > 2 else ""
    vpn_users = re.sub(r"@\S+", "", vpn_users).strip()
    vpn_location = re.sub(r"@\S+", "", vpn_location).strip()
    urls = [u for u in _extract_urls(head) if _is_jenkins_job_url(u)]
    if not urls:
        return None
    url = urls[0]
    job_base, build = _parse_job_base_and_build(url)
    if build is None:
        build = _explicit_build_after_url(head, url)
    if build is None:
        return None
    if not vpn_users:
        return None
    job_base = _normalize_vpn_job_base(job_base)
    return {
        "mode": "vpn_conf",
        "job_base": job_base,
        "build": build,
        "vpn_users": vpn_users,
        "vpn_location": vpn_location,
    }


def _list_build_artifacts(
    job_base: str, build: int, auth: Tuple[str, str]
) -> List[Tuple[str, str]]:
    """Return ``[(fileName, relativePath), ...]`` from the build's artifacts API."""
    url = (
        f"{job_base.rstrip('/')}/{build}/api/json?tree=artifacts[fileName,relativePath]"
    )
    try:
        r = requests.get(url, auth=auth, timeout=30)
        if r.status_code != 200:
            logger.warning("artifacts api HTTP %s url=%s", r.status_code, url)
            return []
        data = r.json()
    except Exception as exc:
        logger.warning("artifacts api failed: %s", exc)
        return []
    out: List[Tuple[str, str]] = []
    for a in data.get("artifacts") or []:
        if isinstance(a, dict):
            fn = (a.get("fileName") or "").strip()
            rel = (a.get("relativePath") or "").strip()
            if fn and rel:
                out.append((fn, rel))
    return out


def _pick_vpn_conf_artifact(
    artifacts: List[Tuple[str, str]], vpn_users: str, vpn_location: str
) -> Optional[Tuple[str, str]]:
    """Find ``{username}{number}.conf`` (number = trailing digits of location); fall back to
    any ``{username}*.conf`` when the location has no number (ALL / TEST_SERVER) or no exact hit."""
    user = (vpn_users or "").strip()
    ucf = user.casefold()
    num = _vpn_trailing_number(vpn_location)
    if num:
        target = f"{user}{num}.conf".casefold()
        for fn, rel in artifacts:
            if fn.casefold() == target:
                return fn, rel
    for fn, rel in artifacts:
        low = fn.casefold()
        if low.endswith(".conf") and ucf and low.startswith(ucf):
            return fn, rel
    for fn, rel in artifacts:
        low = fn.casefold()
        if low.endswith(".conf") and ucf and ucf in low:
            return fn, rel
    return None


def _download_artifact(
    job_base: str, build: int, rel_path: str, auth: Tuple[str, str], dest_path: str
) -> bool:
    url = f"{job_base.rstrip('/')}/{build}/artifact/{rel_path}"
    try:
        r = requests.get(url, auth=auth, timeout=90)
        if r.status_code != 200:
            logger.warning("artifact download HTTP %s url=%s", r.status_code, url)
            return False
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as exc:
        logger.exception("artifact download failed: %s", exc)
        return False


def _upload_file_lark(
    path: str, file_name: str, *, timeout: int = 120
) -> Optional[str]:
    token = _get_tenant_access_token()
    if not token:
        return None
    url = f"{_lark_open_base()}/open-apis/im/v1/files"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with open(path, "rb") as fh:
            files = {
                "file_type": (None, "stream"),
                "file_name": (None, file_name),
                "file": (file_name, fh, "application/octet-stream"),
            }
            resp = requests.post(url, headers=headers, files=files, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("upload_file_lark failed: %s", exc)
        return None
    if data.get("code") != 0:
        logger.error("upload_file_lark API error: %s", data)
        return None
    return (data.get("data") or {}).get("file_key")


def _console_log_display_name(total_bytes: int, base_name: str, cap: int) -> str:
    """What the card should CALL the attachment.

    Decided from the raw byte count alone, so the card can go out before the (multi-second)
    gzip runs and still name the file the reader will actually receive.
    :func:`_console_log_payloads` derives its names from the same rule, so the two cannot
    drift apart."""
    if total_bytes <= cap:
        return base_name
    if _log_single_file_only() and not _log_gzip_enabled():
        return ""  # nothing will be attached; the card points at the consoleText URL instead
    stem = base_name[:-4] if base_name.lower().endswith(".log") else base_name
    if _log_gzip_enabled():
        return f"{stem}.log.gz"
    # Plain parts are exactly cap-sized, so the count is known without doing the work.
    parts = -(-total_bytes // cap)
    return f"{stem}.partNof{parts}.log"


def _console_log_payloads(
    text: str, *, base_name: str, cap: int
) -> List[Tuple[str, bytes]]:
    """``[(file_name, blob), …]`` carrying the **complete** console log — nothing omitted.

    Lark caps a single upload at 30 MB, which is the only reason this returns a list rather
    than one blob. Tiers, in order of how much the reader has to do:

    1. Fits as-is -> plain ``{pipeline}.log``, openable in one click.
    2. Too big, gzip allowed -> ONE ``{pipeline}.log.gz``. A Jenkins console compresses
       10-30x, so ~190 MB lands well under a megabyte.
    3. Too big for even a gzip (or ``JENKINS_LOG_GZIP=0``) -> numbered parts. Every byte
       still ships; with gzip off the parts are plain ``.log`` files you can open directly.
    """
    raw = (text or "").encode("utf-8", errors="replace")
    if len(raw) <= cap:
        return [(base_name, raw)]
    if _log_single_file_only() and not _log_gzip_enabled():
        # One attachment or none. Splitting is what the caller asked us not to do.
        return []

    stem = base_name[:-4] if base_name.lower().endswith(".log") else base_name

    if not _log_gzip_enabled():
        # Plain parts: each chunk IS the payload, so cap bounds it directly.
        chunks = range(0, len(raw), cap)
        total = len(chunks)
        return [
            (f"{stem}.part{n}of{total}.log", raw[i : i + cap])
            for n, i in enumerate(chunks, 1)
        ]

    gz = gzip.compress(raw, compresslevel=6)
    if len(gz) <= cap:
        return [(f"{stem}.log.gz", gz)]

    # Only reachable for a console in the hundreds of MB that barely compresses. Size chunks by
    # the compression ratio we just measured, NOT by ``cap`` — chunking raw bytes at ``cap``
    # when ``cap`` bounds the COMPRESSED size produces ratio-times more parts than the ceiling
    # needs (a 22x-compressing log gave 23 uploads where 2 would do).
    ratio = max(1.0, len(raw) / max(1, len(gz)))
    chunk = max(cap, int(cap * ratio * 0.9))
    parts: List[bytes] = []
    for _attempt in range(8):
        parts = [
            gzip.compress(raw[i : i + chunk], compresslevel=6)
            for i in range(0, len(raw), chunk)
        ]
        if all(len(p) <= cap for p in parts):
            break
        chunk = max(cap // 2, chunk // 2)
    total = len(parts)
    return [
        (f"{stem}.part{n}of{total}.log.gz", blob)
        for n, blob in enumerate(parts, 1)
    ]


def _send_console_log_file(
    console_text: str,
    *,
    file_name: str,
    chat_id: str,
    reply_message_id: Optional[str] = None,
) -> bool:
    """Upload the **whole** console log and post it as clickable file message(s) in the card's
    own thread. Nothing is ever truncated — see :func:`_console_log_payloads` for how a log
    larger than Lark's per-upload cap is gzipped, and split only if it has to be.

    **Never raises.** This runs after the duty-bot callbacks in
    :func:`_jenkins_watch_worker`, but an escaping exception would still abandon the watcher
    mid-cleanup, and a 185 MB console is exactly when that would happen.

    Uses :func:`_emit_message`, not :func:`_send_file_message`: the latter routes to
    ``_send_chat_message`` (chat_id only), which would drop the attachment into the chat root
    while the done card sits in the thread."""
    try:
        text = console_text or ""
        if not text.strip():
            logger.info("console log attach skipped: empty log file=%s", file_name)
            return False  # Lark rejects an empty upload outright
        payloads = _console_log_payloads(
            text, base_name=file_name, cap=_log_file_max_bytes()
        )
        tmpdir = tempfile.mkdtemp(prefix="jenkinslog_")
        sent = 0
        try:
            for name, blob in payloads:
                if not blob:
                    continue
                dest = os.path.join(tmpdir, name)
                # Binary: the payload is already encoded (and may be gzip), so nothing here
                # may re-encode it or rewrite \n to \r\n on Windows.
                with open(dest, "wb") as fh:
                    fh.write(blob)
                file_key = _upload_file_lark(dest, name, timeout=300)
                if not file_key:
                    logger.error(
                        "console log upload FAILED file=%s bytes=%s chat=%s", name, len(blob), chat_id
                    )
                    continue
                if _emit_message(
                    "file",
                    {"file_key": file_key},
                    chat_id=chat_id,
                    reply_message_id=reply_message_id,
                ):
                    sent += 1
                os.unlink(dest)
            logger.info(
                "console log attached %s/%s part(s) raw=%s file=%s chat=%s thread=%s",
                sent,
                len(payloads),
                len(text),
                payloads[0][0] if payloads else file_name,
                chat_id,
                bool((reply_message_id or "").strip()),
            )
            return sent == len(payloads) and sent > 0
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as exc:
        logger.exception("console log attach failed: %s", exc)
        return False


def _send_file_message(chat_id: str, file_key: str) -> bool:
    return _send_chat_message(chat_id, "file", {"file_key": file_key})


def _handle_vpn_conf_after_success(
    result: str, job_base: str, build: int, meta: Dict[str, Any]
) -> None:
    chat = (meta.get("chat_id") or "").strip() or NOTIFY_CHAT_ID
    rmid = (meta.get("reply_message_id") or "").strip() or None
    vpn_users = (meta.get("vpn_users") or "").strip()
    vpn_location = (meta.get("vpn_location") or "").strip()
    artifact_dir = f"{job_base.rstrip('/')}/{build}/artifact/"

    def _out(msg_type: str, content_obj: Dict[str, Any]) -> bool:
        return _emit_message(msg_type, content_obj, chat_id=chat, reply_message_id=rmid)

    if result != "SUCCESS":
        _out(
            "text",
            {
                "text": (
                    f"⚠️ VPN build #{build} finished {result} — .conf not sent. "
                    f"请检查 console。"
                )
            },
        )
        return

    job_base = _normalize_vpn_job_base(job_base)
    auth = meta.get("_jenkins_auth") or _auth_for(job_base)
    artifacts = _list_build_artifacts(job_base, build, auth)
    if not artifacts:
        _out(
            "text",
            {"text": f"⚠️ VPN build #{build} SUCCESS 但找不到 artifacts：{artifact_dir}"},
        )
        return

    picked = _pick_vpn_conf_artifact(artifacts, vpn_users, vpn_location)
    if not picked:
        names = ", ".join(fn for fn, _ in artifacts[:20])
        _out(
            "text",
            {
                "text": (
                    f"⚠️ 没找到 {vpn_users}（{vpn_location}）对应的 .conf。\n"
                    f"Artifacts: {names}\n{artifact_dir}"
                )
            },
        )
        return

    fn, rel = picked
    tmpdir = tempfile.mkdtemp(prefix="vpnconf_")
    try:
        dest = os.path.join(tmpdir, fn)
        if not _download_artifact(job_base, build, rel, auth, dest):
            _out(
                "text",
                {"text": f"⚠️ 下载 {fn} 失败。手动下载：{artifact_dir}{rel}"},
            )
            return
        file_key = _upload_file_lark(dest, fn)
        if not file_key:
            _out(
                "text",
                {"text": f"⚠️ 上传 {fn} 到飞书失败。手动下载：{artifact_dir}{rel}"},
            )
            return
        _out(
            "text",
            {
                "text": (
                    f"✅ VPN created for {vpn_users} ({vpn_location}) — build #{build} SUCCESS.\n"
                    f"配置文件：{fn}"
                )
            },
        )
        ok = _out("file", {"file_key": file_key})
        logger.info("vpn conf sent ok=%s file=%s chat=%s", ok, fn, chat)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _jenkins_watch_worker(
    job_base: str, build: int, meta: Optional[Dict[str, Any]] = None
) -> None:
    job_base = _normalize_vpn_job_base(job_base)
    auth = (
        (meta or {}).get("_jenkins_auth")
        if isinstance(meta, dict) and meta.get("_jenkins_auth")
        else None
    ) or _auth_for(job_base)
    target_chat = (
        str((meta or {}).get("chat_id") or "").strip()
        if isinstance(meta, dict)
        else ""
    ) or NOTIFY_CHAT_ID
    is_vpn_mode = isinstance(meta, dict) and meta.get("mode") == "vpn_conf"
    reply_mid = (
        str((meta or {}).get("reply_message_id") or "").strip()
        if isinstance(meta, dict)
        else ""
    ) or None
    console_url = f"{job_base.rstrip('/')}/{build}/consoleText"
    logger.info("jenkins watch start job_base=%s build=%s", job_base, build)

    last_text: Optional[str] = None
    unchanged_since = time.monotonic()
    stuck_sent = False

    while True:
        text = _fetch_console_text(console_url, auth)
        now = time.monotonic()

        if text is None:
            time.sleep(POLL_SECONDS)
            continue

        if text != last_text:
            last_text = text
            unchanged_since = now

        fin = _FINISHED_RE.search(text or "")
        if fin:
            result = fin.group(1)
            ctx = _resolve_job_context(job_base, build, text or "", auth)
            logger.info(
                "jenkins finished %s build=%s env=%s pipeline=%s",
                result,
                build,
                ctx["environment"],
                ctx["pipeline"],
            )
            try:
                log_file_name = _safe_log_filename(ctx["pipeline"], build)
            except Exception:
                log_file_name = f"console-{build}.log"
            attach_log = _log_file_enabled() and _console_wanted_for(
                result, vpn_mode=is_vpn_mode
            )
            # What the card CALLS the attachment. Kept separate from log_file_name, which is
            # the base _console_log_payloads derives its own names from — feeding this back in
            # as the base yields "FPMS_PROD_SCRIPT_RUN.log.gz.log.gz".
            #
            # A console over Lark's per-upload ceiling arrives gzipped, and a card promising
            # "FPMS_PROD_SCRIPT_RUN.log" when FPMS_PROD_SCRIPT_RUN.log.gz turns up sends the
            # reader hunting for a file that does not exist.
            log_shown_name = log_file_name
            log_too_big = 0
            if attach_log:
                try:
                    log_bytes = _utf8_len(text or "")
                    log_shown_name = _console_log_display_name(
                        log_bytes, log_file_name, _log_file_max_bytes()
                    )
                    if not log_shown_name:
                        # Over Feishu's 30 MB attachment ceiling with splitting and gzip both
                        # off. Nothing will be uploaded, so do not attempt it and do not
                        # apologise for a failure that never happened — the card carries the
                        # size and the consoleText URL instead.
                        attach_log = False
                        log_too_big = log_bytes
                        logger.info(
                            "console log %s bytes exceeds the %s byte attachment ceiling — "
                            "card points at %s instead",
                            log_bytes,
                            _log_file_max_bytes(),
                            ctx["console_text_url"],
                        )
                except Exception as exc:
                    logger.warning("console log name prediction failed: %s", exc)
            _send_done_card(
                result,
                text or "",
                pipeline=ctx["pipeline"],
                environment=ctx["environment"],
                build=build,
                build_url=ctx["build_url"],
                console_text_url=ctx["console_text_url"],
                chat_id=target_chat,
                reply_message_id=reply_mid,
                vpn_mode=is_vpn_mode,
                log_file_name=log_shown_name if attach_log else None,
                log_too_big_bytes=log_too_big or None,
            )
            # VPN: the threaded card + .conf are enough — skip the plain "Done update…" line.
            if not is_vpn_mode:
                _send_done_notify(
                    result,
                    pipeline=ctx["pipeline"],
                    environment=ctx["environment"],
                    build=build,
                    chat_id=target_chat,
                    reply_message_id=reply_mid,
                )
            # After Jenkins finishes, @-tag the duty bot in the same thread/chat.
            _send_duty_bot_finish_tag(
                result,
                pipeline=ctx["pipeline"],
                environment=ctx["environment"],
                build=build,
                build_url=ctx["build_url"],
                chat_id=target_chat,
                reply_message_id=reply_mid,
            )
            if isinstance(meta, dict) and meta.get("mode") in ("inform", "inform_time"):
                _notify_duty_after_inform_watch(result, meta, ctx)
            if isinstance(meta, dict) and meta.get("mode") == "vpn_conf":
                try:
                    _handle_vpn_conf_after_success(result, job_base, build, meta)
                except Exception as exc:
                    logger.exception("vpn conf handling failed: %s", exc)
            # The COMPLETE console log as a downloadable {pipeline}.log, in the same thread as
            # the card. Deliberately last: a 185 MB console can take minutes to gzip and
            # upload, and the duty bot's /updatemore queue must not wait on that — it has a
            # watchdog, and a queue parked at waiting_jenkins is the incident
            # tests/test_watch_dedupe_and_notify_failures.py was written about.
            if attach_log:
                attached = False
                try:
                    attached = _send_console_log_file(
                        text or "",
                        file_name=log_file_name,
                        chat_id=target_chat,
                        reply_message_id=reply_mid,
                    )
                except Exception as exc:
                    logger.exception("console log attach step failed: %s", exc)
                if not attached:
                    # The card already said "(attached below)". Correct that rather than
                    # leaving the reader waiting for a file that will never arrive.
                    try:
                        _emit_message(
                            "text",
                            {
                                "text": (
                                    f"⚠️ {log_shown_name} 上传失败，完整日志请直接看 console："
                                    f"{ctx['console_text_url']}"
                                )
                            },
                            chat_id=target_chat,
                            reply_message_id=reply_mid,
                        )
                    except Exception as exc:
                        logger.exception("console log failure note failed: %s", exc)
            with _watch_meta_lock:
                _watch_meta.pop((job_base, build), None)
            return

        if (not stuck_sent) and (now - unchanged_since) >= STUCK_SECONDS:
            stuck_sent = True
            tail = (text or "")[-1500:]
            logger.warning("jenkins stuck build=%s", build)
            _send_stuck_card(tail, chat_id=target_chat, reply_message_id=reply_mid)

        time.sleep(POLL_SECONDS)


def _probe_detail_or_default() -> str:
    """返回最近一次探测失败的可读原因；没有则回退到旧的笼统提示。"""
    return _last_probe_detail or "该 Job 下不存在或无权限查看，请核对 Build History 里的号码。"


def _build_probe_detail(http_status: int, job_base: str, auth: Tuple[str, str]) -> str:
    """根据 HTTP 状态码给出可操作的中文提示（区分权限 / 不存在 / 网络）。"""
    host = (urlparse(job_base).hostname or "?")
    user = (auth[0] if auth else "?") or "?"
    if http_status in (401, 403):
        return (
            f"鉴权/权限不足（HTTP {http_status}）。当前用 `{user}` 账号访问 `{host}`，"
            f"但该账号对此 Job 没有 Read 权限或凭据无效。"
            "请确认 jenkinsbot 在该 Jenkins 主机上的账号/Token 及文件夹权限。"
        )
    if http_status == 404:
        return "该 Job 下没有这个构建号（HTTP 404），请核对 Build History 里的号码。"
    if http_status == 0:
        return "网络或 Jenkins 服务器无法访问，请稍后重试或查看 jenkinsbot 日志。"
    return f"检查失败（HTTP {http_status}），请查看 jenkinsbot 日志。"


def _start_jenkins_watch_from_url(
    url: str,
    message_text: str = "",
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[int], str, Optional[str]]:
    """
    仅监控你指定的构建号（URL 末尾 /680/ 或链接后单独写 680）。
    不再使用 lastBuild。
    返回: (status, build, pipeline, path_env_hint)
    status: ok | no_build_number | build_not_found | build_forbidden | build_error
    失败时把可读原因存入模块级 ``_last_probe_detail`` 供调用方拼进回复。
    """
    global _last_probe_detail
    _last_probe_detail = ""
    job_base, build = _parse_job_base_and_build(url)
    job_base = _normalize_vpn_job_base(job_base)
    segments = _job_path_segments(job_base)
    pipeline, path_env = _pipeline_and_env_from_segments(segments)

    if build is None:
        explicit = _explicit_build_after_url(message_text, url)
        if explicit is not None:
            build = explicit
            logger.info("jenkins using explicit build from message: %s", build)

    if build is None:
        logger.error("no build number provided for job %s", job_base)
        return "no_build_number", None, pipeline, path_env

    auth, ok, http_status = _resolve_jenkins_auth(job_base, build)
    if not ok:
        _last_probe_detail = _build_probe_detail(http_status, job_base, auth)
        logger.error(
            "jenkins build #%s probe failed HTTP %s under %s (user=%s)",
            build, http_status, job_base, (auth[0] if auth else "?"),
        )
        status = "build_forbidden" if http_status in (401, 403) else (
            "build_error" if http_status not in (404,) else "build_not_found"
        )
        return status, build, pipeline, path_env

    watch_meta = dict(meta) if isinstance(meta, dict) else {}
    watch_meta["_jenkins_auth"] = auth

    # Claim this (build, mode) BEFORE starting the thread. Registering afterwards — which is what
    # the ``_watch_meta`` write below used to do on its own — leaves a window in which a second
    # inform for the same build sees nothing registered and starts its own watcher.
    watch_key = (job_base, build, str(watch_meta.get("mode") or ""))
    with _watch_meta_lock:
        if watch_key in _active_watches:
            logger.info(
                "jenkins watch already running for %s #%s mode=%r — not starting a second "
                "(a duplicate would fire the duty notification twice and advance its queue "
                "past a segment that has not built)",
                job_base, build, watch_key[2],
            )
            return "ok", build, pipeline, path_env
        _active_watches.add(watch_key)
        if isinstance(meta, dict) and meta.get("mode"):
            _watch_meta[(job_base, build)] = dict(meta)

    def _guarded_watch() -> None:
        # The worker returns from several places inside its poll loop, so the release lives here
        # rather than in its control flow: a watcher that ends any other way (stuck, exception,
        # loop exit) must still free its slot, or that build can never be watched again.
        try:
            _jenkins_watch_worker(job_base, build, meta=watch_meta)
        finally:
            with _watch_meta_lock:
                _active_watches.discard(watch_key)

    t = threading.Thread(
        target=_guarded_watch,
        daemon=True,
        name=f"jenkins-watch-{build}",
    )
    t.start()
    return "ok", build, pipeline, path_env


def _is_event_delivery(payload: Dict[str, Any]) -> bool:
    if payload.get("type") == "event_callback":
        return True
    return payload.get("schema") == "2.0" and isinstance(payload.get("event"), dict)


def _event_type(payload: Dict[str, Any]) -> str:
    if payload.get("schema") == "2.0":
        return (payload.get("header") or {}).get("event_type", "")
    evt = payload.get("event") or {}
    return evt.get("type") or (payload.get("header") or {}).get("event_type", "")


def _extract_text_message(event: Dict[str, Any]) -> str:
    content_raw = event.get("message", {}).get("content", "")
    if not content_raw:
        return ""
    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return ""
    return (content.get("text") or "").strip()


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "jenkinsbot"})


@app.route("/internal/vpn-conf-search", methods=["POST"])
def internal_vpn_conf_search():
    """Duty bot → search old VPN ``.conf`` artifacts on VPN_CREATION (Jenkins REST)."""
    if not _internal_api_token_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    raw_multi = payload.get("queries")
    if isinstance(raw_multi, list):
        queries = _split_find_vpn_queries(" ".join(str(q) for q in raw_multi))
    else:
        queries = _split_find_vpn_queries(str(payload.get("query") or ""))
    if not queries:
        return jsonify({"ok": False, "error": "query required"}), 400
    try:
        max_b = int(payload.get("max_builds") or 0)
    except (TypeError, ValueError):
        max_b = 0
    matches, unmatched, err = search_vpn_conf_files_multi(
        queries, max_builds=max_b if max_b > 0 else None
    )
    if err:
        return jsonify({"ok": False, "error": err, "queries": queries}), 502
    return jsonify(
        {
            "ok": True,
            "query": queries[0],
            "queries": queries,
            "unmatched": unmatched,
            "matches": matches,
            "count": len(matches),
        }
    )


@app.route("/internal/vpn-conf-deliver", methods=["POST"])
def internal_vpn_conf_deliver():
    """Duty bot → download one artifact and send ``.conf`` file into Lark chat."""
    if not _internal_api_token_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    chat_id = str(payload.get("chat_id") or "").strip()
    try:
        build = int(payload.get("build") or 0)
    except (TypeError, ValueError):
        build = 0
    rel = str(payload.get("relative_path") or "").strip()
    fn = str(payload.get("file") or "").strip()
    rmid = str(payload.get("reply_message_id") or "").strip() or None
    jb = str(payload.get("job_base") or "").strip() or None
    ok, msg = deliver_vpn_conf_file(
        chat_id, build, rel, fn, reply_message_id=rmid, job_base=jb
    )
    if ok:
        return jsonify({"ok": True, "file": msg})
    return jsonify({"ok": False, "error": msg}), 502


# ---- chatops deploy: "@bot git pull and restart service" --------------------------------
# This command pulls code and restarts the service, which is remote code execution by any other
# name. Two deliberate guards:
#
#  1. FAIL CLOSED. Nothing runs until DEPLOY_ALLOWED_OPEN_IDS names who may do it. An empty
#     allowlist refuses and explains, rather than trusting whoever happens to be in the chat.
#  2. ANCHORED PATTERN. The whole message (minus @ mentions) must BE the command. A pasted
#     Jenkins console containing "+ git pull origin main" must never deploy production, and a
#     substring match would have done exactly that.
#
# The command itself is fixed: no branch, remote, or flag is ever taken from chat, and nothing
# is run through a shell, so there is no injection surface.
_DEPLOY_CMD_RE = re.compile(
    r"""^\s*/?
        (?:git\s*pull|gitpull|deploy)
        (?:\s+origin)?(?:\s+main)?
        (?:\s*(?:and|&|\+|,|then|然后|并且|并|再)\s*)?
        # \s* not \s+ on the suffix: Chinese does not put spaces between words, so
        # "并重启服务" has to match with no separators at all.
        (?:(?P<restart>restart|重启)(?:\s*(?:the\s+)?(?:service|svc|bot|jenkinsbot|服务))?)?
        \s*[.!?。！]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_deploy_lock = threading.Lock()


def _deploy_repo_dir() -> str:
    """The checkout to pull — the directory this module lives in."""
    return str(Path(__file__).resolve().parent)


def _deploy_service_name() -> str:
    return (os.getenv("JENKINSBOT_SERVICE_NAME") or "jenkinsbot").strip() or "jenkinsbot"


def _deploy_allowed_open_ids() -> set:
    """Who may deploy. Empty set means the feature is off — see the note above."""
    raw = (
        os.getenv("DEPLOY_ALLOWED_OPEN_IDS")
        or os.getenv("DEPLOY_ALLOWED_OPEN_ID")
        or ""
    )
    return {p for p in re.split(r"[,\s;]+", raw) if p}


def _parse_deploy_command(text: str) -> Optional[Dict[str, Any]]:
    """``@bot git pull and restart service`` -> ``{"restart": True}``.

    Returns ``None`` for anything that is not exactly this command. ``git pull`` on its own
    pulls and reports without restarting; the restart happens only when asked for."""
    clean = _strip_lark_mentions(text or "")
    if not clean:
        return None
    m = _DEPLOY_CMD_RE.match(clean)
    if not m:
        return None
    return {"restart": bool(m.group("restart"))}


def _run_cmd(args: List[str], timeout: int) -> Tuple[int, str]:
    """Run ``args`` with no shell; return ``(returncode, combined output)``. Never raises."""
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, f"{args[0]}: not found on this host"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — a deploy reply is better than a dead thread
        return 1, f"{type(exc).__name__}: {exc}"


def _restart_own_service(unit: str) -> None:
    """Ask systemd to restart the unit this process IS.

    ``systemctl restart`` hands the job to PID 1 and ``--no-block`` returns without waiting, so
    the restart survives this process being killed — which is precisely what happens next. If
    systemctl is missing or refuses, fall back to dying with a non-zero status, which both
    ``Restart=on-failure`` and ``Restart=always`` treat as a reason to bring us back."""
    rc, out = _run_cmd(["systemctl", "restart", "--no-block", f"{unit}.service"], 30)
    if rc == 0:
        logger.warning("restart requested for %s.service — this process is going away", unit)
        return
    logger.error(
        "systemctl restart %s failed rc=%s out=%r — exiting non-zero so the unit's Restart= "
        "policy brings us back instead",
        unit,
        rc,
        out[:300],
    )
    # Give the reply above a moment to leave the socket before the process ends.
    threading.Timer(2.0, lambda: os._exit(3)).start()


def _handle_deploy_command(
    opts: Dict[str, Any], *, chat_id: str, message_id: str, sender_id: str
) -> None:
    who = (sender_id or "").strip()
    allowed = _deploy_allowed_open_ids()

    def _out(text: str) -> None:
        if not _reply_in_thread_message(message_id, "text", {"text": text}):
            _send_chat_message(chat_id or NOTIFY_CHAT_ID, "text", {"text": text})

    if not allowed:
        logger.warning("deploy REFUSED — DEPLOY_ALLOWED_OPEN_IDS unset (sender=%s)", who or "?")
        _out(
            "⚠️ 部署命令默认关闭（它会拉代码并重启服务，等同远程执行）。\n"
            "在 .env 加一行再重启一次服务即可启用：\n"
            f"DEPLOY_ALLOWED_OPEN_IDS={who or 'ou_你的open_id'}"
        )
        return
    if who not in allowed:
        logger.warning("deploy DENIED sender=%s not in allowlist", who or "?")
        _out(f"⚠️ 你没有部署权限。你的 open_id：{who or '(未知)'}")
        return

    if not _deploy_lock.acquire(blocking=False):
        _out("⏳ 已有一个部署在进行，忽略这次。")
        return
    try:
        repo = _deploy_repo_dir()
        unit = _deploy_service_name()
        logger.warning("DEPLOY started by %s repo=%s restart=%s", who, repo, opts.get("restart"))

        before_rc, before = _run_cmd(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"], 20
        )
        # --ff-only on purpose: if the server has local commits or edits, fail loudly rather
        # than silently creating a merge commit on a production checkout.
        rc, out = _run_cmd(["git", "-C", repo, "pull", "--ff-only", "origin", "main"], 180)
        _, head = _run_cmd(["git", "-C", repo, "log", "-1", "--pretty=%h %s"], 20)
        ok = rc == 0

        lines = [
            f"{'✅' if ok else '❌'} `git pull --ff-only origin main` — exit {rc}",
            f"- 目录：{repo}",
        ]
        if before_rc == 0 and before:
            lines.append(f"- 之前：{before}")
        if head:
            lines.append(f"- 现在：{head}")
        lines.append("```\n" + ((out or "(no output)")[-1200:]) + "\n```")
        if not ok:
            lines.append("拉取失败，**未重启**。")
        elif opts.get("restart"):
            lines.append(f"正在重启 `{unit}.service` …")
        else:
            lines.append(f"未重启（加上 restart 才会重启，例如 `git pull and restart service`）。")
        _out("\n".join(lines))

        logger.warning("DEPLOY pull rc=%s by=%s head=%r", rc, who, head[:120])
        if ok and opts.get("restart"):
            _restart_own_service(unit)
    finally:
        _deploy_lock.release()


def _process_message_command(
    text: str, message_id: str, event_chat_id: str, sender_id: str = ""
) -> None:
    """Heavy work (build-exists checks, replies, watch start) — runs off the webhook thread so
    Lark gets a fast 200 and does not retry (retries previously caused minutes-long delays)."""
    try:
        chat_id = event_chat_id or NOTIFY_CHAT_ID

        deploy = _parse_deploy_command(text)
        if deploy is not None:
            _handle_deploy_command(
                deploy,
                chat_id=chat_id,
                message_id=(message_id or "").strip(),
                sender_id=sender_id,
            )
            return

        find_qs = _parse_find_vpn_conf_command(text)
        if find_qs is not None:
            _handle_find_vpn_conf_lark(
                chat_id,
                find_qs,
                reply_message_id=(message_id or "").strip(),
                sender_id=sender_id,
            )
            return

        vpn = _parse_send_vpn_conf_command(text)
        if vpn:
            vpn["chat_id"] = event_chat_id or NOTIFY_CHAT_ID
            # Thread all VPN output under the triggering message (which the duty bot already
            # posted inside the user's original ``create vpn`` thread).
            vpn["reply_message_id"] = (message_id or "").strip()
            url = f"{vpn['job_base'].rstrip('/')}/{vpn['build']}/"
            status, build_no, _pipeline, _path_env = _start_jenkins_watch_from_url(
                url, text, meta=vpn
            )
            if status == "ok":
                _reply_in_thread_message(
                    message_id,
                    "text",
                    {
                        "text": (
                            f"已开始监控 VPN 构建 #{build_no}（{vpn.get('vpn_users')} / "
                            f"{vpn.get('vpn_location')}）。Finished: SUCCESS 后会下载 .conf 发到这里。"
                        )
                    },
                )
            elif status == "no_build_number":
                _reply_text(message_id, "VPN 监控失败：缺少构建号。")
            else:
                _reply_text(
                    message_id,
                    f"VPN 构建 #{build_no} 无法监控：{_probe_detail_or_default()}",
                )
            return

        inform = _parse_success_inform_command(text)
        if inform:
            inform["chat_id"] = event_chat_id or NOTIFY_CHAT_ID
            inform["reply_message_id"] = (message_id or "").strip()
            url = f"{inform['job_base'].rstrip('/')}/{inform['build']}/"
            status, build_no, pipeline, _path_env = _start_jenkins_watch_from_url(
                url, text, meta=inform
            )
            if status == "ok":
                mode = inform.get("mode") or "inform"
                _reply_in_thread_message(
                    message_id,
                    "text",
                    {
                        "text": (
                            f"已开始监控 Jenkins #{build_no}（{mode}）— Pipeline：{pipeline}。"
                            "完成后会在此线程通知。"
                        )
                    },
                )
            elif status == "no_build_number":
                _reply_text(message_id, "请指定构建号（链接末尾 /680/ 或链接后空格写 680）。")
            else:
                _reply_text(
                    message_id,
                    f"构建 #{build_no} 无法监控：{_probe_detail_or_default()}",
                )
            return

        jenkins_urls = [u for u in _extract_urls(text) if _is_jenkins_job_url(u)]
        if jenkins_urls:
            url = jenkins_urls[0]
            status, build_no, pipeline, path_env = _start_jenkins_watch_from_url(
                url,
                text,
                meta={
                    "mode": "watch",
                    "chat_id": event_chat_id or NOTIFY_CHAT_ID,
                    "reply_message_id": (message_id or "").strip(),
                },
            )
            if status == "ok":
                _poll_hint = (
                    str(int(POLL_SECONDS))
                    if float(POLL_SECONDS).is_integer()
                    else str(POLL_SECONDS)
                )
                env_hint = f"，Environment（路径推测）：{path_env}" if path_env else ""
                _reply_in_thread_message(
                    message_id,
                    "text",
                    {
                        "text": (
                            f"已开始后台监控 Jenkins #{build_no}（Pipeline：{pipeline}{env_hint}）的 "
                            f"console（约每 {_poll_hint}s 拉取完整日志，尽快检测 Finished）。"
                            "结束后会在此线程通知 Environment / Pipeline / 状态。"
                        )
                    },
                )
            elif status == "no_build_number":
                _reply_text(
                    message_id,
                    "请指定历史构建号：链接末尾带 /680/，或在链接后空格写 680。"
                    "不再自动使用最新一次构建（last build）。",
                )
            else:
                _reply_text(
                    message_id,
                    f"构建 #{build_no} 无法监控：{_probe_detail_or_default()}",
                )
            return

        logger.info("no command in message_id=%s", message_id)
    except Exception as exc:
        logger.exception("process message failed (message_id=%s): %s", message_id, exc)


@app.route("/webhook/event", methods=["POST", "GET"])
def webhook_event():
    if request.method == "GET":
        return jsonify({"ok": True, "service": "jenkinsbot"})
    payload = request.get_json(silent=True) or {}
    incoming_token = payload.get("token") or payload.get("header", {}).get("token")

    if incoming_token and incoming_token != VERIFICATION_TOKEN:
        logger.warning("Invalid verification token.")
        return jsonify({"ok": False, "error": "invalid_verification_token"}), 403

    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")})

    if not _is_event_delivery(payload):
        logger.info(
            "ignored webhook: not event delivery keys=%s",
            list(payload.keys())[:20],
        )
        return jsonify({"ok": True, "ignored": "not_event_callback"})

    event_type = _event_type(payload)
    if event_type in ("card.action.trigger", "card.action.trigger_v1"):
        event_key = (
            (payload.get("header") or {}).get("event_id")
            or payload.get("uuid")
            or ""
        )
        if event_key and _event_seen_already(str(event_key)):
            return jsonify({})
        threading.Thread(
            target=_process_card_action_payload,
            args=(payload,),
            daemon=True,
            name="jenkinsbot-card",
        ).start()
        return jsonify({})

    if event_type != "im.message.receive_v1":
        logger.info("ignored event_type=%s", event_type or "unknown")
        return jsonify({"ok": True, "ignored": event_type or "unknown"})

    event = payload.get("event", {})
    message = event.get("message", {})
    message_id = message.get("message_id", "")
    message_type = message.get("message_type", "")
    event_chat_id = (message.get("chat_id") or "").strip()

    if message_type != "text" or not message_id:
        return jsonify({"ok": True, "ignored": "non_text_or_missing_message_id"})

    text = _extract_text_message(event)
    logger.info(
        "im.message.receive_v1 message_id=%s chat_type=%s text=%r",
        message_id,
        event.get("message", {}).get("chat_type"),
        text[:300] if text else text,
    )

    # Dedupe Lark retries (same event_id) so a retried push doesn't double-watch / double-reply.
    event_key = (
        (payload.get("header") or {}).get("event_id")
        or payload.get("uuid")
        or message_id
    )
    if _event_seen_already(str(event_key)):
        logger.info("duplicate event skipped key=%s message_id=%s", event_key, message_id)
        return jsonify({"ok": True, "ignored": "duplicate"})

    # ACK Lark immediately; do build-exists check + reply + watch start off-thread.
    # (Previously these blocking calls ran before the 200, so a slow Jenkins/token response
    #  delayed the ACK and Lark retried with backoff — causing minutes-long delays.)
    threading.Thread(
        target=_process_message_command,
        args=(text, message_id, event_chat_id, _event_sender_open_id(event)),
        daemon=True,
        name=f"jenkinsbot-msg-{(message_id or '')[:12]}",
    ).start()
    return jsonify({"ok": True, "queued": True})


def _mask_secret(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return "(not set)"
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}…{s[-2:]} (len={len(s)})"


def _jenkins_rest_get(
    url: str, auth: Tuple[str, str]
) -> Tuple[bool, int, Optional[Dict[str, Any]], str]:
    """GET Jenkins REST URL; return ``(ok, http_status, json_or_none, err_hint)``."""
    try:
        r = requests.get(url, auth=auth, timeout=25)
    except Exception as exc:
        return False, 0, None, str(exc)[:200]
    if r.status_code != 200:
        hint = _build_probe_detail(r.status_code, url, auth)
        return False, r.status_code, None, hint
    try:
        return True, r.status_code, r.json(), ""
    except Exception as exc:
        return False, r.status_code, None, f"HTTP 200 but JSON parse failed: {exc}"


def _internal_api_token_ok(req) -> bool:
    need = (os.getenv("JENKINS_INTERNAL_TOKEN") or os.getenv("DUTY_INTERNAL_TOKEN") or "").strip()
    if not need:
        return True
    got = (req.headers.get("X-Duty-Internal-Token") or req.headers.get("X-Jenkins-Internal-Token") or "").strip()
    return got == need


def _jenkins_site_label(job_base: str) -> str:
    """``https://host`` origin — what users see in "is not accessible" replies."""
    parsed = urlparse(job_base or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return (job_base or "").strip() or "Jenkins"


def _resolve_vpn_creation_auth_detail() -> Tuple[Optional[Tuple[str, Tuple[str, str]]], str]:
    """Working VPN_CREATION auth plus a user-facing error when there is none.

    Separates *site unreachable* (DNS/TCP/TLS/timeout — ``_jenkins_rest_get`` reports status 0)
    from *site reachable but rejected us* (HTTP 401/403/404), so the chat reply can name which.
    """
    job_base = VPN_CREATION_JOB_FOLDER_URL.rstrip("/") + "/"
    job_api = f"{job_base.rstrip('/')}/api/json?tree=lastBuild[number]"
    site = _jenkins_site_label(job_base)
    candidates = [(u, p) for u, p in _auth_candidates_for(job_base) if u and p]
    if not candidates:
        return None, (
            f"❌ No Jenkins credentials for `{site}` — set `createvpnid` / `createvpnpass` in `.env`."
        )

    unreachable = ""
    http_status = 0
    http_hint = ""
    for auth in candidates:
        ok, status, data, hint = _jenkins_rest_get(job_api, auth)
        if ok and isinstance(data, dict):
            return (job_base, auth), ""
        if status == 0:
            unreachable = unreachable or (hint or "connection failed")
        else:
            http_status = status
            http_hint = hint

    if unreachable and not http_status:
        return None, f"❌ `{site}` is not accessible — {unreachable}"
    return None, (
        f"❌ `{site}` rejected the request (HTTP {http_status})"
        + (f" — {http_hint}" if http_hint else "")
    )


def _resolve_vpn_creation_auth() -> Optional[Tuple[str, Tuple[str, str]]]:
    """Working Jenkins REST auth for VPN_CREATION (same host/credentials as create-vpn flow)."""
    return _resolve_vpn_creation_auth_detail()[0]


def _vpn_find_max_builds() -> int:
    try:
        return max(5, min(300, int(os.getenv("VPN_FIND_MAX_BUILDS", "100"))))
    except ValueError:
        return 100


def _list_success_build_numbers(
    job_base: str, auth: Tuple[str, str], *, limit: int
) -> List[int]:
    """Recent build numbers (newest first), SUCCESS only."""
    url = (
        f"{job_base.rstrip('/')}/api/json"
        f"?tree=builds[number,result]{{0,{limit}}}"
    )
    ok, _status, data, _hint = _jenkins_rest_get(url, auth)
    if not ok or not isinstance(data, dict):
        return []
    out: List[int] = []
    for b in data.get("builds") or []:
        if not isinstance(b, dict):
            continue
        if (b.get("result") or "").strip().upper() != "SUCCESS":
            continue
        try:
            n = int(b.get("number"))
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    out.sort(reverse=True)
    return out


def search_vpn_conf_files_multi(
    queries: List[str], *, max_builds: int | None = None
) -> Tuple[List[Dict[str, Any]], List[str], str]:
    """
    Search recent VPN_CREATION SUCCESS builds for ``.conf`` artifacts whose **filename stem**
    contains **any** of ``queries`` (case-insensitive). Newest build wins per distinct filename.

    Returns ``(rows, unmatched_queries, error)``. ``error`` is a ready-to-send chat string when
    the Jenkins site is unreachable or rejects us — ``rows`` is empty in that case, so callers
    can tell "site down" apart from "searched fine, found nothing". Each row gains ``matched``:
    the queries that hit that filename.
    """
    qs: List[str] = []
    seen: set = set()
    for raw in queries or []:
        q = str(raw or "").strip()
        if not q or q.casefold() in seen:
            continue
        seen.add(q.casefold())
        qs.append(q)
    if not qs:
        return [], [], ""

    pairs = [(q, q.casefold()) for q in qs]
    cap = max_builds if max_builds is not None else _vpn_find_max_builds()
    resolved, err = _resolve_vpn_creation_auth_detail()
    if not resolved:
        return [], qs, err
    job_base, auth = resolved
    builds = _list_success_build_numbers(job_base, auth, limit=cap)
    if not builds:
        site = _jenkins_site_label(job_base)
        return [], qs, (
            f"❌ `{site}` is not accessible — could not list VPN_CREATION builds."
        )

    best: Dict[str, Dict[str, Any]] = {}
    hit: set = set()
    for build in builds:
        for fn, rel in _list_build_artifacts(job_base, build, auth):
            if not fn.casefold().endswith(".conf"):
                continue
            stem = fn[:-5].casefold()
            matched = [q for q, qcf in pairs if qcf in stem]
            if not matched:
                continue
            hit.update(q.casefold() for q in matched)
            prev = best.get(fn)
            if prev is None or build > int(prev.get("build") or 0):
                artifact_url = f"{job_base.rstrip('/')}/{build}/artifact/{rel}"
                best[fn] = {
                    "file": fn,
                    "relative_path": rel,
                    "build": build,
                    "job_base": job_base,
                    "artifact_url": artifact_url,
                    "matched": matched,
                }
    rows = sorted(best.values(), key=lambda r: (r["file"].casefold(), -int(r["build"])))
    unmatched = [q for q, qcf in pairs if qcf not in hit]
    return rows, unmatched, ""


def search_vpn_conf_files(query: str, *, max_builds: int | None = None) -> List[Dict[str, Any]]:
    """Single-query form of :func:`search_vpn_conf_files_multi` (kept for existing callers)."""
    rows, _unmatched, _err = search_vpn_conf_files_multi([query], max_builds=max_builds)
    return rows


def deliver_vpn_conf_file(
    chat_id: str,
    build: int,
    relative_path: str,
    file_name: str,
    *,
    reply_message_id: Optional[str] = None,
    job_base: Optional[str] = None,
) -> Tuple[bool, str]:
    """Download one VPN ``.conf`` from Jenkins and send it into Lark ``chat_id``."""
    cid = (chat_id or "").strip()
    fn = (file_name or "").strip()
    rel = (relative_path or "").strip()
    if not cid or not fn or not rel or build < 1:
        return False, "missing chat_id, build, or artifact path"
    resolved = _resolve_vpn_creation_auth()
    if not resolved:
        return False, "Jenkins VPN_CREATION auth failed (check createvpnid/token)"
    jb = (job_base or resolved[0]).rstrip("/") + "/"
    auth = resolved[1]
    tmpdir = tempfile.mkdtemp(prefix="vpnfind_")
    try:
        dest = os.path.join(tmpdir, fn)
        if not _download_artifact(jb, build, rel, auth, dest):
            return False, f"download failed: {jb}{build}/artifact/{rel}"
        fkey = _upload_file_lark(dest, fn)
        if not fkey:
            return False, f"upload to Lark failed for {fn}"
        ok = _emit_message(
            "file",
            {"file_key": fkey},
            chat_id=cid,
            reply_message_id=reply_message_id,
        )
        if not ok:
            return False, "send file message to chat failed"
        return True, fn
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _vpn_find_callback_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in payload.items():
        ks = str(key)
        if isinstance(val, (dict, list)):
            out[ks] = val
        elif val is None:
            out[ks] = ""
        else:
            out[ks] = str(val)
    return out


def _vpn_find_callback_button(
    label: str,
    btn_type: str,
    payload: Dict[str, Any],
    *,
    element_id: Optional[str] = None,
) -> Dict[str, Any]:
    btn: Dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "behaviors": [{"type": "callback", "value": _vpn_find_callback_payload(payload)}],
    }
    eid = (element_id or "").strip()
    if eid:
        btn["element_id"] = eid
    return btn


def _vpn_find_button_row(buttons: List[Dict[str, Any]]) -> Dict[str, Any]:
    columns: List[Dict[str, Any]] = []
    for btn in buttons:
        columns.append(
            {
                "tag": "column",
                "width": "auto",
                "weight": 1,
                "vertical_align": "top",
                "elements": [btn],
            }
        )
    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "columns": columns,
    }


def _vpn_find_pick_card(
    matches: List[Dict[str, Any]],
    queries: Any,
    *,
    picker_sid: str,
    sent: Optional[List[int]] = None,
    unmatched: Optional[List[str]] = None,
    total: Optional[int] = None,
) -> Dict[str, Any]:
    """Picker card listing every match as its own button. Buttons stay live so the user can tap
    several; already-sent ones are re-rendered with ✅ when the card is patched."""
    qs = [queries] if isinstance(queries, str) else [str(q) for q in (queries or [])]
    done = {int(i) for i in (sent or [])}
    cap = len(matches)
    buttons: List[Dict[str, Any]] = []
    for i in range(cap):
        fn = str(matches[i].get("file") or "?")
        picked = (i + 1) in done
        prefix = "✅" if picked else f"{i + 1}."
        buttons.append(
            _vpn_find_callback_button(
                f"{prefix} {fn}"[:60],
                "default" if (picked or i) else "primary",
                {"k": "vpn_find", "i": i + 1, "sid": picker_sid},
                element_id=f"vpnf{i}"[:20],
            )
        )

    shown = ", ".join(f"**`{q}`**" for q in qs) or "your search"
    head = f"🔍 **{cap}** VPN `.conf` files match {shown}"
    if total and int(total) > cap:
        head += f" (showing newest {cap} of {int(total)})"
    head += " — tap a file to download; you can tap more than one:"

    body_elements: List[Dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": head}}
    ]
    for off in range(0, len(buttons), 3):
        body_elements.append(_vpn_find_button_row(buttons[off : off + 3]))
    if unmatched:
        body_elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "⚠️ No `.conf` found for: "
                    + ", ".join(f"`{q}`" for q in unmatched),
                },
            }
        )
    if done:
        body_elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"✅ Sent **{len(done)}** of **{cap}**.",
                },
            }
        )
    body_elements.append({"tag": "hr"})
    body_elements.append(
        _vpn_find_button_row(
            [
                _vpn_find_callback_button(
                    "Done",
                    "primary" if done else "default",
                    {"k": "vpn_find_done", "sid": picker_sid},
                    element_id="vpn_find_done",
                ),
                _vpn_find_callback_button(
                    "Cancel",
                    "default",
                    {"k": "vpn_find_cancel", "sid": picker_sid},
                    element_id="vpn_find_can",
                ),
            ]
        )
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "body": {"elements": body_elements},
    }


def _vpn_find_max_buttons() -> int:
    try:
        return max(1, min(40, int(os.getenv("VPN_FIND_MAX_BUTTONS", "20"))))
    except ValueError:
        return 20


def _vpn_find_session_ttl() -> int:
    try:
        return max(60, min(86400, int(os.getenv("VPN_FIND_SESSION_TTL_SEC", "1800"))))
    except ValueError:
        return 1800


def _vpn_find_prune_locked() -> None:
    """Drop timed-out picker sessions and orphaned sids. Caller holds the sessions lock.

    Needed because a picker now survives past the first pick (multi-select), so nothing else
    would ever remove a session the user neither finishes nor cancels."""
    ttl = _vpn_find_session_ttl()
    now = time.time()
    for key, sess in list(_vpn_find_sessions.items()):
        created = float((sess or {}).get("created_at") or 0)
        if created and now - created > ttl:
            _vpn_find_sessions.pop(key, None)
    for psid, key in list(_vpn_find_picker_sids.items()):
        if key not in _vpn_find_sessions:
            _vpn_find_picker_sids.pop(psid, None)


def _vpn_find_register_picker(picker_sid: str, session_key: str) -> None:
    with _vpn_find_sessions_lock:
        _vpn_find_picker_sids[picker_sid] = session_key


def _vpn_find_thread_root(sess: Optional[Dict[str, Any]]) -> str:
    if not isinstance(sess, dict):
        return ""
    return str(sess.get("thread_root_id") or "").strip()


def _vpn_find_deliver_row(chat_id: str, row: Dict[str, Any], thread_root_id: str) -> None:
    fn = str(row.get("file") or "?")
    root = (thread_root_id or "").strip()
    if root:
        _emit_message(
            "text",
            {"text": f"📤 Sending `{fn}` — kindly wait…"},
            chat_id=chat_id,
            reply_message_id=root,
        )
    ok, msg = deliver_vpn_conf_file(
        chat_id,
        int(row["build"]),
        str(row["relative_path"]),
        str(row["file"]),
        reply_message_id=root or None,
        job_base=str(row.get("job_base") or ""),
    )
    if ok:
        _emit_message(
            "text",
            {"text": f"✅ Sent `{msg}`."},
            chat_id=chat_id,
            reply_message_id=root or None,
        )
    else:
        _emit_message(
            "text",
            {"text": f"❌ Deliver failed: {msg}"},
            chat_id=chat_id,
            reply_message_id=root or None,
        )


def _normalize_card_action_value(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_card_action_fields(payload: Dict[str, Any]) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    value = _normalize_card_action_value(action.get("value"))
    if not value:
        return None

    operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
    op_ids = operator.get("operator_id")
    sender_id = ""
    if isinstance(op_ids, dict):
        sender_id = (op_ids.get("open_id") or "").strip()

    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    chat_id = (
        (context.get("open_chat_id") or context.get("chat_id") or event.get("open_chat_id") or "")
        .strip()
    )
    if not chat_id:
        return None
    return chat_id, sender_id, value


def _process_card_action_payload(payload: Dict[str, Any]) -> None:
    try:
        fields = _extract_card_action_fields(payload)
        if not fields:
            logger.info("card.action ignored: could not parse fields")
            return
        chat_id, sender_id, value = fields
        k = str(value.get("k") or "").strip().lower()
        sid = str(value.get("sid") or "").strip()

        session_key = ""
        with _vpn_find_sessions_lock:
            _vpn_find_prune_locked()
            if sid:
                session_key = (_vpn_find_picker_sids.get(sid) or "").strip()
            if not session_key:
                session_key = _vpn_find_session_key(chat_id, sender_id)
            sess = _vpn_find_sessions.get(session_key)
        thread_root = _vpn_find_thread_root(sess)

        if k in ("vpn_find_cancel", "vpn_find_done"):
            with _vpn_find_sessions_lock:
                closed = _vpn_find_sessions.pop(session_key, None)
                if sid:
                    _vpn_find_picker_sids.pop(sid, None)
            thread_root = _vpn_find_thread_root(closed) or thread_root
            n_sent = len(list((closed or {}).get("sent") or []))
            if k == "vpn_find_done":
                note = f"✅ VPN file search closed — {n_sent} file(s) sent."
            else:
                note = "VPN file search cancelled."
            if thread_root:
                _emit_message(
                    "text",
                    {"text": note},
                    chat_id=chat_id,
                    reply_message_id=thread_root,
                )
            return

        if k != "vpn_find":
            return

        try:
            idx = int(str(value.get("i")).strip())
        except (TypeError, ValueError):
            return

        # Claim the pick under the lock so a double-tap (Lark can deliver the same action twice,
        # and each action runs on its own thread) cannot download and send the same file twice.
        state = ""
        row: Optional[Dict[str, Any]] = None
        dup_file = ""
        card_mid = ""
        card: Optional[Dict[str, Any]] = None
        with _vpn_find_sessions_lock:
            sess = _vpn_find_sessions.get(session_key)
            stale_sid = bool(sid) and str((sess or {}).get("picker_sid") or "") != sid
            if not isinstance(sess, dict) or sess.get("state") != "vpn_find_pick" or stale_sid:
                state = "expired"
            else:
                thread_root = _vpn_find_thread_root(sess) or thread_root
                candidates = list(sess.get("candidates") or [])
                sent = [int(x) for x in (sess.get("sent") or [])]
                if idx < 1 or idx > len(candidates):
                    state = "invalid"
                elif idx in sent:
                    state = "duplicate"
                    dup_file = str(candidates[idx - 1].get("file") or "?")
                else:
                    sent.append(idx)
                    sess["sent"] = sent
                    row = candidates[idx - 1]
                    state = "ok"
                    card_mid = str(sess.get("card_message_id") or "").strip()
                    card = _vpn_find_pick_card(
                        candidates,
                        list(sess.get("queries") or []),
                        picker_sid=str(sess.get("picker_sid") or ""),
                        sent=list(sent),
                        unmatched=list(sess.get("unmatched") or []),
                        total=int(sess.get("total") or len(candidates)),
                    )

        if state == "expired":
            if thread_root:
                _emit_message(
                    "text",
                    {"text": "⚠️ VPN find session expired — search again."},
                    chat_id=chat_id,
                    reply_message_id=thread_root,
                )
            return
        if state == "invalid":
            if thread_root:
                _emit_message(
                    "text",
                    {"text": "⚠️ Invalid pick — try again or **Cancel**."},
                    chat_id=chat_id,
                    reply_message_id=thread_root,
                )
            return
        if state == "duplicate":
            if thread_root:
                _emit_message(
                    "text",
                    {"text": f"ℹ️ `{dup_file}` was already sent — pick another or **Done**."},
                    chat_id=chat_id,
                    reply_message_id=thread_root,
                )
            return
        if not row:
            return

        # Tick the button before the (slow) download+upload so the user sees the tap landed.
        if card_mid and card:
            _patch_card_message(card_mid, card)
        _vpn_find_deliver_row(chat_id, row, thread_root)
    except Exception as exc:
        logger.exception("card.action failed: %s", exc)


_FIND_VPN_MAX_QUERIES = 10

# Split on comma / full-width comma / ideographic comma / semicolon / slash / pipe / whitespace,
# so ``alex,bob``, ``{alex}, {bob}``, ``alex；bob`` and ``alex bob`` all mean the same thing.
_FIND_VPN_SPLIT_RE = re.compile(r"[,，、;；/|\s]+")

# Words that are part of the command phrasing, never a username.
_FIND_VPN_STOPWORDS = frozenset(
    {"vpn", "conf", "confs", "config", "configs", "file", "files", "for", "and", "the"}
)


def _split_find_vpn_queries(raw: str) -> List[str]:
    """``alex, bob`` / ``{alex},{bob}`` / ``alex bob`` → ``["alex", "bob"]`` (order kept, deduped)."""
    out: List[str] = []
    seen: set = set()
    for part in _FIND_VPN_SPLIT_RE.split(raw or ""):
        tok = part.strip().strip("{}[]()<>\"'`").strip(":,.").strip()
        if not tok:
            continue
        key = tok.casefold()
        if key in _FIND_VPN_STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(tok)
        if len(out) >= _FIND_VPN_MAX_QUERIES:
            break
    return out


def _parse_find_vpn_conf_command(text: str) -> Optional[List[str]]:
    """``/FindVpnConf alex,bob`` or ``find vpn file alex, bob`` → ``["alex", "bob"]``.

    ``None`` means "not this command"; ``[]`` means the command was used with no usable name,
    which the handler answers with usage text instead of searching for a stray word.
    """
    raw = _strip_lark_mentions((text or "").strip())
    if not re.search(r"(?i)/FindVpnConf\b|find\s+vpn\s+(?:conf|file)", raw):
        return None
    m = re.search(r"(?i)/FindVpnConf\s+(.+)$", raw)
    if m:
        return _split_find_vpn_queries(m.group(1))
    m2 = re.search(
        r"(?i)(?:find|search)\s+(?:vpn\s+)?(?:conf(?:ig)?s?\s+)?(?:files?\s+)?(?:for\s+)?(.+?)\s*$",
        raw,
    )
    if m2:
        return _split_find_vpn_queries(m2.group(1))
    return []


def _handle_find_vpn_conf_lark(
    chat_id: str,
    queries: Any,
    reply_message_id: Optional[str] = None,
    sender_id: str = "",
) -> None:
    """Lark entry when user @ jenkinsbot with ``find vpn file <name>[,<name>…]``."""
    if isinstance(queries, str):
        qs = _split_find_vpn_queries(queries)
    else:
        qs = _split_find_vpn_queries(" ".join(str(q) for q in (queries or [])))
    thread_root = (reply_message_id or "").strip()
    if not qs:
        _emit_message(
            "text",
            {
                "text": (
                    "Usage: `find vpn file <name>[,<name>…]` — "
                    "e.g. `find vpn file alex` or `find vpn file alex,bob`"
                )
            },
            chat_id=chat_id,
            reply_message_id=thread_root or None,
        )
        return

    shown = ", ".join(f"`{q}`" for q in qs)
    if thread_root:
        _add_message_reaction(thread_root, "OK")
    _emit_message(
        "text",
        {"text": f"🔍 Finding VPN files matching {shown} — kindly wait…"},
        chat_id=chat_id,
        reply_message_id=thread_root or None,
    )

    matches, unmatched, err = search_vpn_conf_files_multi(qs)
    if err:
        _emit_message(
            "text",
            {"text": err},
            chat_id=chat_id,
            reply_message_id=thread_root or None,
        )
        return
    if not matches:
        _emit_message(
            "text",
            {"text": f"❌ No VPN `.conf` found matching {shown} in recent VPN_CREATION builds."},
            chat_id=chat_id,
            reply_message_id=thread_root or None,
        )
        return
    # Single name with a single hit stays a one-shot send — no card to tap for the common case.
    if len(qs) == 1 and len(matches) == 1:
        _vpn_find_deliver_row(chat_id, matches[0], thread_root)
        return

    cap = min(_vpn_find_max_buttons(), len(matches))
    picker_sid = secrets.token_hex(16)
    key = _vpn_find_session_key(chat_id, sender_id)
    with _vpn_find_sessions_lock:
        _vpn_find_prune_locked()
        _vpn_find_sessions[key] = {
            "state": "vpn_find_pick",
            "candidates": matches[:cap],
            "queries": qs,
            "unmatched": unmatched,
            "sent": [],
            "total": len(matches),
            "thread_root_id": thread_root,
            "picker_sid": picker_sid,
            "card_message_id": "",
            "created_at": time.time(),
        }
    _vpn_find_register_picker(picker_sid, key)

    card = _vpn_find_pick_card(
        matches[:cap],
        qs,
        picker_sid=picker_sid,
        sent=[],
        unmatched=unmatched,
        total=len(matches),
    )
    _ok, card_mid = _emit_message_result(
        "interactive",
        card,
        chat_id=chat_id,
        reply_message_id=thread_root or None,
    )
    # Needed so each tap can patch ✅ back onto the card it came from.
    if card_mid:
        with _vpn_find_sessions_lock:
            sess = _vpn_find_sessions.get(key)
            if isinstance(sess, dict) and sess.get("picker_sid") == picker_sid:
                sess["card_message_id"] = card_mid


def _parse_testaccess_build_arg(argv: List[str]) -> Optional[int]:
    for i, arg in enumerate(argv):
        if arg == "--build" and i + 1 < len(argv):
            try:
                n = int(str(argv[i + 1]).strip())
                return n if n > 0 else None
            except ValueError:
                return None
    return None


def _run_testaccess_cli(argv: Optional[List[str]] = None) -> int:
    """CLI: verify Jenkins REST access for VPN_CREATION (and optional build #)."""
    argv = argv if argv is not None else sys.argv
    job_base = VPN_CREATION_JOB_FOLDER_URL.rstrip("/") + "/"
    job_api = (
        f"{job_base.rstrip('/')}/api/json"
        "?tree=name,url,color,buildable,lastBuild[number]"
    )
    build_no = _parse_testaccess_build_arg(argv)

    print("jenkinsbot --testaccess")
    print(f"Job: {job_base}")
    print(f"createvpnid     = {VPN_JENKINS_USER or '(not set)'}")
    print(f"createvpntoken  = {_mask_secret(VPN_JENKINS_TOKEN)}")
    print(f"createvpnpass   = {_mask_secret(VPN_JENKINS_PASSWORD)}")
    if build_no:
        print(f"Build to probe  = #{build_no}")
    print()

    failures = 0
    working_auth: Optional[Tuple[str, str]] = None
    last_build: Optional[int] = None

    if not VPN_JENKINS_USER:
        print("FAIL  createvpnid is missing — set it in jenkinsbot/.env")
        failures += 1
    elif not VPN_JENKINS_TOKEN and not VPN_JENKINS_PASSWORD:
        print("FAIL  createvpntoken and createvpnpass are both missing")
        failures += 1
    else:
        candidates = _auth_candidates_for(job_base)
        for i, auth in enumerate(candidates, start=1):
            via = (
                "createvpntoken"
                if VPN_JENKINS_TOKEN and auth[1] == VPN_JENKINS_TOKEN
                else "createvpnpass"
            )
            print(f"--- VPN credential try {i}/{len(candidates)}: {auth[0]} via {via} ---")
            ok, status, data, hint = _jenkins_rest_get(job_api, auth)
            if ok and isinstance(data, dict):
                working_auth = auth
                lb = data.get("lastBuild")
                if isinstance(lb, dict) and lb.get("number"):
                    last_build = int(lb["number"])
                print(
                    f"OK    HTTP {status}  job={data.get('name')!r}  "
                    f"buildable={data.get('buildable')}  lastBuild=#{last_build or '?'}"
                )
                break
            print(f"FAIL  HTTP {status}  {hint or 'request failed'}")
            failures += 1
        print()

    probe_build = build_no or last_build
    if working_auth and probe_build:
        print(f"--- Build probe #{probe_build} ---")
        build_api = f"{job_base.rstrip('/')}/{probe_build}/api/json?tree=number,result,building,url"
        ok, status, data, hint = _jenkins_rest_get(build_api, working_auth)
        if ok and isinstance(data, dict):
            print(
                f"OK    HTTP {status}  #{data.get('number')}  "
                f"result={data.get('result')!r}  building={data.get('building')}"
            )
        else:
            print(f"FAIL  HTTP {status}  {hint or 'request failed'}")
            failures += 1
        print()

    # Download test (download only — nothing is created or sent to any chat).
    if working_auth and probe_build:
        print(f"--- Artifact download test (build #{probe_build}, download only) ---")
        artifacts = _list_build_artifacts(job_base, probe_build, working_auth)
        if not artifacts:
            print("WARN  no artifacts on this build — try another build with --build <n>")
        else:
            confs = [(fn, rel) for fn, rel in artifacts if fn.casefold().endswith(".conf")]
            picked = confs[0] if confs else artifacts[0]
            fn, rel = picked
            print(f"      artifacts found: {len(artifacts)}  picking: {fn}")
            tmpdir = tempfile.mkdtemp(prefix="vpnconf_test_")
            try:
                dest = os.path.join(tmpdir, fn)
                if _download_artifact(job_base, probe_build, rel, working_auth, dest):
                    size = os.path.getsize(dest) if os.path.isfile(dest) else 0
                    print(f"OK    downloaded {fn} ({size} bytes) — not sent anywhere.")
                else:
                    print(f"FAIL  could not download {fn}")
                    failures += 1
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        print()

    if failures:
        print("RESULT: FAIL — fix createvpnid / createvpntoken (API Token) / folder Read permission.")
        return 1
    print("RESULT: OK — jenkinsbot can access VPN_CREATION via REST.")
    return 0


def _lark_uses_persistent_connection() -> bool:
    """Default: persistent connection. Set LARK_EVENT_MODE=webhook for public Request URL."""
    mode = (os.getenv("LARK_EVENT_MODE") or "websocket").strip().lower()
    return mode not in ("webhook", "http", "request_url", "url", "request-url")


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _start_flask_server() -> None:
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)
    except OSError as exc:
        logger.error("Flask bind failed on 0.0.0.0:%s: %s", PORT, exc)
        raise


def _wait_flask_ready(timeout_sec: float = 90.0) -> bool:
    url = f"http://127.0.0.1:{PORT}/healthz"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def _handle_ws_im_message(data) -> None:
    """Persistent connection: handle im.message.receive_v1 without HTTP loopback."""
    try:
        import lark_oapi as lark

        raw = json.loads(lark.JSON.marshal(data))
    except Exception as exc:
        logger.warning("ws event marshal failed: %s", exc)
        return

    event = raw.get("event") if isinstance(raw, dict) else None
    if not isinstance(event, dict):
        event = raw if isinstance(raw, dict) else {}

    message = event.get("message", {})
    if not isinstance(message, dict):
        message = {}
    message_id = (message.get("message_id") or event.get("message_id") or "").strip()
    message_type = message.get("message_type", "")
    event_chat_id = (message.get("chat_id") or "").strip()

    if message_type != "text" or not message_id:
        return

    text = _extract_text_message(event)
    if not text:
        return

    logger.info("ws im.message message_id=%s text=%r", message_id, text[:300])
    # Same dedupe the HTTP route does. This path had none, and persistent connection is the
    # default mode — so a redelivery after a websocket reconnect started a SECOND watch worker on
    # the same build, which then sent a second /SuccessProceedNext to the duty bot and advanced
    # its queue past a segment that had not built yet.
    event_key = (raw.get("header") or {}).get("event_id") or raw.get("uuid") or message_id
    if _event_seen_already(str(event_key)):
        logger.info("ws duplicate event skipped key=%s message_id=%s", event_key, message_id)
        return
    threading.Thread(
        target=_process_message_command,
        args=(text, message_id, event_chat_id, _event_sender_open_id(event)),
        daemon=True,
        name=f"jenkinsbot-ws-{(message_id or '')[:12]}",
    ).start()


def _handle_ws_card_action(data) -> None:
    try:
        import lark_oapi as lark

        raw = json.loads(lark.JSON.marshal(data))
    except Exception as exc:
        logger.warning("ws card marshal failed: %s", exc)
        return
    if not isinstance(raw, dict):
        return
    if "header" in raw and "event" in raw:
        payload = dict(raw)
    else:
        # Carry the event id through instead of dropping it — without it the dedupe below can
        # never fire, and a redelivered card frame runs the handler twice.
        _hdr = {"event_type": "card.action.trigger"}
        _eid = (raw.get("header") or {}).get("event_id") or raw.get("uuid") or ""
        if _eid:
            _hdr["event_id"] = str(_eid)
        payload = {
            "schema": "2.0",
            "header": _hdr,
            "event": raw.get("event", raw),
        }
    event_key = (payload.get("header") or {}).get("event_id") or payload.get("uuid") or ""
    if event_key and _event_seen_already(str(event_key)):
        logger.info("ws duplicate card action skipped key=%s", event_key)
        return
    threading.Thread(
        target=_process_card_action_payload,
        args=(payload,),
        daemon=True,
        name="jenkinsbot-ws-card",
    ).start()


def _run_lark_persistent_connection() -> None:
    global _LARK_RUNTIME_HOST
    try:
        import lark_oapi as lark
    except ImportError:
        logger.error("pip install lark-oapi  (required for persistent connection)")
        raise SystemExit(1)

    encrypt_key = (
        (os.getenv("LARK_ENCRYPT_KEY") or os.getenv("ENCRYPT_KEY") or "").strip()
    )
    vtoken = (VERIFICATION_TOKEN or "").strip()
    if encrypt_key:
        builder = lark.EventDispatcherHandler.builder(vtoken, encrypt_key)
    else:
        builder = lark.EventDispatcherHandler.builder("", "")

    handler = (
        builder.register_p2_im_message_receive_v1(_handle_ws_im_message)
        .register_p2_card_action_trigger(_handle_ws_card_action)
        .build()
    )

    candidates = _lark_ws_domain_candidates(lark)
    print(
        "[jenkinsbot] 1) Wait for: connected to wss://…\n"
        "[jenkinsbot] 2) Then Feishu console → Events → "
        "Receive events through persistent connection → Save",
        flush=True,
    )

    last_exc: Optional[BaseException] = None
    for label, domain_url in candidates:
        logger.info(
            "Trying Lark WebSocket domain=%s (%s) APP_ID=%s…",
            domain_url,
            label,
            (APP_ID or "")[:8],
        )
        cli = lark.ws.Client(
            APP_ID,
            APP_SECRET,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            domain=domain_url,
        )
        try:
            _LARK_RUNTIME_HOST = domain_url.rstrip("/")
            cli.start()
            return
        except Exception as exc:
            last_exc = exc
            if _is_lark_domain_mismatch(exc):
                logger.warning("Domain %s rejected (%s) — trying next", domain_url, exc)
                _LARK_RUNTIME_HOST = None
                continue
            err = str(exc)
            logger.exception("Lark WebSocket failed: %s", exc)
            if "CERTIFICATE_VERIFY_FAILED" in err or "certificate verify failed" in err.lower():
                print(
                    "[jenkinsbot] SSL error — try: pip install certifi && "
                    "export SSL_CERT_FILE=$(python -c \"import certifi; print(certifi.where())\")",
                    flush=True,
                )
            raise SystemExit(1) from exc

    logger.error("All Lark domains failed. Last error: %s", last_exc)
    print(
        "[jenkinsbot] 1000040351 Incorrect domain name — your APP was created on ONE of:\n"
        "  • https://open.feishu.cn/app     → LARK_HOST=https://open.feishu.cn  LARK_DOMAIN=feishu\n"
        "  • https://open.larksuite.com/app → LARK_HOST=https://open.larksuite.com  LARK_DOMAIN=lark\n"
        "Open the console URL where this APP_ID exists and match LARK_HOST.",
        flush=True,
    )
    raise SystemExit(1) from last_exc


def _run_main_entry() -> int:
    logger.info(
        "jenkinsbot start python=%s cwd=%s .env=%s exists=%s",
        sys.executable,
        os.getcwd(),
        _env_file,
        _env_file.is_file(),
    )
    try:
        from vpn_warm import prewarm_vpn_browser_on_startup

        prewarm_vpn_browser_on_startup()
    except Exception as exc:
        logger.warning("VPN browser prewarm skipped: %s", exc)
    if _lark_uses_persistent_connection():
        if _port_in_use(PORT):
            logger.error(
                "Port %s already in use — stop manual `python main.py` before systemctl start",
                PORT,
            )
            return 1
        t = threading.Thread(
            target=_start_flask_server,
            daemon=True,
            name="jenkinsbot-flask",
        )
        t.start()
        if not _wait_flask_ready():
            logger.error("Flask did not start on port %s", PORT)
            return 1
        logger.info("Flask ready on 0.0.0.0:%s (/healthz, /internal/*)", PORT)
        _run_lark_persistent_connection()
        return 0

    logger.info("jenkinsbot webhook mode on 0.0.0.0:%s (/webhook/event)", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    if "--testaccess" in sys.argv:
        raise SystemExit(_run_testaccess_cli())
    raise SystemExit(_run_main_entry())
