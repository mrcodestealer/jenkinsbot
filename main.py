import json
import logging
import os
from typing import Any, Dict, Optional

import requests
from flask import Flask, jsonify, request


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jenkinsbot")

app = Flask(__name__)

# 国内飞书用 https://open.feishu.cn；国际 Lark 用 https://open.larksuite.com
# Feishu (China): https://open.feishu.cn | Lark (intl): https://open.larksuite.com
LARK_HOST = os.getenv("LARK_HOST", "https://open.feishu.cn").rstrip("/")
VERIFICATION_TOKEN = os.getenv(
    "VERIFICATION_TOKEN", "DwMDDJluT9vFnQUxGxvxBcRbhODKPlah"
)
APP_ID = os.getenv("APP_ID", "cli_a97610f57db85ed2")
APP_SECRET = os.getenv("APP_SECRET", "wkC8KYe3nR5YLkn3xJW3lglyoEVMzAMF")
PORT = int(os.getenv("PORT", "5008"))


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


def _is_event_delivery(payload: Dict[str, Any]) -> bool:
    """飞书 Schema 2.0 只有 schema+header+event，没有顶层 type=event_callback。"""
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

    # URL verification when configuring webhook in Lark/Feishu.
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

    # Only process text messages.
    if message_type != "text" or not message_id:
        return jsonify({"ok": True, "ignored": "non_text_or_missing_message_id"})

    text = _extract_text_message(event)
    logger.info(
        "im.message.receive_v1 message_id=%s chat_type=%s text=%r",
        message_id,
        event.get("message", {}).get("chat_type"),
        text[:300] if text else text,
    )

    if "hi" in text.lower():
        success = _reply_text(message_id, "hi")
        if success:
            logger.info("replied hi to message_id=%s", message_id)
        else:
            logger.error(
                "reply failed (check LARK_HOST feishu.cn vs larksuite.com, scopes, logs above)"
            )
        return jsonify({"ok": success})

    return jsonify({"ok": True, "ignored": "text_not_hi"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
