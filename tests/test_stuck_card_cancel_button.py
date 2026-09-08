"""The stuck card warns but never stops monitoring; only its button does.

Run with ``python tests/test_stuck_card_cancel_button.py``. No network, no Jenkins, no Lark.

Why this file exists
--------------------
"Jenkins 日志可能卡住" is a WARNING, not a verdict: a long-running step (a big DB migration, a slow
artifact upload) legitimately prints nothing for an hour, and killing the watch there means the
finish notification — and the customer's done-reply email — never fires. So the default must be
"keep monitoring", and stopping must be an explicit human choice.

These tests pin that contract from both sides:

* the stuck path leaves the watch running and offers a cancel button (default = keep watching);
* tapping that button, and only that, makes the watcher exit.
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
):
    os.environ.setdefault(_k, _v)

import main as jb  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILURES.append(msg)


def buttons_in(card: dict) -> list[dict]:
    out: list[dict] = []

    def walk(els):
        for el in els or []:
            if not isinstance(el, dict):
                continue
            if el.get("tag") == "button":
                out.append(el)
            for col in el.get("columns") or []:
                walk(col.get("elements"))
            if el.get("elements"):
                walk(el["elements"])

    walk((card.get("body") or {}).get("elements"))
    return out


print("1) default is 1 hour, not 10 minutes")
check(jb.STUCK_SECONDS == 3600, f"STUCK_SECONDS == 3600 (got {jb.STUCK_SECONDS})")

print("2) stuck card carries a cancel button and says monitoring continues")
sent: list[dict] = []
jb._emit_message = lambda kind, body, **kw: (sent.append({"kind": kind, "body": body, **kw}), True)[1]
jb._tag_user_at_card = lambda: "@duty"
jb._send_stuck_card("tail-log-text", chat_id="oc_X", reply_message_id="om_root",
                    job_base="https://j/job/FPMS", build=737)
check(len(sent) == 1 and sent[0]["kind"] == "interactive", "one interactive card sent")
card = sent[0]["body"]
check(card.get("schema") == "2.0", "card is schema 2.0 (callback buttons need it)")
md = card["body"]["elements"][0]["text"]["content"]
check("监控仍在继续" in md, "card states monitoring continues")
check("60 分钟" in md, "card shows the threshold in minutes")
check("tail-log-text" in md, "card includes the log tail")
btns = buttons_in(card)
check(len(btns) == 1, f"exactly one button (got {len(btns)})")
val = (btns[0].get("behaviors") or [{}])[0].get("value") or {}
check(val.get("k") == "jk_watch_cancel", f"button k=jk_watch_cancel (got {val.get('k')!r})")
tok = str(val.get("t") or "")
check(bool(tok), "button carries a cancel token")
check("job/FPMS" not in str(val), "long job URL is NOT embedded in the payload")
check(sent[0].get("reply_message_id") == "om_root", "card threads under the original message")

print("3) sending the card does NOT cancel anything by itself (default = keep monitoring)")
check(not jb._watch_is_cancelled("https://j/job/FPMS", 737), "watch not cancelled after stuck card")

print("4) tapping the button flags exactly that build")
key = jb._watch_cancel_request(tok)
check(key == ("https://j/job/FPMS", 737), f"token resolves to the build (got {key!r})")
check(jb._watch_is_cancelled("https://j/job/FPMS", 737), "that build is now cancelled")
check(not jb._watch_is_cancelled("https://j/job/FPMS", 738), "a different build is unaffected")
check(not jb._watch_is_cancelled("https://j/job/OTHER", 737), "a different job is unaffected")

print("5) unknown / reused token is rejected, not silently accepted")
check(jb._watch_cancel_request("deadbeefdead") is None, "unknown token -> None")
check(jb._watch_cancel_request("") is None, "empty token -> None")

print("6) cleanup lets a re-run of the same build start fresh")
jb._watch_cancel_clear("https://j/job/FPMS", 737)
check(not jb._watch_is_cancelled("https://j/job/FPMS", 737), "cancel state cleared")
check(jb._watch_cancel_request(tok) is None, "token invalidated after cleanup")

print("7) no button when the caller has no build context (card still sends)")
sent.clear()
jb._send_stuck_card("t", chat_id="oc_X")
check(len(buttons_in(sent[0]["body"])) == 0, "no cancel button without job/build")

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    sys.exit(1)
print("ALL_TESTS_PASSED")
