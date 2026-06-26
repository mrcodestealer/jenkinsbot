"""Deprecated wrapper — use ``python main.py`` (persistent connection is the default)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from main import _run_main_entry  # noqa: E402

if __name__ == "__main__":
    print("[jenkinsbot] run_local_bot.py → use `python main.py`", flush=True)
    raise SystemExit(_run_main_entry())
