import json
import logging
import os
from typing import Any, Dict, Optional

import requests
from flask import Flask, jsonify, request


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jenkinsbot")

app = Flask(__name__)

LARK_HOST = os.getenv("LARK_HOST", "https://open.larksuite.com").rstrip("/")
APP_ID = os.getenv("APP_ID", "")
APP_SECRET = os.getenv("APP_SECRET", "")
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

    # URL verification when configuring webhook in Lark/Feishu.
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")})

    if payload.get("type") != "event_callback":
        return jsonify({"ok": True, "ignored": "not_event_callback"})

    event_type = payload.get("header", {}).get("event_type", "")
    if event_type != "im.message.receive_v1":
        return jsonify({"ok": True, "ignored": event_type or "unknown"})

    event = payload.get("event", {})
    message = event.get("message", {})
    message_id = message.get("message_id", "")
    message_type = message.get("message_type", "")

    # Only process text messages.
    if message_type != "text" or not message_id:
        return jsonify({"ok": True, "ignored": "non_text_or_missing_message_id"})

    text = _extract_text_message(event)
    if "hi" in text.lower():
        success = _reply_text(message_id, "hi")
        return jsonify({"ok": success})

    return jsonify({"ok": True, "ignored": "text_not_hi"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
