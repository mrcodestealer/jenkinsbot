"""One watcher per (build, mode), and a duty notification that can never fail in silence.

Run with ``python3 tests/test_watch_dedupe_and_notify_failures.py``. No network, no Jenkins.

Why this file exists
--------------------
FPMS_PROD_SCRIPT_RUN #737 produced TWO ``/SuccessProceedNext`` POSTs eight seconds apart. Both were
harmless that day because the duty bot had no queue and answered 409 — but on a run that DOES have
one, the second proceed advances the queue past a segment that has not built yet. ``main.py``
already documents that exact failure at its websocket dedupe, and that dedupe is not enough: it
suppresses a REDELIVERY of one event, while two genuinely distinct inform messages for the same
build each started their own watcher, because ``_start_jenkins_watch_from_url`` had no check at all.

The same incident showed the notification path failing invisibly: both sends inside
``_send_duty_text`` failed, and the only trace was whatever ``_send_chat_message`` logged one frame
down. If the ⚠️ warning also failed to send, the completion reached nobody and nothing said so.

The two tests below pin opposite risks, and the second is the expensive one:

* a duplicate watcher double-advances a real queue;
* deduping too aggressively DROPS a watch — and dropping an ``inform_time`` watch means a
  customer's done-reply email is never sent. Hence ``mode`` is part of the dedupe key.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main.py reads its whole config at import time (and raises on anything missing) but starts
# nothing on import. Same placeholders as tests/test_duty_callback_chat.py.
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

JOB = "https://jenkins.invalid/job/FPMS/job/FPMS_PROD_SCRIPT_RUN"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


class _Harness:
    """Replace the pieces that would touch Jenkins/Lark, and record what the watcher did."""

    def __init__(self):
        self.started: list[tuple] = []
        self.release = threading.Event()
        self._saved = {}

    def __enter__(self):
        self._saved = {
            n: getattr(jb, n)
            for n in ("_resolve_jenkins_auth", "_jenkins_watch_worker")
        }
        jb._resolve_jenkins_auth = lambda job_base, build: (("u", "p"), True, 200)

        def fake_worker(job_base, build, meta=None):
            self.started.append((job_base, build, (meta or {}).get("mode")))
            # Stay alive so the slot is genuinely held while the second request arrives.
            self.release.wait(timeout=5)

        jb._jenkins_watch_worker = fake_worker
        with jb._watch_meta_lock:
            jb._active_watches.clear()
        return self

    def __exit__(self, *a):
        self.release.set()
        time.sleep(0.05)
        for n, fn in self._saved.items():
            setattr(jb, n, fn)
        with jb._watch_meta_lock:
            jb._active_watches.clear()
        return False


def _start(mode: str):
    return jb._start_jenkins_watch_from_url(
        f"{JOB}/737/", f"/SuccessInformMe {JOB}/737/",
        meta={"mode": mode, "chat_id": "oc_test"},
    )


def test_a_second_watch_on_the_same_build_and_mode_is_not_started():
    with _Harness() as h:
        s1 = _start("inform")
        time.sleep(0.05)
        s2 = _start("inform")
        time.sleep(0.05)
        check(s1[0] == "ok", f"first watch should start: {s1!r}")
        check(s2[0] == "ok", f"the duplicate must still ACK, not error: {s2!r}")
        check(
            len(h.started) == 1,
            f"exactly ONE watcher may run for a (build, mode); got {len(h.started)} — a second "
            "fires the duty notification again and advances the queue past an unbuilt segment",
        )


def test_a_different_mode_on_the_same_build_still_gets_its_own_watcher():
    """The expensive direction: dropping an inform_time watch loses a customer email."""
    with _Harness() as h:
        _start("inform")
        time.sleep(0.05)
        _start("inform_time")
        time.sleep(0.05)
        modes = sorted(m for _j, _b, m in h.started)
        check(
            modes == ["inform", "inform_time"],
            f"both modes must run — they do different jobs; got {modes!r}",
        )


def test_a_finished_watcher_frees_its_slot():
    """Released in a `finally`, so a watcher that dies any other way does not block the build."""
    with _Harness() as h:
        _start("inform")
        time.sleep(0.05)
        h.release.set()          # let the first worker return
        time.sleep(0.15)
        _start("inform")
        time.sleep(0.05)
        check(
            len(h.started) == 2,
            f"the same build must be watchable again once the first watcher ends; got "
            f"{len(h.started)} start(s)",
        )


def test_the_slot_is_claimed_before_the_thread_starts():
    """Registering after `t.start()` leaves a window where a second inform sees nothing."""
    import inspect
    import re

    src = inspect.getsource(jb._start_jenkins_watch_from_url)
    code = re.sub(r'"""(?:.|\n)*?"""', "", src)
    add_at = code.find("_active_watches.add(")
    start_at = code.find(".start()")
    check(add_at > 0, "the watcher must claim a slot")
    check(
        0 < add_at < start_at,
        "the slot must be claimed BEFORE the thread starts, or two informs arriving together "
        "both see an unclaimed build",
    )
    check(
        "finally:" in code and "_active_watches.discard(" in code,
        "and released in a finally, so a worker that exits any other way frees it",
    )


def test_a_failed_duty_notification_is_never_silent():
    """Both sends failing used to leave no record that a notification was even attempted."""
    logged: list[str] = []
    sent: list[str] = []
    saved = {
        n: getattr(jb, n)
        for n in ("_notify_duty_updatemore_callback_http", "_send_duty_text", "_send_chat_message")
    }
    orig_error = jb.logger.error
    try:
        jb._notify_duty_updatemore_callback_http = lambda cmd, chat=None: False  # the 409
        jb._send_duty_text = lambda text, chat=None: False                       # Lark fails too
        jb._send_chat_message = lambda chat, mt, obj: (
            sent.append(str(obj.get("text", ""))), False
        )[1]
        jb.logger.error = lambda msg, *a, **k: logged.append(str(msg) % a if a else str(msg))

        jb._notify_duty_after_inform_watch(
            "SUCCESS", {"mode": "inform", "chat_id": "oc_test"}, {"pipeline": "P"}
        )
    finally:
        jb.logger.error = orig_error
        for n, fn in saved.items():
            setattr(jb, n, fn)

    check(
        any("duty notify FAILED" in m for m in logged),
        f"an unreachable duty bot must log an ERROR naming the failure; got {logged!r}",
    )
    check(
        any("queue" in m.lower() for m in logged),
        "and must say the consequence — the queue will not advance on its own",
    )
    check(
        any("Could not reach duty bot" in s for s in sent),
        f"it must still try to tell the chat; got {sent!r}",
    )
    check(
        any("reached nobody" in m for m in logged),
        "and when even that send fails, say so — otherwise the completion vanishes silently",
    )


def test_a_failed_build_that_cannot_be_reported_is_also_loud():
    logged: list[str] = []
    sent: list[str] = []
    saved = {
        n: getattr(jb, n)
        for n in ("_notify_duty_updatemore_callback_http", "_send_duty_text", "_send_chat_message")
    }
    orig_error = jb.logger.error
    try:
        jb._notify_duty_updatemore_callback_http = lambda cmd, chat=None: False
        jb._send_duty_text = lambda text, chat=None: False
        jb._send_chat_message = lambda chat, mt, obj: (
            sent.append(str(obj.get("text", ""))), True
        )[1]
        jb.logger.error = lambda msg, *a, **k: logged.append(str(msg) % a if a else str(msg))

        jb._notify_duty_after_inform_watch(
            "FAILURE", {"mode": "inform", "chat_id": "oc_test"}, {"pipeline": "P"}
        )
    finally:
        jb.logger.error = orig_error
        for n, fn in saved.items():
            setattr(jb, n, fn)

    check(
        any("/FailedStop" in m for m in logged),
        f"a dropped /FailedStop must be logged — the queue waits on a build that already "
        f"failed; got {logged!r}",
    )
    check(
        any("FAILED" in s for s in sent),
        f"and the chat must be told; got {sent!r}",
    )


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        print(f"- {fn.__name__}")
        try:
            fn()
        except Exception:
            _FAILURES.append(f"{fn.__name__} raised")
            traceback.print_exc()
    print(f"\n{_RUN} checks, {len(_FAILURES)} failure(s)")
    for f in _FAILURES:
        print(f"  FAILED: {f}")
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
