"""Reactions on the Jenkins watch commands, and /secret1 open_id lookup.

Run with ``python tests/test_watch_reactions_and_secret1.py``. No network, no Jenkins, no Lark.

Why this file exists
--------------------
A build watch finishes minutes or hours after the command, on another thread. The reaction_id
needed to take the "working on it" emoji off again therefore has to travel with the watcher
inside ``meta`` — and ``_start_jenkins_watch_from_url`` copies ``meta``, so a key added after
that call would be silently lost.

Three ways this leaves an emoji stuck on "working on it" forever, which reads as a build that
never finished:

* the watch never starts (bad build number, no permission) — nothing downstream to resolve it;
* the command is a duplicate of a build already being watched, where
  ``_start_jenkins_watch_from_url`` returns ``"ok"`` WITHOUT starting a second watcher;
* the reaction call itself throws, and takes the duty-bot callbacks down with it.
"""

from __future__ import annotations

import inspect
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
    ("JENKINS_STUCK_SECONDS", "600"),
):
    os.environ.setdefault(_k, _v)

import main as jb  # noqa: E402

_FAILURES: list[str] = []
_RUN = 0

ME = "ou_me"
CHAT = "oc_REAL"
MSG = "om_msg"
JOB = "https://jenkins.internal.client8.me/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


class Run:
    """Capture reactions, replies, and the meta each watch was started with."""

    def __init__(self, *, start_status: str = "ok") -> None:
        self.reactions: list[tuple[str, str]] = []
        self.replies: list[str] = []
        self.started: list[dict] = []
        self._status = start_status
        self._saved: dict[str, object] = {}

    def __enter__(self) -> "Run":
        def add(mid, emoji):
            self.reactions.append(("+", emoji))
            return f"rid_{len(self.reactions)}"

        def rm(mid, rid):
            self.reactions.append(("-", rid))
            return True

        def start(url, text, meta=None):
            self.started.append(dict(meta or {}))
            return self._status, 740, "FPMS_PROD_SCRIPT_RUN", "FPMS"

        for name, fn in (
            ("_add_message_reaction_id", add),
            ("_remove_message_reaction", rm),
            ("_start_jenkins_watch_from_url", start),
            ("_reply_in_thread_message", lambda m, t, o: (
                self.replies.append(o.get("text", "")), True)[1]),
            ("_reply_text", lambda m, t: (self.replies.append(t), True)[1]),
            ("_send_chat_message", lambda c, t, o, **k: (
                self.replies.append(o.get("text", "")), True)[1]),
        ):
            self._saved[name] = getattr(jb, name)
            setattr(jb, name, fn)
        return self

    def __exit__(self, *exc) -> None:
        for name, fn in self._saved.items():
            setattr(jb, name, fn)

    @property
    def emojis(self) -> list[str]:
        return [e for _k, e in self.reactions]

    @property
    def text(self) -> str:
        return "\n".join(self.replies)


# --------------------------------------------------------------------------------------------
# Watch commands react
# --------------------------------------------------------------------------------------------
def test_every_watch_command_reacts_working_on_it() -> None:
    """`/SuccessInformMe <url> 740` had no reaction at all — only the deploy command did."""
    cases = [
        ("inform", f"@_user_1 /SuccessInformMe {JOB} 740"),
        ("inform_time", f"@_user_1 /SuccessInformMeTime {JOB} 740 | Some Subject"),
        ("watch", f"@_user_1 {JOB}740/"),
    ]
    for mode, text in cases:
        with Run() as r:
            jb._process_message_command(text, MSG, CHAT, ME, [])
        check(
            r.reactions and r.reactions[0] == ("+", "OnIt"),
            f"{mode}: reacts OnIt on arrival (got {r.reactions!r})",
        )
        check(len(r.started) == 1, f"{mode}: a watch was started")
        if r.started:
            check(
                r.started[0].get("react_message_id") == MSG,
                f"{mode}: the message id travels in meta (got {r.started[0]!r})",
            )
            check(
                bool(r.started[0].get("react_working_id")),
                f"{mode}: the reaction_id travels in meta too",
            )


def test_the_reaction_ids_are_in_meta_before_the_watch_starts() -> None:
    """_start_jenkins_watch_from_url does ``watch_meta = dict(meta)``, so anything added to meta
    AFTER that call never reaches the worker."""
    src = inspect.getsource(jb._process_message_command)
    for begin, start in (
        ("_watch_react_begin(message_id, inform)", "meta=inform"),
        ("_watch_react_begin(message_id, vpn)", "meta=vpn"),
        ("_watch_react_begin(message_id, watch_kw)", "meta=watch_kw"),
    ):
        i, j = src.find(begin), src.find(start)
        check(i >= 0, f"{begin} is present")
        check(i >= 0 and j > i, f"{begin} runs BEFORE the watch is started")


def test_a_finished_build_resolves_the_reaction() -> None:
    for result, want in (
        ("SUCCESS", "DONE"),
        ("FAILURE", "CrossMark"),
        ("UNSTABLE", "CrossMark"),
        ("ABORTED", "CrossMark"),
    ):
        with Run() as r:
            meta = {"react_message_id": MSG, "react_working_id": "rid_1"}
            jb._watch_react_finish(meta, result)
        check(
            r.reactions == [("-", "rid_1"), ("+", want)],
            f"{result}: OnIt removed then {want} (got {r.reactions!r})",
        )
        check(
            "react_message_id" not in meta,
            f"{result}: meta is cleared so it cannot be resolved twice",
        )


