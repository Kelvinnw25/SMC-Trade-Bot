"""infra.config — native runtime configuration for V2 (replaces src/config.py).

Step 1 of the V2-standalone work: this is the native home for the runtime
configuration the infra adapters (`market_data`, `notifier`) previously obtained
from `src/config.py`. It reads the SAME environment — the same `.env`, the same
variable names, the same defaults — via the same `python-dotenv` loader, so every
value is byte-for-byte identical to V1's. No behavior changes; nothing is
optimized or renamed.

Only the runtime values V2 actually consumes are carried here (verified by
tracing `src.api_handler` and `src.telegram_notifier`):

- Binance: ``API_KEY``, ``SECRET_KEY``, ``MODE`` — `src/config.py:9-11`.
- Technical: ``ATR_PERIOD`` — `src/config.py:27` (a literal 14 in V1, not env-read;
  kept as a literal here so it stays identical).
- Telegram: ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID`` — `src/config.py:13-14`.

Strategy/lifecycle calibration is NOT configured through this module — the
lifecycle keeps its own `LifecycleConfig`, and V2's orchestrator carries its own
operational literals. This module only removes the infra→`src/config.py` runtime
dependency; it does not become a general settings hub.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Same loader V1 used (`src/config.py:2-4`): read the project `.env` on import.
load_dotenv()

# -- Binance (identical to src/config.py:9-11) ------------------------------
API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
MODE = os.getenv("BINANCE_MODE", "testnet")

# -- Telegram (identical to src/config.py:13-14) ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -- Technical (identical to src/config.py:27 — a literal, not env-read) -----
ATR_PERIOD = 14
