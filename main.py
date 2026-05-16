import json
import logging
import os
import re
import threading
import time
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

app = Flask(__name__)


def _env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} in .env")
    return value


# 国内飞书 | Lark intl — 全部从 .env 读取
LARK_HOST = _env("LARK_HOST").rstrip("/")
VERIFICATION_TOKEN = _env("VERIFICATION_TOKEN")
APP_ID = _env("APP_ID")
APP_SECRET = _env("APP_SECRET")
PORT = int(_env("PORT"))

JENKINS_USER = _env("JENKINS_USER")
JENKINS_PASSWORD = _env("JENKINS_PASSWORD")

NOTIFY_CHAT_ID = _env("NOTIFY_CHAT_ID")
NOTIFY_USER_OPEN_ID = _env("NOTIFY_USER_OPEN_ID")

_POLL_RAW = _env("JENKINS_POLL_SECONDS")
try:
    POLL_SECONDS = max(0.3, float(_POLL_RAW))
except ValueError:
    POLL_SECONDS = 1.0

STUCK_SECONDS = int(_env("JENKINS_STUCK_SECONDS"))

_FINISHED_RE = re.compile(
    r"Finished:\s*(SUCCESS|FAILURE|ABORTED)\s*$", re.MULTILINE
)
_URL_RE = re.compile(r"https?://[^\s<>'\"{}|\\^`\[\]]+", re.IGNORECASE)


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


def _send_done_card(
    result: str,
    console_tail: str,
    *,
    pipeline: str,
    environment: str,
    build: int,
    build_url: str,
) -> None:
    template = "green"
    if result == "FAILURE":
        template = "red"
    elif result == "ABORTED":
        template = "grey"

    # 飞书卡片 lark_md：使用 <at id=ou_xxx></at>（与 user_id 写法不同）
    at = f"<at id={NOTIFY_USER_OPEN_ID}></at>"
    tail = console_tail[-800:] if console_tail else ""
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
                    "content": (
                        f"{at}\n**done update kindly check**\n\n"
                        f"- **Environment：** {environment}\n"
                        f"- **Pipeline：** {pipeline}\n"
                        f"- **Build：** #{build}\n"
                        f"- **状态：** {result}\n"
                        f"- **链接：** {build_url}\n\n"
                        f"```\n{tail}\n```"
                    ),
                },
            }
        ],
    }
    ok = _send_chat_message(NOTIFY_CHAT_ID, "interactive", card)
    logger.info(
        "send_done_card interactive ok=%s result=%s env=%s pipeline=%s build=%s",
        ok,
        result,
        environment,
        pipeline,
        build,
    )


def _send_stuck_card(last_snippet: str) -> None:
    at = f"<at id={NOTIFY_USER_OPEN_ID}></at>"
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
    ok = _send_chat_message(NOTIFY_CHAT_ID, "interactive", card)
    logger.info("send_stuck_card ok=%s", ok)


def _extract_urls(text: str) -> List[str]:
    return _URL_RE.findall(text or "")


def _is_jenkins_job_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return "/job/" in (p.path or "")
    except Exception:
        return False


def _parse_job_base_and_build(url: str) -> Tuple[str, Optional[int]]:
    """返回 (job_base_url 以 / 结尾, 可选 build 号)。若 URL 未带 build，则 build 为 None。"""
    raw = url.strip().rstrip("/")
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


def _jenkins_build_exists(
    job_base: str, build: int, auth: Tuple[str, str]
) -> bool:
    """确认历史构建号存在（对应 Build History 里那一次）。"""
    url = f"{job_base.rstrip('/')}/{build}/api/json"
    try:
        r = requests.get(url, auth=auth, timeout=20)
        if r.status_code != 200:
            logger.warning("build api/json HTTP %s for #%s", r.status_code, build)
        return r.status_code == 200
    except Exception as exc:
        logger.warning("build exists check failed: %s", exc)
        return False


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


def _jenkins_watch_worker(job_base: str, build: int) -> None:
    auth = (JENKINS_USER, JENKINS_PASSWORD)
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
            )
            return

        if (not stuck_sent) and (now - unchanged_since) >= STUCK_SECONDS:
            stuck_sent = True
            tail = (text or "")[-1500:]
            logger.warning("jenkins stuck build=%s", build)
            _send_stuck_card(tail)

        time.sleep(POLL_SECONDS)


def _start_jenkins_watch_from_url(
    url: str, message_text: str = ""
) -> Tuple[str, Optional[int], str, Optional[str]]:
    """
    仅监控你指定的构建号（URL 末尾 /680/ 或链接后单独写 680）。
    不再使用 lastBuild。
    返回: (status, build, pipeline, path_env_hint)
    """
    job_base, build = _parse_job_base_and_build(url)
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

    auth = (JENKINS_USER, JENKINS_PASSWORD)
    if not _jenkins_build_exists(job_base, build, auth):
        logger.error("jenkins build #%s not found under %s", build, job_base)
        return "build_not_found", build, pipeline, path_env

    t = threading.Thread(
        target=_jenkins_watch_worker,
        args=(job_base, build),
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

    if message_type != "text" or not message_id:
        return jsonify({"ok": True, "ignored": "non_text_or_missing_message_id"})

    text = _extract_text_message(event)
    logger.info(
        "im.message.receive_v1 message_id=%s chat_type=%s text=%r",
        message_id,
        event.get("message", {}).get("chat_type"),
        text[:300] if text else text,
    )

    jenkins_urls = [u for u in _extract_urls(text) if _is_jenkins_job_url(u)]
    if jenkins_urls:
        url = jenkins_urls[0]
        status, build_no, pipeline, path_env = _start_jenkins_watch_from_url(url, text)
        if status == "ok":
            _poll_hint = (
                str(int(POLL_SECONDS))
                if float(POLL_SECONDS).is_integer()
                else str(POLL_SECONDS)
            )
            env_hint = f"，Environment（路径推测）：{path_env}" if path_env else ""
            _reply_text(
                message_id,
                f"已开始后台监控 Jenkins #{build_no}（Pipeline：{pipeline}{env_hint}）的 "
                f"console（约每 {_poll_hint}s 拉取完整日志，尽快检测 Finished）。"
                "结束后会在目标群通知 Environment / Pipeline / 状态。",
            )
            return jsonify(
                {
                    "ok": True,
                    "jenkins_watch": "started",
                    "build": build_no,
                    "pipeline": pipeline,
                    "path_env": path_env,
                }
            )
        if status == "no_build_number":
            _reply_text(
                message_id,
                "请指定历史构建号：链接末尾带 /680/，或在链接后空格写 680。"
                "不再自动使用最新一次构建（last build）。",
            )
            return jsonify({"ok": False, "jenkins_watch": "no_build_number"})
        _reply_text(
            message_id,
            f"构建 #{build_no} 在该 Job 下不存在或无权限查看，请核对 Build History 里的号码。",
        )
        return jsonify({"ok": False, "jenkins_watch": "build_not_found"})

    return jsonify({"ok": True, "ignored": "no_command"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
