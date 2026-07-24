"""Shared frame preparation — V1's `build_signal_context` preamble.

V1 prepares its D1/H1 frames exactly once at the top of `build_signal_context`
(`_safe_df`, strategy.py:1077-1105) and every downstream computation reads those
prepared frames. More than one V2 stage needs that identical preparation, so it
lives here rather than inside any single stage: one stage importing another
stage's private helper would both misplace ownership and create an import cycle
(`analysis.htf` already depends on `analysis.regime` for the regime label).

This module owns no analysis behavior — only V1's frame hygiene and the row
minimums that gate it. Pure: no I/O, no clock, no state.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

# V1 minimum frame lengths (strategy.py:1104-1105).
D1_MIN_ROWS = 5
H1_MIN_ROWS = 20

# Columns V1 requires to be non-null, when present (strategy.py:1081).
_REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close")


def safe_frame(df: Optional[pd.DataFrame], min_len: int) -> Optional[pd.DataFrame]:
    """V1 `build_signal_context._safe_df` (strategy.py:1077-1084), verbatim.

    Returns a re-indexed copy with rows missing any required OHLC/timestamp
    value dropped, or None when the frame is absent, empty, or falls below
    `min_len` either before or after the drop.
    """
    if df is None or df.empty or len(df) < min_len:
        return None
    x = df.copy().reset_index(drop=True)
    x = x.dropna(subset=[c for c in _REQUIRED_COLUMNS if c in x.columns])
    if len(x) < min_len:
        return None
    return x
