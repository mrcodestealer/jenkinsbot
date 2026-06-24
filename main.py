import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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
NOTIFY_USER_OPEN_ID = _env("NOTIFY_USER_OPEN_ID")
DUTY_BOT_OPEN_ID = (os.getenv("DUTY_BOT_OPEN_ID") or "ou_1f6596a9923a2a835918e7e2513595d5").strip()

_POLL_RAW = _env("JENKINS_POLL_SECONDS")
try:
    POLL_SECONDS = max(0.3, float(_POLL_RAW))
except ValueError:
    POLL_SECONDS = 1.0

STUCK_SECONDS = int(_env("JENKINS_STUCK_SECONDS") or "600")

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

    token_url = f"{LARK_HOST}/open-apis/auth/v3/tenant_access_token/internal"
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

    reply_url = f"{LARK_HOST}/open-apis/im/v1/messages/{message_id}/reply"
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


def _send_chat_message(
    chat_id: str, msg_type: str, content_obj: Dict[str, Any]
) -> bool:
    token = _get_tenant_access_token()
    if not token:
        return False

    url = f"{LARK_HOST}/open-apis/im/v1/messages?receive_id_type=chat_id"
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
        return False
    if data.get("code") != 0:
        logger.error("send_chat_message API error: %s", data)
        return False
    return True


def _reply_in_thread_message(
    message_id: str, msg_type: str, content_obj: Dict[str, Any]
) -> bool:
    """Reply to ``message_id`` **inside its thread** (reply_in_thread=true). Used for VPN so all
    jenkinsbot output lands under the user's original ``create vpn`` message thread."""
    mid = (message_id or "").strip()
    if not mid:
        return False
    token = _get_tenant_access_token()
    if not token:
        return False
    url = f"{LARK_HOST}/open-apis/im/v1/messages/{mid}/reply"
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
        return False
    if data.get("code") != 0:
        logger.error("reply_in_thread API error: %s", data)
        return False
    return True


def _emit_message(
    msg_type: str,
    content_obj: Dict[str, Any],
    *,
    chat_id: str,
    reply_message_id: Optional[str] = None,
) -> bool:
    """Thread-reply under ``reply_message_id`` when given, else plain send to ``chat_id``."""
    rmid = (reply_message_id or "").strip()
    if rmid and _reply_in_thread_message(rmid, msg_type, content_obj):
        return True
    return _send_chat_message(chat_id, msg_type, content_obj)


