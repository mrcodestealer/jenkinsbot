"""
Lark WebSocket persistent connection → local jenkinsbot webhook.

No public URL needed. In Lark Developer Console set event subscription to
**Persistent connection** (长连接), then:

  python run_local_bot.py

Requires: pip install lark-oapi
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
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


_load_dotenv()

_PORT = (os.getenv("PORT") or "5001").strip() or "5001"


def _local_webhook_url() -> str:
    return (
        os.getenv("LARK_LOCAL_WEBHOOK_URL")
        or f"http://127.0.0.1:{_PORT}/webhook/event"
    ).strip()


def _wait_for_webhook(local_webhook: str, timeout_sec: float = 90.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = requests.get(local_webhook, timeout=2)
            if r.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def _ensure_inbound_message_id(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    ev = payload.get("event")
    if not isinstance(ev, dict):
        return payload
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return payload
    if (msg.get("message_id") or "").strip():
        return payload
    for alt in (ev.get("message_id"),):
        mid = str(alt or "").strip()
        if mid:
            msg["message_id"] = mid
            break
    return payload


def _to_webhook_payload(data, verification_token: str) -> dict:
    import lark_oapi as lark

    raw = json.loads(lark.JSON.marshal(data))
    if isinstance(raw, dict) and "header" in raw and "event" in raw:
        payload = dict(raw)
        hdr = dict(payload.get("header") or {})
        payload["header"] = hdr
    else:
        inner = raw.get("event", raw) if isinstance(raw, dict) else raw
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": str(uuid.uuid4()),
                "event_type": "im.message.receive_v1",
                "create_time": str(int(time.time() * 1000)),
            },
            "event": inner,
        }

    if verification_token:
        hdr = payload.setdefault("header", {})
        if not str(hdr.get("token") or "").strip():
            hdr["token"] = verification_token
    payload = _ensure_inbound_message_id(payload)
    return payload


def _on_message(data, local_webhook: str, verification_token: str) -> None:
    try:
        payload = _to_webhook_payload(data, verification_token)
        r = requests.post(local_webhook, json=payload, timeout=300)
        print(f"[jenkins-ws] forwarded → {local_webhook} status={r.status_code}", flush=True)
    except Exception as exc:
        print(f"[jenkins-ws] forward failed: {exc!r}", flush=True)


def run_forever(local_webhook_url: str | None = None) -> None:
    import lark_oapi as lark

    app_id = (os.getenv("APP_ID") or "").strip()
    app_secret = (os.getenv("APP_SECRET") or "").strip()
    verification_token = (os.getenv("VERIFICATION_TOKEN") or "").strip()
    local_webhook = (local_webhook_url or _local_webhook_url()).strip()

    if not app_id or not app_secret:
        print("[jenkins-ws] Set APP_ID and APP_SECRET in .env", file=sys.stderr)
        sys.exit(1)

    if not _wait_for_webhook(local_webhook):
        print(
            f"[jenkins-ws] Local webhook not ready at {local_webhook} — Flask did not start",
            file=sys.stderr,
        )
        sys.exit(1)

    def _handler(data) -> None:
        _on_message(data, local_webhook, verification_token)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_handler)
        .build()
    )
    domain_name = (os.getenv("LARK_DOMAIN") or "").strip().lower()
    if not domain_name:
        host = (os.getenv("LARK_HOST") or "").casefold()
        domain_name = "feishu" if "feishu.cn" in host else "lark"
    domain = lark.FEISHU_DOMAIN if domain_name == "feishu" else lark.LARK_DOMAIN

    cli = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        domain=domain,
    )
    print(
        f"[jenkins-ws] Persistent connection active (domain={domain_name}) → {local_webhook}",
        flush=True,
    )
    print(
        "[jenkins-ws] Feishu console → Events → Subscription: "
        "**Receive events through persistent connection**",
        flush=True,
    )
    cli.start()


if __name__ == "__main__":
    run_forever()
