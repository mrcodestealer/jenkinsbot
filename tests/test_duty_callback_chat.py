"""Pin that a build's duty-bot callback goes to the chat the build was informed from.

Run with ``python3 tests/test_duty_callback_chat.py``. No network — every outbound call is
captured.

The bug this guards: ``_notify_duty_after_inform_watch`` receives the watcher's ``meta``, which
carries ``chat_id`` (set from the real event chat in ``_process_message_command`` and used
correctly for the done card), but all four of its outbound channels hard-coded ``NOTIFY_CHAT_ID``.
The duty bot resolves a ``/updatemore`` queue **by chat id**, so any update started in a chat other
than the duty chat produced:

  * ``/SuccessProceedNext`` delivered against a chat with no queue -> the real queue stayed at
    ``waiting_jenkins`` until its watchdog gave up, and the "no active queue" warning was posted
    in front of the wrong people;
  * ``/replyupdateemail`` delivered against a chat with no e-mail batch -> the customer reply was
    never sent, with no error anywhere.

Both the HTTP call and its Lark fallback failed the same way, so there was no second chance.
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Console output here is UTF-8 (em dashes in messages, arrows in diagnostics). A cp1252 console
# raises UnicodeEncodeError mid-print and the run reads as a test failure, so make stdout tolerant
# rather than requiring PYTHONIOENCODING to be set by whoever runs this.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# main.py reads its whole config at import time (and raises on anything missing) but starts
# nothing on import. Fill in placeholders so the module is importable without a real .env.
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


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


REAL_CHAT = "oc_REAL"
DUTY_CHAT = "oc_DUTY"
CTX = {"pipeline": "FPMS-UAT", "environment": "UAT", "build_url": "https://j/job/FPMS/412/"}


class Capture:
    """Replaces every outbound channel and records where each one aimed."""

    def __init__(self, *, http_ok: bool) -> None:
        self.http_updatemore: list[tuple[str, str]] = []   # (chat_id, command)
        self.http_email: list[tuple[str, str]] = []        # (chat_id, title)
        self.lark_duty: list[tuple[str, str]] = []         # (chat_id, text)
        self.chat_msgs: list[tuple[str, str]] = []         # (chat_id, text)
        self._http_ok = http_ok
        self._saved: dict[str, object] = {}

    def __enter__(self) -> "Capture":
        def fake_http_updatemore(command, chat_id=None):
            self.http_updatemore.append(((chat_id or "").strip(), command))
            return self._http_ok

        def fake_http_email(title, pipeline, when, chat_id=None):
            self.http_email.append(((chat_id or "").strip(), title))
            return self._http_ok

        def fake_duty_text(text, chat_id=None):
            self.lark_duty.append(((chat_id or "").strip(), text))
            return True

        def fake_chat_message(chat_id, msg_type, content, **kw):
            self.chat_msgs.append((chat_id, str(content)))
            return True

        for name, fn in (
            ("_notify_duty_updatemore_callback_http", fake_http_updatemore),
            ("_notify_duty_reply_update_email_http", fake_http_email),
            ("_send_duty_text", fake_duty_text),
            ("_send_chat_message", fake_chat_message),
        ):
            self._saved[name] = getattr(jb, name)
            setattr(jb, name, fn)
        return self

    def __exit__(self, *exc) -> None:
        for name, fn in self._saved.items():
            setattr(jb, name, fn)

    def every_target(self) -> set[str]:
        return {
            c
            for c, _ in (
                self.http_updatemore + self.http_email + self.lark_duty + self.chat_msgs
            )
        }


def test_a_successful_proceed_targets_the_informing_chat() -> None:
    meta = {"mode": "inform", "chat_id": REAL_CHAT}
    with Capture(http_ok=True) as cap:
        jb._notify_duty_after_inform_watch("SUCCESS", meta, CTX)
    check(
        cap.http_updatemore == [(REAL_CHAT, "/SuccessProceedNext")],
        f"the proceed carries the informing chat (got {cap.http_updatemore!r})",
    )
    check(DUTY_CHAT not in cap.every_target(), "nothing leaked to the duty chat")


def test_a_failure_targets_the_informing_chat() -> None:
    meta = {"mode": "inform", "chat_id": REAL_CHAT}
    with Capture(http_ok=True) as cap:
        jb._notify_duty_after_inform_watch("FAILURE", meta, CTX)
    check(
        cap.http_updatemore == [(REAL_CHAT, "/FailedStop")],
        f"/FailedStop carries the informing chat (got {cap.http_updatemore!r})",
    )


def test_an_email_completion_targets_the_informing_chat() -> None:
    meta = {"mode": "inform_time", "chat_id": REAL_CHAT, "email_title": "Livechat v1.0.27 - CP"}
    with Capture(http_ok=True) as cap:
        jb._notify_duty_after_inform_watch("SUCCESS", meta, CTX)
    check(
        cap.http_email == [(REAL_CHAT, "Livechat v1.0.27 - CP")],
        f"the e-mail callback carries the informing chat (got {cap.http_email!r})",
    )
    check(DUTY_CHAT not in cap.every_target(), "nothing leaked to the duty chat")


def test_the_lark_fallback_targets_the_informing_chat_too() -> None:
    """A fallback that lands in the wrong chat is not a fallback."""
    for meta in (
        {"mode": "inform", "chat_id": REAL_CHAT},
        {"mode": "inform_time", "chat_id": REAL_CHAT, "email_title": "Some Subject"},
    ):
        with Capture(http_ok=False) as cap:
            jb._notify_duty_after_inform_watch("SUCCESS", meta, CTX)
        check(
            bool(cap.lark_duty) and all(c == REAL_CHAT for c, _ in cap.lark_duty),
            f"{meta['mode']}: Lark fallback aimed at {[c for c, _ in cap.lark_duty]!r}",
        )
        check(
            DUTY_CHAT not in cap.every_target(),
            f"{meta['mode']}: nothing leaked to the duty chat on the fallback path",
        )


def test_a_watch_with_no_chat_still_falls_back_to_the_duty_chat() -> None:
    """Older metas (and manual commands) carry no chat — the old default must still apply."""
    with Capture(http_ok=True) as cap:
        jb._notify_duty_after_inform_watch("SUCCESS", {"mode": "inform"}, CTX)
    check(
        cap.http_updatemore == [(DUTY_CHAT, "/SuccessProceedNext")],
        f"no chat in meta -> NOTIFY_CHAT_ID (got {cap.http_updatemore!r})",
    )


def test_send_duty_text_defaults_to_the_duty_chat() -> None:
    sent: list[str] = []
    saved = jb._send_chat_message
    jb._send_chat_message = lambda chat_id, t, c, **kw: (sent.append(chat_id), True)[1]
    try:
        jb._send_duty_text("/SuccessProceedNext")
        jb._send_duty_text("/SuccessProceedNext", REAL_CHAT)
    finally:
        jb._send_chat_message = saved
    check(sent == [DUTY_CHAT, REAL_CHAT], f"explicit chat wins, default holds (got {sent!r})")


def test_a_slash_command_is_addressed_to_the_duty_bot_not_a_person() -> None:
    """``/SuccessProceedNext`` is an instruction to osedutybot, so the @ has to be the BOT.

    It used to be ``TAG_USER_OPEN_ID`` — the OM duty *person* — so the chat showed
    "@CP OM Duty /SuccessProceedNext": a slash command shouted at a human, while the bot that
    actually handles it was never the addressee. ``DUTY_BOT_OPEN_ID`` existed for exactly this
    and was referenced nowhere in the module."""
    sent: list[str] = []
    saved = jb._send_chat_message
    jb._send_chat_message = lambda chat_id, t, c, **kw: (sent.append(c["text"]), True)[1]
    try:
        jb._send_duty_text("/SuccessProceedNext", REAL_CHAT)
        jb._send_duty_text("/FailedStop", REAL_CHAT)
    finally:
        jb._send_chat_message = saved
    check(len(sent) == 2, f"both commands were sent (got {len(sent)})")
    for text in sent:
        check(
            jb.DUTY_BOT_OPEN_ID in text,
            f"the duty bot is the addressee (got {text!r})",
        )
        check(
            jb.TAG_USER_OPEN_ID not in text,
            f"the OM duty person is NOT tagged with a slash command (got {text!r})",
        )
    # And the human-facing card @ must NOT have been switched over with it.
    check(
        jb.TAG_USER_OPEN_ID in jb._tag_user_at_card(),
        "the done card still @-mentions the OM duty person",
    )
    check(
        jb.DUTY_BOT_OPEN_ID != jb.TAG_USER_OPEN_ID,
        "the two ids are genuinely different recipients",
    )


def test_an_unknown_mode_notifies_nobody() -> None:
    """Unchanged behaviour, pinned so the chat threading did not accidentally widen it."""
    with Capture(http_ok=True) as cap:
        jb._notify_duty_after_inform_watch("SUCCESS", {"mode": "vpn_conf", "chat_id": REAL_CHAT}, CTX)
    check(cap.every_target() == set(), f"vpn_conf sends no duty callback (got {cap.every_target()!r})")


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