def _jenkins_console_text_url(job_base: str, build: int) -> str:
    return f"{job_base.rstrip('/')}/{build}/consoleText"


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
) -> None:
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    template = "green"
    if result == "FAILURE":
        template = "red"
    elif result == "UNSTABLE":
        template = "orange"
    elif result == "ABORTED":
        template = "grey"

    # Only @-mention the fixed duty user when posting to the default notify group (not in threads).
    at = (
        f"<at id={NOTIFY_USER_OPEN_ID}></at>"
        if target_chat == NOTIFY_CHAT_ID and not (reply_message_id or "").strip()
        else ""
    )
    if vpn_mode:
        summary = (
            "**done created vpn**"
            if result == "SUCCESS"
            else f"**VPN build {result}**"
        )
        body = (
            f"{at}\n{summary}\n\n"
            f"- **Environment：** {environment}\n"
            f"- **Pipeline：** {pipeline}\n"
            f"- **Build：** #{build}\n"
            f"- **状态：** {result}\n"
            f"- **链接：** {build_url}\n"
            f"- **Logs :** {console_text_url}"
        )
    else:
        tail = _console_last_lines(console_tail, max_lines=10)
        body = (
            f"{at}\n**done update kindly check**\n\n"
            f"- **Environment：** {environment}\n"
            f"- **Pipeline：** {pipeline}\n"
            f"- **Build：** #{build}\n"
            f"- **状态：** {result}\n"
            f"- **链接：** {build_url}\n"
            f"- **Logs :** {console_text_url}\n\n"
            f"```\n{tail}\n```"
        )
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": (
                    f"Jenkins Finished: {result} | {environment} / {pipeline}"
                ),
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": body,
                },
            }
        ],
    }
    ok = _emit_message(
        "interactive", card, chat_id=target_chat, reply_message_id=reply_message_id
    )
    logger.info(
        "send_done_card interactive ok=%s result=%s env=%s pipeline=%s build=%s chat=%s thread=%s",
        ok,
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


def _send_stuck_card(
    last_snippet: str,
    *,
    chat_id: Optional[str] = None,
    reply_message_id: Optional[str] = None,
) -> None:
    target_chat = (chat_id or "").strip() or NOTIFY_CHAT_ID
    at = (
        f"<at id={NOTIFY_USER_OPEN_ID}></at>"
        if target_chat == NOTIFY_CHAT_ID and not (reply_message_id or "").strip()
        else ""
    )
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


def _send_duty_text(text: str) -> bool:
    plain = (text or "").strip()
    if not plain:
        logger.warning("empty duty notify text — skip")
        return False
    duty = (DUTY_BOT_OPEN_ID or "").strip()
    # Prefer **@tagging** the duty bot (per request). The command text is still present, so the
    # duty bot recognizes it by command even if Lark drops the bot→bot mention in a group.
    if duty:
        at = f'<at user_id="{duty}">duty bot</at>'
        if _send_chat_message(NOTIFY_CHAT_ID, "text", {"text": f"{at} {plain}".strip()}):
            logger.info("duty notify sent (@tag): %r", plain[:120])
            return True
        logger.warning("duty @tag send failed — retrying plain")
    else:
        logger.warning("DUTY_BOT_OPEN_ID missing — sending duty command without @tag")
    if _send_chat_message(NOTIFY_CHAT_ID, "text", {"text": plain}):
        logger.info("duty notify sent (plain): %r", plain[:120])
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


def _notify_duty_updatemore_callback_http(command: str) -> bool:
    """POST to duty bot — reliable when Lark skips bot→bot group delivery."""
    cmd = (command or "").strip()
    if not cmd:
        return False
    url = _duty_updatemore_callback_url()
    token = (os.getenv("DUTY_INTERNAL_TOKEN") or "").strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["X-Duty-Internal-Token"] = token
    payload = {"chat_id": NOTIFY_CHAT_ID, "command": cmd}
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
    title: str, pipeline: str, when: str
) -> bool:
    """POST to duty bot — reliable when Lark skips bot→bot group delivery."""
    url = _duty_reply_update_url()
    token = (os.getenv("DUTY_INTERNAL_TOKEN") or "").strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["X-Duty-Internal-Token"] = token
    payload = {
        "chat_id": NOTIFY_CHAT_ID,
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
    if result != "SUCCESS":
        if _notify_duty_updatemore_callback_http("/FailedStop"):
            logger.info("duty bot notified via HTTP for /FailedStop")
            return
        _send_duty_text("/FailedStop")
        return
    if meta.get("mode") == "inform_time":
        title = (meta.get("email_title") or "").strip()
        pipeline = (ctx.get("pipeline") or "").strip()
        when = _format_local_time_pm()
        cmd = f"/replyupdateemail | {title} | {pipeline} | {when}".strip()
        if _notify_duty_reply_update_email_http(title, pipeline, when):
            logger.info("duty bot notified via HTTP for email=%r", title)
            return
        if _send_duty_text(cmd):
            logger.info("duty bot notified via Lark for email=%r", title)
            return
        warn = (
            f"⚠️ Jenkins build SUCCESS but **could not reach duty bot** for email reply.\n"
            f"Run manually: `{cmd}`"
        )
        logger.error("duty notify failed — %s", cmd)
        _send_chat_message(NOTIFY_CHAT_ID, "text", {"text": warn})
    elif meta.get("mode") == "inform":
        if _notify_duty_updatemore_callback_http("/SuccessProceedNext"):
            logger.info("duty bot notified via HTTP for /SuccessProceedNext")
            return
        if not _send_duty_text("/SuccessProceedNext"):
            _send_chat_message(
                NOTIFY_CHAT_ID,
                "text",
                {"text": "⚠️ Could not reach duty bot for `/SuccessProceedNext`"},
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


def _upload_file_lark(path: str, file_name: str) -> Optional[str]:
    token = _get_tenant_access_token()
    if not token:
        return None
    url = f"{LARK_HOST}/open-apis/im/v1/files"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with open(path, "rb") as fh:
            files = {
                "file_type": (None, "stream"),
                "file_name": (None, file_name),
                "file": (file_name, fh, "application/octet-stream"),
            }
            resp = requests.post(url, headers=headers, files=files, timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("upload_file_lark failed: %s", exc)
        return None
    if data.get("code") != 0:
        logger.error("upload_file_lark API error: %s", data)
        return None
    return (data.get("data") or {}).get("file_key")


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
            if isinstance(meta, dict) and meta.get("mode") in ("inform", "inform_time"):
                _notify_duty_after_inform_watch(result, meta, ctx)
            if isinstance(meta, dict) and meta.get("mode") == "vpn_conf":
                try:
                    _handle_vpn_conf_after_success(result, job_base, build, meta)
                except Exception as exc:
                    logger.exception("vpn conf handling failed: %s", exc)
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

    t = threading.Thread(
        target=_jenkins_watch_worker,
        args=(job_base, build),
        kwargs={"meta": watch_meta},
        daemon=True,
        name=f"jenkins-watch-{build}",
    )
    t.start()
    if isinstance(meta, dict) and meta.get("mode"):
        with _watch_meta_lock:
            _watch_meta[(job_base, build)] = dict(meta)
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


def _process_message_command(text: str, message_id: str, event_chat_id: str) -> None:
    """Heavy work (build-exists checks, replies, watch start) — runs off the webhook thread so
    Lark gets a fast 200 and does not retry (retries previously caused minutes-long delays)."""
    try:
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


@app.post("/webhook/event")
def webhook_event():
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
        args=(text, message_id, event_chat_id),
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


if __name__ == "__main__":
    if "--testaccess" in sys.argv:
        raise SystemExit(_run_testaccess_cli())
    app.run(host="0.0.0.0", port=PORT)
