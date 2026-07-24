"""Liquidity stage — liquidity sweep detection.

Pipeline position: 4 of 9.

Produces: `context.sweep` (`LiquiditySweep`).

Ported from V1 (`src/strategy.py`):
- `_detect_sweep` (74-235) → `detect_sweep`

V1 calls `_detect_sweep` exactly once (strategy.py:1348), immediately after the
structural swings are computed, and **discards the metadata**
(`sweep_valid, _ = ...`). The metadata is nonetheless carried here because the
frozen `LiquiditySweep` domain object models it and it costs nothing — V1
produces those exact values, it simply ignores them. No decision depends on
them.

## What V1 actually has — and does not

Verified by direct inspection of `src/strategy.py`:

- **Sweep detection**: `_detect_sweep` — the single liquidity routine, covering
  both the buy-side (LONG: sweep below a swing low, then reclaim) and sell-side
  (SHORT: sweep above a swing high, then reclaim) cases.
- **V1 has no implementation** of: raid detection, inducement, a stop-hunt
  routine, or any standalone equal-high / equal-low detector. Those concepts do
  not exist in the legacy system and are **not invented here**.
- The only EQH/EQL concept in V1 is `_cluster_liquidity_levels` (254-277), which
  exists solely to rank take-profit targets and is called only by
  `find_liquidity_target` (346, 360). Architecture §3 assigns that whole path —
  "liquidity target selection (EQH/EQL, swing, PDH/PDL, RR-aware, mode-aware, 2R
  fallback)" — to `analysis.risk`. It therefore stays outside this stage.

## A path V1 never reaches

V1 only reaches line 1348 once a direction exists (it returns at its
`no_direction` exit, 1238). V2's pipeline has no early exit yet, so this stage
can run with `direction=None`. V1's own function falls through both branches in
that case and returns `(False, None)`, so the behavior is identical — no
divergence is introduced by reaching the call earlier.

Purity: no I/O, no clock, no state, no caches. ATR is *read* from the frame's
existing `atr` column; nothing here computes ATR.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.domain import LiquiditySweep, Swing

# V1 `_detect_sweep` default parameters (strategy.py:80-83).
SWEEP_LOOKBACK = 12
SWEEP_ATR_DEPTH_MULT = 0.12
SWEEP_RECLAIM_RATIO = 0.40
SWEEP_MAX_SWING_AGE = 36

# V1's reclaim buffer multiplier (strategy.py:140, 206).
_RECLAIM_BUFFER_ATR_MULT = 0.03

# V1's minimum frame length for sweep detection (strategy.py:86).
_MIN_ROWS = 5

_NO_SWEEP = LiquiditySweep(detected=False)


def _atr_at(frame: pd.DataFrame, i: int) -> float:
    """V1's nested `_atr_at` (strategy.py:92-101): missing column, NaN or
    non-finite all yield 0.0."""
    if "atr" not in frame.columns:
        return 0.0
    val = frame["atr"].iloc[i]
    if pd.isna(val) or not math.isfinite(float(val)):
        return 0.0
    return float(val)


def _fresh_reference(
    swings: Sequence[Swing], last_idx: int, max_swing_age: int
) -> Optional[Swing]:
    """V1's valid-swing filter plus its "freshest" pick (strategy.py:109-124).

    A swing qualifies when it sits strictly before the final bar and is no older
    than `max_swing_age`; V1 then takes the LAST qualifying entry, which is the
    most recent one because `identify_swings` appends in ascending index order.
    """
    valid = [
        s
        for s in swings
        if int(s.index) < last_idx and (last_idx - int(s.index)) <= max_swing_age
    ]
    if not valid:
        return None
    return valid[-1]


def detect_sweep(
    direction: Optional[str],
    frame: Optional[pd.DataFrame],
    swing_highs: Sequence[Swing],
    swing_lows: Sequence[Swing],
    *,
    lookback: int = SWEEP_LOOKBACK,
    atr_depth_mult: float = SWEEP_ATR_DEPTH_MULT,
    reclaim_ratio: float = SWEEP_RECLAIM_RATIO,
    max_swing_age: int = SWEEP_MAX_SWING_AGE,
) -> LiquiditySweep:
    """V1 `_detect_sweep` (strategy.py:74-235), verbatim behavior.

    Scans backwards from the final bar over `lookback` bars for a candle that
    pierced the freshest qualifying swing level and closed back through it with
    sufficient depth and body reclaim. Returns an undetected `LiquiditySweep`
    where V1 returned `(False, None)`.
    """
    if frame is None or frame.empty or len(frame) < _MIN_ROWS:
        return _NO_SWEEP

    last_idx = len(frame) - 1
    start_idx = max(0, last_idx - lookback)

    # =========================================================
    # LONG SWEEP
    # =========================================================
    if direction == "LONG":
        ref_swing = _fresh_reference(swing_lows, last_idx, max_swing_age)
        if ref_swing is None:
            return _NO_SWEEP

        ref_idx = int(ref_swing.index)
        ref_price = float(ref_swing.price)

        for i in range(last_idx, start_idx - 1, -1):
            candle_low = float(frame["low"].iloc[i])
            candle_high = float(frame["high"].iloc[i])
            candle_close = float(frame["close"].iloc[i])

            candle_range = max(abs(candle_high - candle_low), 1e-9)

            atr_now = _atr_at(frame, i)

            # Depth below liquidity
            sweep_depth = ref_price - candle_low

            # Close reclaimed back above liquidity
            reclaim_buffer = atr_now * _RECLAIM_BUFFER_ATR_MULT

            reclaimed = candle_close > (ref_price + reclaim_buffer)

            # Body reclaim strength
            reclaim_strength = (candle_close - candle_low) / candle_range

            # Minimum depth validation
            depth_valid = sweep_depth >= (atr_now * atr_depth_mult)

            if (
                candle_low < ref_price
                and reclaimed
                and depth_valid
                and reclaim_strength >= reclaim_ratio
            ):
                return LiquiditySweep(
                    detected=True,
                    index=i,
                    ref_index=ref_idx,
                    ref_price=ref_price,
                    sweep_depth=sweep_depth,
                    reclaim_strength=reclaim_strength,
                )

    # =========================================================
    # SHORT SWEEP
    # =========================================================
    elif direction == "SHORT":
        ref_swing = _fresh_reference(swing_highs, last_idx, max_swing_age)
        if ref_swing is None:
            return _NO_SWEEP

        ref_idx = int(ref_swing.index)
        ref_price = float(ref_swing.price)

        for i in range(last_idx, start_idx - 1, -1):
            candle_high = float(frame["high"].iloc[i])
            candle_low = float(frame["low"].iloc[i])
            candle_close = float(frame["close"].iloc[i])

            candle_range = max(abs(candle_high - candle_low), 1e-9)

            atr_now = _atr_at(frame, i)

            # Depth above liquidity
            sweep_depth = candle_high - ref_price

            # Close reclaimed back below liquidity
            reclaim_buffer = atr_now * _RECLAIM_BUFFER_ATR_MULT

            reclaimed = candle_close < (ref_price - reclaim_buffer)

            # Body reclaim strength
            reclaim_strength = (candle_high - candle_close) / candle_range

            # Minimum depth validation
            depth_valid = sweep_depth >= (atr_now * atr_depth_mult)

            if (
                candle_high > ref_price
                and reclaimed
                and depth_valid
                and reclaim_strength >= reclaim_ratio
            ):
                return LiquiditySweep(
                    detected=True,
                    index=i,
                    ref_index=ref_idx,
                    ref_price=ref_price,
                    sweep_depth=sweep_depth,
                    reclaim_strength=reclaim_strength,
                )

    return _NO_SWEEP


class LiquidityStage:
    """Detects the liquidity sweep that precedes a valid reaction."""

    name = "liquidity"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        htf = context.htf
        if htf is None or context.swing_highs is None or context.swing_lows is None:
            # V1 exits before the sweep call when HTF failed, and never reaches
            # it without the structural swings.
            return context

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return context

        return context.with_(
            sweep=detect_sweep(
                htf.direction, h1, context.swing_highs, context.swing_lows
            )
        )
