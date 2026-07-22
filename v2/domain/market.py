"""MarketSnapshot — raw closed-candle market data for one symbol at one moment.

Architecture ref: 02-software-architecture.md §2 (MarketSnapshot), §8 data contract.
V1 behavior ref: main.py fetch block — forming candle stripped via ``iloc[:-1]``;
the live forming-candle H1 close is carried as a *separate, explicitly-labeled
field* because V1 uses it only for invalidation (kill switch / STALE), never for
structural analysis.

Immutability caveat (the single documented exception, per the approved plan §5):
pandas DataFrames cannot be deep-frozen. The DataFrames held here are immutable
BY CONTRACT — no module may mutate a received snapshot's frames; change is
expressed by constructing a new snapshot. DataFrame fields are excluded from
equality (``compare=False``); tests compare frame content via ``DataFrame.equals``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class MarketSnapshot:
    """Input to all analysis. Never mutated.

    Fields:
        symbol: pair symbol, V1 format (e.g. ``"BTC/USDT"``).
        d1: closed D1 candles (columns: timestamp, open, high, low, close, v, atr).
        h1: closed H1 candles (same columns).
        h1_last_ts: epoch of the last closed H1 candle (V1 ``_to_epoch_ts`` result).
        h1_last_ts_dt: same instant as a pandas Timestamp (V1 ``h1_last_ts_dt``).
        live_price: close of the FORMING H1 candle (V1 ``context["market_price"]``).
            Labeled distinct per Architecture §8; used only for invalidation.
        m5: optional closed M5 candles (fetched only when M5 enrichment applies).
    """

    symbol: str
    d1: pd.DataFrame = field(compare=False, repr=False)
    h1: pd.DataFrame = field(compare=False, repr=False)
    h1_last_ts: float
    h1_last_ts_dt: pd.Timestamp
    live_price: float
    m5: Optional[pd.DataFrame] = field(default=None, compare=False, repr=False)
