"""Fair Value Gap stage — the three-candle imbalance test.

Pipeline position: 6 of 9 (the last of the structural confirmations).

Produces: `context.fvg` (`FairValueGap`).

Ported from V1 (`src/strategy.py`), all inline inside `build_signal_context`:
- `min_gap = atr_last * 0.16`   (1351)      → `FVG_MIN_GAP_ATR_MULT`
- the LONG scan                 (1401-1405) → `detect_fvg`
- the SHORT/else scan           (1425-1428) → `detect_fvg`

## What V1 actually implements — and what it does not

V1 **does** have a fair value gap routine, but a minimal one, and it is
preserved exactly as minimal. Verified by grepping `src/` and `main.py` for
`fvg|fair.?value|imbalance`:

- **There is no helper function.** No `_detect_fvg`, no method, no class — the
  detection exists only as two inline loops, one per direction.
- **The only output is a boolean.** The loop sets `fvg_valid = True` and
  immediately `break`s, so the gap's index, top and bottom are never captured.
  `FairValueGap.index/top/bottom` therefore stay at their `None` defaults: V1
  records nothing to put in them, and inventing values would be expansion, not
  migration. (`v2.domain.structure.FairValueGap` documents the same reason.)
- **V1 has no implementation** of: FVG mitigation or fill tracking, partial
  fill, inversion (IFVG), balanced price range, consequent encroachment, an FVG
  candidate set, multi-timeframe FVGs, or FVG-based entries. None are invented
  here.

## Where the boolean goes

- `h1_setup["fvg_valid"]` (1685) → read by `score_signal` (795), worth **+1** in
  each of the CONTINUATION / RETRACEMENT / WATCH_ONLY branches (876, 898, 918).
  That is its only effect on a trading decision — it never gates an entry; the
  entry engine does not read it at all.
- `debug["conf"]["fvg"]` (1706) and `debug["ob_audit"]["fvg_valid"]` (1787) are
  pure diagnostics (dashboard line main.py:421, audit JSONL main.py:518).

Scoring is Phase 3.8; this stage only produces the value.

## V1 quirks preserved

1. **The scan includes the last closed bar** and runs 13 bars back
   (`range(last_idx, max(2, last_idx - 12) - 1, -1)`) — unlike the order-block
   window, which excludes the final bar and spans 10. The floor of 2 is what
   keeps `i - 2` in range.
2. **The `else` branch is not "SHORT" — it is "not LONG".** A `direction` of
   None runs the bearish scan, exactly as V1 does.
3. **`atr_last <= 0` makes `min_gap` zero or negative**, so the gap test
   degenerates and any touching (even zero-width) gap on a correctly-coloured
   candle counts. That is V1's behavior, not a defect to fix.
4. **The gap is measured against the candle two bars back**, and the candle that
   closes the gap must close in the trade direction; V1 checks nothing at all
   about the middle candle.

## Ordering note

Within each of V1's direction branches the FVG scan runs *before* the order-block
scan (1401 vs 1407). V2 runs order_block first (pipeline position 5 vs 6). Both
are pure functions of the frame and share no state, so the order has no
behavioral consequence — pinned by test.

Purity: no I/O, no clock, no state, no caches. ATR is *read* (from
`context.atr_last`, which the structure stage took from the frame's existing
`atr` column); nothing here computes ATR.
"""
from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.domain import FairValueGap

# V1 scan parameters (strategy.py:1351, 1402/1425).
FVG_MIN_GAP_ATR_MULT = 0.16
FVG_LOOKBACK = 12

# V1's floor on the scan index: `i - 2` must stay in range (strategy.py:1402).
_MIN_SCAN_INDEX = 2

_NO_FVG = FairValueGap(valid=False)


def scan_window(last_idx: int) -> Tuple[int, int]:
    """V1's FVG scan bounds (strategy.py:1402/1425) as ``(start, end)``, both
    inclusive. The scan runs from `end` down to `start`."""
    return max(_MIN_SCAN_INDEX, last_idx - FVG_LOOKBACK), last_idx


def detect_fvg(
    direction: Optional[str],
    frame: Optional[pd.DataFrame],
    atr_last: float,
) -> FairValueGap:
    """V1's inline FVG scan (strategy.py:1401-1405 / 1425-1428), verbatim.

    Scans backwards from the last closed bar for a three-candle imbalance in the
    traded direction and stops at the first one found. Returns a `FairValueGap`
    carrying only `valid` — V1 breaks out of the loop without recording the
    gap's index or bounds, so there is nothing else to report.
    """
    if frame is None or frame.empty:
        return _NO_FVG

    last_idx = len(frame) - 1
    start_idx, end_idx = scan_window(last_idx)

    min_gap = atr_last * FVG_MIN_GAP_ATR_MULT

    if direction == "LONG":
        for i in range(end_idx, start_idx - 1, -1):
            if (
                float(frame["low"].iloc[i]) - float(frame["high"].iloc[i - 2])
                >= min_gap
                and float(frame["close"].iloc[i]) > float(frame["open"].iloc[i])
            ):
                return FairValueGap(valid=True)

    else:
        for i in range(end_idx, start_idx - 1, -1):
            if (
                float(frame["low"].iloc[i - 2]) - float(frame["high"].iloc[i])
                >= min_gap
                and float(frame["close"].iloc[i]) < float(frame["open"].iloc[i])
            ):
                return FairValueGap(valid=True)

    return _NO_FVG


class FairValueGapStage:
    """Detects the fair value gap (imbalance) confirmation."""

    name = "fair_value_gap"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        htf = context.htf
        if htf is None or context.atr_last is None:
            # V1 exits before the FVG scan when the HTF map failed, and never
            # reaches it without the structural `atr_last`.
            return context

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return context

        return context.with_(fvg=detect_fvg(htf.direction, h1, context.atr_last))
