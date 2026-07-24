"""Structure stage — swing points and the structural break signal.

Pipeline position: 3 of 9.

Produces: `context.swing_highs`, `context.swing_lows`, `context.atr_last`,
`context.displacement_valid`, `context.volume_spike`.

Ported from V1 (`src/strategy.py`):
- `identify_swings`                   → `identify_swings`        (517-554)
- `build_signal_context._is_displacement_candle`
                                      → `is_displacement_candle` (1086-1102)
- the `atr_last` read                 → `last_atr`               (1344)
- the volume-spike test               → `detect_volume_spike`    (1460-1461)

`identify_swings` was co-located in `analysis.htf` for Phases 3.1-3.2 because HTF
bias derivation needs it. It is **moved here verbatim** in Phase 3.3: this module
is now its single owner (Architecture §3 assigns swing detection to
`analysis.structure`), and `analysis.htf` imports it. There is exactly one
implementation.

## What V1 actually has — and does not

V1 contains **no** break-of-structure engine, no change-of-character engine on
H1, no pivot routine beyond `identify_swings`, and no internal-structure or
structure-state machine. Direct verification of `src/strategy.py`:

- the only swing/pivot detector is `identify_swings`;
- the H1 structural-break signal is `_is_displacement_candle` — a single-candle
  displacement test, which is what the regression harness reports as its "BOS"
  observable;
- `choch` exists only as `m5_setup["choch_confirmed"]` / `m5_choch`, produced by
  `enrich_with_m5`. Architecture §3 assigns the M5 enrichment re-score to
  `analysis.scoring`, so it is not this stage's concern.

Nothing is invented here to fill those gaps: behavioral equivalence forbids
adding structure analysis V1 never performed.

## Two call sites, one function

V1 calls `identify_swings` with two different parameter sets, and both are
preserved:

- **bias** (`_derive_simple_bias_from_df`, 620): window 3, prominence 0.18 —
  used by `analysis.htf` on D1/H4/H1 frames.
- **structure** (`build_signal_context`, 1345): window 2, prominence 0.3 — the
  H1 swing set this stage publishes, consumed later by liquidity, order blocks,
  entry and risk.

Purity: no I/O, no clock, no state, no caches. ATR is *read* from the frame's
existing `atr` column (V1 computes it upstream in `api_handler`); nothing here
computes ATR.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence, Tuple

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.domain import Swing

# V1 structural swing parameters (strategy.py:1345).
STRUCTURE_SWING_WINDOW = 2
STRUCTURE_PROMINENCE_MULT = 0.3

# V1 displacement thresholds (strategy.py:1094-1101).
_DISPLACEMENT_BODY_ATR_MULT = 0.60
_DISPLACEMENT_BODY_RATIO = 0.50

# V1 volume-spike parameters (strategy.py:1460-1461).
_VOLUME_SMA_WINDOW = 20
_VOLUME_SPIKE_MULT = 1.5


def identify_swings(
    df: pd.DataFrame, window: int, prominence_mult: float
) -> Tuple[list, list]:
    """V1 `identify_swings` (strategy.py:517-554), verbatim behavior.

    Returns V1's two raw lists of ``{"index": i, "price": value}`` dicts. Reads
    the existing `atr` column for the prominence floor; when the frame has no
    `atr` column (V1's resampled H4 frame) the mean is 0.0, which disables
    prominence filtering exactly as it does in V1.
    """
    swing_highs: list = []
    swing_lows: list = []

    if "atr" in df.columns and not df["atr"].dropna().empty:
        atr_mean = float(df["atr"].dropna().mean())
    else:
        atr_mean = 0.0
    if not math.isfinite(atr_mean):
        atr_mean = 0.0
    min_prominence = atr_mean * prominence_mult

    for i in range(window, len(df) - window):
        is_high, is_low = True, True
        for j in range(1, window + 1):
            if (
                df["high"].iloc[i] < df["high"].iloc[i - j]
                or df["high"].iloc[i] < df["high"].iloc[i + j]
            ):
                is_high = False
            if (
                df["low"].iloc[i] > df["low"].iloc[i - j]
                or df["low"].iloc[i] > df["low"].iloc[i + j]
            ):
                is_low = False

        if is_high:
            if prominence_mult > 0:
                left_lows = df["low"].iloc[i - window : i]
                right_lows = df["low"].iloc[i + 1 : i + window + 1]
                prominence = df["high"].iloc[i] - max(left_lows.min(), right_lows.min())
                if prominence < min_prominence:
                    is_high = False

        if is_low:
            if prominence_mult > 0:
                left_highs = df["high"].iloc[i - window : i]
                right_highs = df["high"].iloc[i + 1 : i + window + 1]
                prominence = min(left_highs.max(), right_highs.max()) - df["low"].iloc[i]
                if prominence < min_prominence:
                    is_low = False

        if is_high:
            if not swing_highs or not (
                swing_highs[-1]["price"] == df["high"].iloc[i]
                and swing_highs[-1]["index"] == i - 1
            ):
                swing_highs.append({"index": i, "price": df["high"].iloc[i]})
        if is_low:
            if not swing_lows or not (
                swing_lows[-1]["price"] == df["low"].iloc[i]
                and swing_lows[-1]["index"] == i - 1
            ):
                swing_lows.append({"index": i, "price": df["low"].iloc[i]})

    return swing_highs, swing_lows


def to_swings(raw: Sequence[dict], kind: str) -> Tuple[Swing, ...]:
    """Translate V1's raw swing dicts into immutable domain `Swing` objects."""
    return tuple(
        Swing(index=int(item["index"]), price=float(item["price"]), kind=kind)
        for item in raw
    )


def is_displacement_candle(
    candle: "pd.Series[Any]", direction: Optional[str], atr_last: float
) -> bool:
    """V1 `build_signal_context._is_displacement_candle` (strategy.py:1086-1102).

    V1's `else` branch applies the SHORT test to *any* non-"LONG" direction —
    including None — and that quirk is preserved.
    """
    body = abs(float(candle["close"]) - float(candle["open"]))
    candle_range = float(candle["high"]) - float(candle["low"])
    br = body / candle_range if candle_range > 0 else 0.0

    if direction == "LONG":
        return (
            float(candle["close"]) > float(candle["open"])
            and body >= (atr_last * _DISPLACEMENT_BODY_ATR_MULT)
            and br >= _DISPLACEMENT_BODY_RATIO
        )
    else:
        return (
            float(candle["close"]) < float(candle["open"])
            and body >= (atr_last * _DISPLACEMENT_BODY_ATR_MULT)
            and br >= _DISPLACEMENT_BODY_RATIO
        )


def last_atr(df: pd.DataFrame) -> float:
    """V1's `atr_last` read (strategy.py:1344) — the last non-null ATR, else 0.0.

    Reads an existing column; it does not compute ATR.
    """
    if "atr" in df.columns and not df["atr"].dropna().empty:
        return float(df["atr"].dropna().iloc[-1])
    return 0.0


def detect_volume_spike(df: pd.DataFrame) -> bool:
    """V1's volume-spike test (strategy.py:1460-1461), verbatim.

    A missing `v` column, a frame shorter than the SMA window, or a
    non-positive/NaN SMA all yield False — exactly as V1's guards do.
    """
    if "v" in df.columns and len(df) >= _VOLUME_SMA_WINDOW:
        v_sma = df["v"].rolling(_VOLUME_SMA_WINDOW).mean().iloc[-1]
    else:
        v_sma = 0
    if v_sma > 0:
        return bool(float(df["v"].iloc[-1]) > (v_sma * _VOLUME_SPIKE_MULT))
    return False


class StructureStage:
    """Identifies swing points and the structural break signal."""

    name = "structure"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        htf = context.htf
        if htf is None:
            # V1 exits before any structural work when the HTF map failed.
            return context

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return context

        atr_last = last_atr(h1)
        raw_highs, raw_lows = identify_swings(
            h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
        )

        return context.with_(
            swing_highs=to_swings(raw_highs, "high"),
            swing_lows=to_swings(raw_lows, "low"),
            atr_last=atr_last,
            displacement_valid=is_displacement_candle(
                h1.iloc[-1], htf.direction, atr_last
            ),
            volume_spike=detect_volume_spike(h1),
        )
