"""
jenkinsbot with Lark **persistent connection** (no public webhook URL).

  pip install lark-oapi requests flask
  copy .env.example → .env
  python run_local_bot.py

Lark Developer Console → Events: choose **Persistent connection** (not Request URL).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

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


def _start_flask() -> None:
    os.chdir(_ROOT)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import main

    port = int((os.getenv("PORT") or "5001").strip() or "5001")
    print(f"[jenkinsbot] Flask on 0.0.0.0:{port} (/webhook/event)", flush=True)
    main.app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


def main() -> int:
    _load_dotenv()
    mode = (os.getenv("LARK_EVENT_MODE") or "websocket").strip().lower()
    if mode != "websocket":
        print(
            "[jenkinsbot] Set LARK_EVENT_MODE=websocket for long connection, "
            "or run `python main.py` for public webhook mode.",
            flush=True,
        )
        return 1

    t = threading.Thread(target=_start_flask, daemon=True, name="jenkinsbot-flask")
    t.start()
    time.sleep(2)
    print("[jenkinsbot] Opening Lark persistent connection…", flush=True)

    from lark_longconn import run_forever

    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
