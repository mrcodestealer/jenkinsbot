"""The whole console log reaches the chat — in an expand/collapse block, and as {pipeline}.log.

Run with ``python tests/test_console_log_attachment.py``. No network, no Jenkins, no Lark.

Why this file exists
--------------------
The done card used to embed ``_console_last_lines(text, max_lines=10)``. Ten lines of a Jenkins
console is the *end* of the story and never the reason for a failure, so the log now goes into a
card JSON 1.0 ``collapsible_panel`` and the complete text is uploaded as ``{pipeline}.log``.

Both halves of that are easy to break in ways nobody notices until a real deploy:

* **The card silently stops sending.** Lark rejects a request body over 30 KB. The card JSON is
  escaped TWICE on the way out, so a raw-byte budget is not a body-size guarantee — a log full of
  Windows paths inflates ~2x. If the shrink ladder regresses, a big or escape-heavy build posts
  nothing at all: no card, no @mention, and the duty bot never hears the build finished.
* **The attachment lands in the wrong place.** ``_send_file_message`` is chat-only, so using it
  would drop the .log into the chat root while the card sits in a thread — the same class of bug
  as ``tests/test_duty_callback_chat.py``.
* **The log path swallows the duty callbacks.** ``_send_console_log_file`` runs BEFORE
  ``_notify_duty_after_inform_watch`` in ``_jenkins_watch_worker``. An exception escaping it parks
  an ``/updatemore`` queue at ``waiting_jenkins`` forever — the incident
  ``tests/test_watch_dedupe_and_notify_failures.py`` was written about.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main.py reads its whole config at import time (and raises on anything missing) but starts
# nothing on import. Same placeholders as the other two test files. Note the deliberate absence
# of any JENKINS_LOG_* key: those are read through os.getenv with defaults, never _env, so a
# fresh .env must not be required for the feature to work.
for _k, _v in (
    ("LARK_HOST", "https://open.larksuite.com"),
    ("VERIFICATION_TOKEN", "tok_test"),
    ("APP_ID", "cli_test"),
    ("APP_SECRET", "secret_test"),
    ("PORT", "5000"),
    ("JENKINS_USER", "u"),
    ("JENKINS_PASSWORD", "p"),
    ("NOTIFY_CHAT_ID", "oc_DUTY"),
    ("JENKINS_POLL_SECONDS", "1"),
    ("JENKINS_STUCK_SECONDS", "600"),
):
    os.environ.setdefault(_k, _v)

import main as jb  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0

CHAT = "oc_REAL"
THREAD = "om_root"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


class Capture:
    """Replaces the two outbound channels and records what each one was handed."""

    def __init__(self, *, file_key: str | None = "file_v3_test") -> None:
        self.sent: list[tuple[str, dict, str, str | None]] = []
        self.uploads: list[tuple[str, int, str]] = []  # (file_name, bytes, first 40 chars)
        self.errors: list[str] = []
        self._file_key = file_key
        self._saved: dict[str, object] = {}

    def __enter__(self) -> "Capture":
        def fake_emit(msg_type, content_obj, *, chat_id, reply_message_id=None):
            self.sent.append((msg_type, content_obj, chat_id, reply_message_id))
            return True

        def fake_upload(path, file_name, *, timeout=120):
            with open(path, encoding="utf-8") as fh:
                head = fh.read(40)
            self.uploads.append((file_name, os.path.getsize(path), head))
            return self._file_key

        def fake_error(msg, *a, **kw):
            self.errors.append(str(msg) % a if a else str(msg))

        for name, fn in (
            ("_emit_message", fake_emit),
            ("_upload_file_lark", fake_upload),
        ):
            self._saved[name] = getattr(jb, name)
            setattr(jb, name, fn)
        self._saved["logger.error"] = jb.logger.error
        jb.logger.error = fake_error
        return self

    def __exit__(self, *exc) -> None:
        for name, fn in self._saved.items():
            if name == "logger.error":
                jb.logger.error = fn
            else:
                setattr(jb, name, fn)

    @property
    def cards(self) -> list[dict]:
        return [obj for t, obj, _c, _r in self.sent if t == "interactive"]


def panel_of(card: dict) -> dict | None:
    for el in card.get("elements") or []:
        if el.get("tag") == "collapsible_panel":
            return el
    return None


def summary_of(card: dict) -> str:
    return (card["elements"][0].get("text") or {}).get("content") or ""


def send_card(log: str, **kw) -> tuple[dict, Capture]:
    """Drive the real _send_done_card and hand back the card it tried to send."""
    with Capture() as cap:
        jb._send_done_card(
            kw.pop("result", "SUCCESS"),
            log,
            pipeline=kw.pop("pipeline", "FPMS_PROD_SCRIPT_RUN"),
            environment=kw.pop("environment", "fpms-prod"),
            build=kw.pop("build", 738),
            build_url="https://j/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/738/",
            console_text_url="https://j/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/738/consoleText",
            chat_id=CHAT,
            reply_message_id=THREAD,
            **kw,
        )
    check(len(cap.cards) == 1, f"exactly one card was sent (got {len(cap.cards)})")
    return (cap.cards[0] if cap.cards else {}), cap


def body_bytes(card: dict) -> int:
    return jb._card_request_bytes(card, chat_id=CHAT, reply_message_id=THREAD)


SMALL_LOG = (
    "Started by user junchen\n"
    "[Pipeline] }\n[Pipeline] // node\n[Pipeline] }\n[Pipeline] // podTemplate\n"
    "[Pipeline] }\n[Pipeline] // node\n[Pipeline] echo\n"
    "No pending scripts for approval.\n[Pipeline] End of Pipeline\nFinished: SUCCESS\n"
)


# --------------------------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------------------------
def test_a_half_megabyte_console_still_produces_a_sendable_card() -> None:
    """The expensive one: if this regresses, a big build posts NOTHING — no card, no @mention,
    and no duty handoff."""
    big = "".join(f"[{i:06d}] a fairly typical jenkins console output line\n" for i in range(9000))
    card, _cap = send_card(big, log_file_name="FPMS_PROD_SCRIPT_RUN.log")
    size = body_bytes(card)
    check(
        size <= jb._CARD_BODY_LIMIT_BYTES,
        f"a {len(big)}-byte log yields a {size}-byte body, cap {jb._CARD_BODY_LIMIT_BYTES}",
    )
    check(panel_of(card) is not None, "the panel survives the shrink ladder")


def test_an_escape_heavy_log_shrinks_instead_of_failing() -> None:
    """Windows paths and embedded JSON inflate ~2x under double JSON escaping, so the raw-byte
    budget alone is not a guarantee — only the measured body size is."""
    nasty = '{"path": "C:\\\\jenkins\\\\workspace\\\\x"}\n' * 12000
    card, _cap = send_card(nasty, result="FAILURE", log_file_name="x.log")
    size = body_bytes(card)
    check(
        size <= jb._CARD_BODY_LIMIT_BYTES,
        f"an escape-heavy log yields a {size}-byte body, cap {jb._CARD_BODY_LIMIT_BYTES}",
    )


def test_the_log_lives_in_an_expand_collapse_block_not_the_summary() -> None:
    card, _cap = send_card(SMALL_LOG, log_file_name="FPMS_PROD_SCRIPT_RUN.log")
    panel = panel_of(card)
    check(panel is not None, "the console is in a collapsible_panel")
    if panel:
        check(panel["expanded"] is False, "the panel arrives collapsed")
        check(
            panel["elements"][0]["tag"] == "markdown",
            f"the log is in a markdown component, not {panel['elements'][0]['tag']!r} "
            "(fenced code blocks do not render in div/lark_md)",
        )
        check(
            panel["header"]["title"]["tag"] in ("markdown", "plain_text"),
            "the panel header title tag is one the 1.0 schema allows",
        )
    check("```" not in summary_of(card), "no code fence left in the always-visible summary")
    check(
        "[Pipeline]" not in summary_of(card),
        "no log line left in the always-visible summary",
    )


def test_the_card_is_still_json_1_0() -> None:
    """collapsible_panel is documented for card JSON 1.0. Migrating to 2.0 would raise the
    client floor from V7.9 to V7.20 and rename header/title keys — a needless regression."""
    card, _cap = send_card(SMALL_LOG, log_file_name="x.log")
    check("schema" not in card, "no 2.0 'schema' key")
    check("body" not in card, "no 2.0 'body' wrapper")
    check(isinstance(card.get("elements"), list), "1.0 top-level 'elements' array")
    check(card["header"]["title"]["tag"] == "plain_text", "1.0 header title untouched")
    check(card["config"] == {"wide_screen_mode": True}, "1.0 config untouched")


def test_the_last_ten_lines_clamp_is_gone() -> None:
    """The whole point of the change: a 60-line log must show all 60 lines, not 10."""
    log = "".join(f"line {i}\n" for i in range(60))
    card, _cap = send_card(log, log_file_name="x.log")
    panel = panel_of(card)
    content = panel["elements"][0]["content"] if panel else ""
    for probe in ("line 0\n", "line 25\n", "line 59"):
        check(probe in content, f"{probe.strip()!r} is present in the panel")
    check("(complete)" in panel["header"]["title"]["content"], "the panel says it is complete")


def test_a_pathological_summary_never_blocks_the_send() -> None:
    """When even the no-log rung is oversized there is nothing left to shrink — send it and let
    the API answer, rather than silently posting nothing."""
    card, cap = send_card(SMALL_LOG, pipeline="P" * 40000, log_file_name="x.log")
    check(bool(card), "a card was still sent")
    check(
        any("with no log embed" in e for e in cap.errors),
        f"the oversize fallback was logged (errors={cap.errors!r})",
    )


def test_ansi_escapes_and_stray_fences_cannot_corrupt_the_card() -> None:
    card, _cap = send_card(
        "\x1b[31mred build\x1b[0m\n```\nnested fence\n```\nFinished: FAILURE\n",
        result="FAILURE",
        log_file_name="x.log",
    )
    blob = json.dumps(card)
    check("\x1b" not in blob and "\\u001b" not in blob, "no ANSI escape reaches the card")
    panel = panel_of(card)
    check(
        panel["elements"][0]["content"].count("```") == 2,
        "exactly one fence pair — a log's own ``` cannot close ours early",
    )


def test_an_escape_leaves_no_visible_payload_behind() -> None:
    """Stripping the ESC but not its payload is worse than leaving both: _LOG_CTRL_RE removes
    the bare ESC, so any escape _ANSI_RE misses gets PROMOTED to visible text. ``tput sgr0``
    is ESC ( B ESC [ m on every xterm* TERM, and used to leave a stray "(B" in the panel."""
    cases = [
        ("+ tput sgr0\x1b(B\x1b[m\nBUILD OK\n", "+ tput sgr0\nBUILD OK\n"),
        ("\x1b[38:2:255:0:0mred\x1b[0m\n", "red\n"),      # colon-separated true colour
        ("\x1b[38;2;255;0;0mred\x1b[0m\n", "red\n"),      # semicolon form
        ("\x1b)0line\n", "line\n"),
        ("\x1b[31mred\x1b[0m\n", "red\n"),
        ("\x1b]0;window title\x07ok\n", "ok\n"),          # OSC
        ("\x1bcreset\n", "reset\n"),                       # RIS
    ]
    for raw, want in cases:
        got = jb._console_text_for_card(raw)
        check(got == want, f"_console_text_for_card({raw!r}) -> {got!r}, wanted {want!r}")


def test_a_long_single_line_still_fills_the_panel() -> None:
    """Re-aligning the embed window to a line boundary used to drop everything up to the first
    newline. When the window opened mid-line — a JSON dump, a base64 blob, a minified bundle —
    that discarded the whole 18 KB budget and left a two-line stub: exactly the truncated tail
    this change exists to remove, just hidden behind an expand arrow."""
    log = "start of build\n" + "B" * 204800 + "\nFinished: FAILURE\n"
    card, _cap = send_card(log, result="FAILURE", log_file_name="mypipe.log")
    panel = panel_of(card)
    check(panel is not None, "the panel exists for a 200 KB single-line log")
    body = panel["elements"][0]["content"] if panel else ""
    check(
        len(body) > 10000,
        f"the embed budget is actually used (panel body is {len(body)} chars, want > 10000)",
    )
    check("Finished: FAILURE" in body, "the tail of the log is still the end of the panel")
    check(body_bytes(card) <= jb._CARD_BODY_LIMIT_BYTES, "and it still fits the body cap")

    # A cheap realignment must still happen, so an ordinary log never starts mid-line.
    tail, _dropped = jb._console_tail_bytes("".join(f"line {i}\n" for i in range(4000)), 1000)
    check(not tail.startswith("ine "), f"an ordinary log is line-aligned (got {tail[:12]!r})")


def test_a_successful_vpn_build_keeps_its_conf_only_card() -> None:
    """VPN-success threads have deliberately never carried console text and already get the
    .conf; a FAILED vpn build, whose message today only says 请检查 console, now gets the log."""
    check(jb._console_wanted_for("SUCCESS", vpn_mode=True) is False, "vpn SUCCESS: no console")
    check(jb._console_wanted_for("FAILURE", vpn_mode=True) is True, "vpn FAILURE: console")
    for r in ("SUCCESS", "FAILURE", "UNSTABLE", "ABORTED"):
        check(
            jb._console_wanted_for(r, vpn_mode=False) is True,
            f"non-vpn {r}: console wanted",
        )
    card, _cap = send_card(SMALL_LOG, vpn_mode=True)
    check(panel_of(card) is None, "no panel on the vpn success card")
    check("done created vpn" in summary_of(card), "the vpn wording is unchanged")


# --------------------------------------------------------------------------------------------
# The {pipeline}.log attachment
# --------------------------------------------------------------------------------------------
def test_the_log_filename_is_safe_everywhere() -> None:
    cases = [
        ("FPMS_PROD_SCRIPT_RUN", "FPMS_PROD_SCRIPT_RUN.log"),
        ("My%20Job%2Fsub", "My_Job_sub.log"),   # job segments are never percent-decoded
        ("unknown", "console-738.log"),         # literal fallback from _pipeline_and_env_…
        ("", "console-738.log"),
        ("NUL", "NUL_job.log"),                 # reserved on Windows
        ("job.", "job.log"),
    ]
    for raw, want in cases:
        got = jb._safe_log_filename(raw, 738)
        check(got == want, f"_safe_log_filename({raw!r}) -> {got!r}, wanted {want!r}")
    long = jb._safe_log_filename("a" * 200, 738)
    check(len(long) <= 84, f"a 200-char pipeline is capped (got {len(long)} chars)")
    bad = jb._safe_log_filename('bad<>:"|?*name', 738)
    check(
        not any(c in bad for c in '<>:"/\\|?*'),
        f"no illegal filename character survives (got {bad!r})",
    )


def test_the_attachment_is_threaded_like_the_card() -> None:
    """_send_file_message is chat-only — using it would drop the .log into the chat root while
    the card sits in the thread."""
    with Capture() as cap:
        ok = jb._send_console_log_file(
            SMALL_LOG,
            file_name="FPMS_PROD_SCRIPT_RUN.log",
            chat_id=CHAT,
            reply_message_id=THREAD,
        )
    check(ok is True, "the attachment reports success")
    check(
        cap.sent == [("file", {"file_key": "file_v3_test"}, CHAT, THREAD)],
        f"the file went to the card's thread (got {cap.sent!r})",
    )
    check(
        cap.uploads and cap.uploads[0][0] == "FPMS_PROD_SCRIPT_RUN.log",
        f"uploaded under the {{pipeline}}.log name (got {cap.uploads!r})",
    )
    # Strip the docstring first — it names _send_file_message on purpose, to say why the
    # chat-only helper is the wrong one here.
    src = re.sub(r'""".*?"""', "", inspect.getsource(jb._send_console_log_file), flags=re.S)
    check("_send_file_message" not in src, "the chat-only helper is not reintroduced")
    check("_emit_message(" in src, "_emit_message is what carries the file")


def test_the_uploaded_log_is_the_whole_console() -> None:
    log = "".join(f"line {i}\n" for i in range(5000))
    with Capture() as cap:
        jb._send_console_log_file(log, file_name="x.log", chat_id=CHAT, reply_message_id=THREAD)
    check(
        cap.uploads and cap.uploads[0][1] == len(log.encode("utf-8")),
        f"every byte of a {len(log)}-byte console was uploaded (got {cap.uploads!r})",
    )


def test_an_oversized_log_is_capped_and_says_so() -> None:
    saved = os.environ.get("JENKINS_LOG_FILE_MAX_BYTES")
    os.environ["JENKINS_LOG_FILE_MAX_BYTES"] = "8192"
    try:
        with Capture() as cap:
            jb._send_console_log_file(
                "x" * (2 * 1024 * 1024), file_name="x.log", chat_id=CHAT
            )
        name, size, head = cap.uploads[0] if cap.uploads else ("", 0, "")
        # <= cap, not cap + slack: the marker is prepended AFTER the tail is cut, so the tail
        # budget has to reserve room for it. At the documented 30 MB ceiling an overshoot is
        # rejected by Lark outright and no attachment arrives at all.
        check(size <= 8192, f"the upload never exceeds its own cap (got {size} bytes / 8192)")
        check(head.startswith("[…"), f"the cut is announced in the file (head={head!r})")
    finally:
        if saved is None:
            os.environ.pop("JENKINS_LOG_FILE_MAX_BYTES", None)
        else:
            os.environ["JENKINS_LOG_FILE_MAX_BYTES"] = saved


def test_an_empty_console_is_never_uploaded() -> None:
    for log in ("", "\n\n   \n"):
        with Capture() as cap:
            ok = jb._send_console_log_file(log, file_name="x.log", chat_id=CHAT)
        check(ok is False and cap.uploads == [], f"{log!r} uploads nothing (Lark rejects it)")


def test_an_upload_failure_is_contained() -> None:
    """Everything downstream of the attachment in _jenkins_watch_worker must still run."""
    with Capture(file_key=None) as cap:
        ok = jb._send_console_log_file(SMALL_LOG, file_name="x.log", chat_id=CHAT)
    check(ok is False, "a refused upload returns False")
    check(cap.sent == [], "no file message is sent without a file_key")

    saved = jb._upload_file_lark

    def boom(*a, **kw):
        raise RuntimeError("lark exploded")

    jb._upload_file_lark = boom
    try:
        ok = jb._send_console_log_file(SMALL_LOG, file_name="x.log", chat_id=CHAT)
    except Exception as exc:  # noqa: BLE001 — the whole point is that this cannot happen
        _FAILURES.append(f"an upload exception escaped _send_console_log_file: {exc!r}")
        ok = None
    finally:
        jb._upload_file_lark = saved
    check(ok is False, "a raising upload is swallowed and reported as False")


# --------------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------------
def test_a_failed_upload_corrects_the_card_it_already_promised() -> None:
    """The card goes out first (it is the primary signal, and the duty callbacks are downstream
    of it, so delaying it behind a 60 s upload would be worse). It therefore already says
    "(attached below)" by the time an upload can fail — so the failure has to be said out loud,
    in the same thread, or the reader waits forever for a file that will never arrive."""
    src = inspect.getsource(jb._jenkins_watch_worker)
    i_log = src.find("_send_console_log_file(")
    i_notify = src.find("_send_done_notify(")
    window = src[i_log:i_notify]
    check(
        "if not attached:" in window,
        "the worker branches on whether the attachment actually landed",
    )
    check(
        "console_text_url" in window,
        "the correction points at the consoleText URL as the remaining copy",
    )
    check(
        window.count("except Exception") >= 2,
        "both the upload and its correction are wrapped, so neither can skip the duty callback",
    )


def test_the_log_send_sits_between_the_card_and_the_duty_callbacks() -> None:
    src = inspect.getsource(jb._jenkins_watch_worker)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)  # drop docstrings/comment prose
    src = re.sub(r"#[^\n]*", "", src)
    i_card = src.find("_send_done_card(")
    i_log = src.find("_send_console_log_file(")
    i_duty = src.find("_notify_duty_after_inform_watch(")
    check(i_card >= 0 and i_log >= 0 and i_duty >= 0, "all three calls are present")
    check(i_card < i_log, "the card goes out before the attachment")
    check(i_log < i_duty, "the attachment goes out before the duty callback")
    check(
        "except Exception" in src[i_log:i_duty],
        "the attachment is wrapped, so it cannot skip the duty callback",
    )


def test_the_new_tunables_do_not_require_a_env_entry() -> None:
    """A new _env() key would raise RuntimeError at import for everyone who has not updated
    their .env — including both existing test files."""
    for fn in (
        jb._log_card_max_bytes,
        jb._log_file_max_bytes,
        jb._log_file_enabled,
        jb._log_panel_expanded,
        jb._log_panel_code_block,
    ):
        src = inspect.getsource(fn)
        check(
            "_env(" not in src,
            f"{fn.__name__} reads os.getenv, not the raising _env()",
        )
    check(jb._log_card_max_bytes() == 18000, "card embed default is 18000")
    check(jb._log_file_max_bytes() == 8 * 1024 * 1024, "file cap default is 8 MB")
    check(jb._log_file_enabled() is True, "the attachment is on by default")
    check(jb._log_panel_expanded() is False, "the panel is collapsed by default")

    for bad in ("", "not-a-number"):
        os.environ["JENKINS_LOG_CARD_MAX_BYTES"] = bad
        try:
            check(
                jb._log_card_max_bytes() == 18000,
                f"a junk JENKINS_LOG_CARD_MAX_BYTES={bad!r} falls back to the default",
            )
        finally:
            os.environ.pop("JENKINS_LOG_CARD_MAX_BYTES", None)
    os.environ["JENKINS_LOG_CARD_MAX_BYTES"] = "999999"
    try:
        check(jb._log_card_max_bytes() == 24000, "an absurd embed budget is clamped to 24000")
    finally:
        os.environ.pop("JENKINS_LOG_CARD_MAX_BYTES", None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception:
            import traceback

            traceback.print_exc()
            _FAILURES.append(f"{fn.__name__} raised")
    print(f"\n{_RUN} checks, {len(_FAILURES)} failure(s)")
    for f in _FAILURES:
        print(f"  - {f}")
    sys.exit(1 if _FAILURES else 0)