def test_a_watch_that_cannot_start_clears_its_reaction() -> None:
    """Otherwise the message sits on "working on it" for a build nobody is watching."""
    with Run(start_status="build_not_found") as r:
        jb._process_message_command(f"@_user_1 /SuccessInformMe {JOB} 999", MSG, CHAT, ME, [])
    check(
        r.emojis == ["OnIt", "rid_1", "CrossMark"],
        f"OnIt then removed then CrossMark (got {r.reactions!r})",
    )
    check("无法监控" in r.text, "and the reply still explains why")


def test_a_duplicate_command_does_not_leave_a_stuck_reaction() -> None:
    """_start_jenkins_watch_from_url returns "ok" for a build already being watched WITHOUT
    starting a second watcher — so nothing downstream would ever resolve this reaction. It is
    not a failure either: the running watcher will report, so just take the emoji back off."""
    src = inspect.getsource(jb._start_jenkins_watch_from_url)
    i_dupe = src.find("already running")
    i_abort = src.find("_watch_react_abort(")
    check(i_dupe >= 0 and i_abort > i_dupe, "the dedupe path clears the reaction")
    check(
        "failed=False" in src[i_abort : i_abort + 80],
        "a duplicate is not reported as a failure",
    )

    with Run() as r:
        meta = {"react_message_id": MSG, "react_working_id": "rid_1"}
        jb._watch_react_abort(meta, failed=False)
    check(
        r.reactions == [("-", "rid_1")],
        f"the emoji is removed and none added (got {r.reactions!r})",
    )


def test_the_reaction_cannot_break_the_duty_callbacks() -> None:
    """The watcher calls this between the done card and _notify_duty_after_inform_watch."""
    saved = jb._react_progress_end

    def boom(*a, **kw):
        raise RuntimeError("lark exploded")

    jb._react_progress_end = boom
    try:
        jb._watch_react_finish({"react_message_id": MSG, "react_working_id": "r"}, "SUCCESS")
        jb._watch_react_abort({"react_message_id": MSG, "react_working_id": "r"}, failed=True)
        check(True, "a throwing reaction is swallowed, not propagated")
    except Exception as exc:  # noqa: BLE001
        _FAILURES.append(f"the reaction exception escaped: {exc!r}")
    finally:
        jb._react_progress_end = saved

    src = inspect.getsource(jb._jenkins_watch_worker)
    i_card = src.find("_send_done_card(")
    i_react = src.find("_watch_react_finish(")
    i_duty = src.find("_notify_duty_after_inform_watch(")
    check(i_card < i_react < i_duty, "resolved after the card, before the duty callback")


def test_a_watch_with_no_message_id_reacts_to_nothing() -> None:
    with Run() as r:
        meta: dict = {}
        jb._watch_react_begin("", meta)
    check(r.reactions == [], "no message id -> no reaction attempt")
    check("react_message_id" not in meta, "and nothing is stashed in meta")


# --------------------------------------------------------------------------------------------
# /secret1
# --------------------------------------------------------------------------------------------
def test_secret1_is_recognised() -> None:
    for raw in ("/secret1", "@_user_1 /secret1", "@_user_1 /secret1 @_user_2", "secret1"):
        check(jb._parse_secret1_command(raw) is True, f"recognised: {raw!r}")
    for raw in ("", "what is the secret1 for this?", "tell me a secret"):
        check(jb._parse_secret1_command(raw) is False, f"must NOT be secret1: {raw!r}")


def test_secret1_reports_the_open_id_of_everyone_mentioned() -> None:
    mentions = [
        {"key": "@_user_1", "id": {"open_id": "ou_bot123"},
         "name": "Jenkins Monitoring Bot", "mentioned_type": "bot"},
        {"key": "@_user_2", "id": {"open_id": "ou_person456"},
         "name": "CP OM Duty", "mentioned_type": "user"},
    ]
    with Run() as r:
        jb._process_message_command("@_user_1 /secret1 @_user_2", MSG, CHAT, ME, mentions)
    for probe in ("ou_bot123", "ou_person456", ME, "Jenkins Monitoring Bot", "CP OM Duty"):
        check(probe in r.text, f"reports {probe!r} (got {r.text!r})")
    check("机器人" in r.text and "用户" in r.text, "distinguishes a bot from a user")


def test_secret1_still_answers_with_no_mentions() -> None:
    """The commonest real use: "what is MY open_id", for DEPLOY_ALLOWED_OPEN_IDS."""
    with Run() as r:
        jb._process_message_command("/secret1", MSG, CHAT, ME, [])
    check(ME in r.text, f"the caller's own open_id is always included (got {r.text!r})")


def test_secret1_survives_a_malformed_mentions_array() -> None:
    for mentions in (None, [], [{}], [{"id": "not-a-dict"}], ["nonsense"], [{"id": {}}]):
        with Run() as r:
            jb._handle_secret1_command(
                chat_id=CHAT, message_id=MSG, sender_id=ME, mentions=mentions  # type: ignore[arg-type]
            )
        check(bool(r.text), f"{mentions!r}: still replies something")
        check(ME in r.text, f"{mentions!r}: still reports the caller")


def test_mentions_reach_the_dispatcher_from_a_real_event() -> None:
    """_event_mentions has to read message.mentions off the event, or /secret1 gets nothing."""
    event = {
        "message": {
            "message_id": MSG,
            "chat_id": CHAT,
            "message_type": "text",
            "content": '{"text":"@_user_1 /secret1"}',
            "mentions": [
                {"key": "@_user_1", "id": {"open_id": "ou_bot123"}, "name": "Bot",
                 "mentioned_type": "bot"}
            ],
        }
    }
    got = jb._event_mentions(event)
    check(len(got) == 1 and got[0]["id"]["open_id"] == "ou_bot123", f"parsed (got {got!r})")
    check(jb._event_mentions({}) == [], "a mention-less event yields an empty list")
    check(jb._event_mentions({"message": {"mentions": "bad"}}) == [], "garbage yields []")


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
