import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jenkinsbot")

app = Flask(__name__)

# 国内飞书 | Lark intl
LARK_HOST = os.getenv("LARK_HOST", "https://open.feishu.cn").rstrip("/")
VERIFICATION_TOKEN = os.getenv(
    "VERIFICATION_TOKEN", "DwMDDJluT9vFnQUxGxvxBcRbhODKPlah"
)
APP_ID = os.getenv("APP_ID", "cli_a97610f57db85ed2")
APP_SECRET = os.getenv("APP_SECRET", "wkC8KYe3nR5YLkn3xJW3lglyoEVMzAMF")
PORT = int(os.getenv("PORT", "5008"))

# Jenkins Basic Auth（建议用环境变量覆盖）
JENKINS_USER = os.getenv("JENKINS_USER", "junchen")
JENKINS_PASSWORD = os.getenv("JENKINS_PASSWORD", "junchen")

# 完成后 / 卡住时通知的群与用户（open_id）
NOTIFY_CHAT_ID = os.getenv(
    "NOTIFY_CHAT_ID", "oc_9de3d63fc589df6feeb9b0bee9c45b72"
)
NOTIFY_USER_OPEN_ID = os.getenv(
    "NOTIFY_USER_OPEN_ID", "ou_d7bc33724e2d6ced4050c944c2ca5650"
)

POLL_SECONDS = int(os.getenv("JENKINS_POLL_SECONDS", "10"))
STUCK_SECONDS = int(os.getenv("JENKINS_STUCK_SECONDS", "300"))

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


def _send_done_card(result: str, console_tail: str) -> None:
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
            "title": {"tag": "plain_text", "content": f"Jenkins Finished: {result}"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"{at}\n**done update kindly check**\n\n"
                        f"状态：**{result}**\n\n```\n{tail}\n```"
                    ),
                },
            }
        ],
    }
    ok = _send_chat_message(NOTIFY_CHAT_ID, "interactive", card)
    logger.info("send_done_card interactive ok=%s result=%s", ok, result)


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
            logger.info("jenkins finished %s build=%s", result, build)
            _send_done_card(result, text or "")
            return

        if (not stuck_sent) and (now - unchanged_since) >= STUCK_SECONDS:
            stuck_sent = True
            tail = (text or "")[-1500:]
            logger.warning("jenkins stuck build=%s", build)
            _send_stuck_card(tail)

        time.sleep(POLL_SECONDS)


def _start_jenkins_watch_from_url(
    url: str, message_text: str = ""
) -> Tuple[str, Optional[int]]:
    """
    仅监控你指定的构建号（URL 末尾 /680/ 或链接后单独写 680）。
    不再使用 lastBuild。
    返回: ("ok", build) | ("no_build_number", None) | ("build_not_found", build)
    """
    job_base, build = _parse_job_base_and_build(url)
    if build is None:
        explicit = _explicit_build_after_url(message_text, url)
        if explicit is not None:
            build = explicit
            logger.info("jenkins using explicit build from message: %s", build)

    if build is None:
        logger.error("no build number provided for job %s", job_base)
        return "no_build_number", None

    auth = (JENKINS_USER, JENKINS_PASSWORD)
    if not _jenkins_build_exists(job_base, build, auth):
        logger.error("jenkins build #%s not found under %s", build, job_base)
        return "build_not_found", build

    t = threading.Thread(
        target=_jenkins_watch_worker,
        args=(job_base, build),
        daemon=True,
        name=f"jenkins-watch-{build}",
    )
    t.start()
    return "ok", build


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
        status, build_no = _start_jenkins_watch_from_url(url, text)
        if status == "ok":
            _reply_text(
                message_id,
                f"已开始后台监控 Jenkins #{build_no} 的 console（每 "
                f"{POLL_SECONDS}s 拉取）。结束或长时间无变化时会发到目标群并 @ 指定同事。",
            )
            return jsonify({"ok": True, "jenkins_watch": "started", "build": build_no})
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
