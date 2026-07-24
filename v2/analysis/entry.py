"""Entry stage — the structural entry plan.

Pipeline position: 7 of 9 (consumes the structural stages above).

Produces: `context.entry` (`EntryPlan`).

Ported from V1 (`src/strategy.py`), the entry engine inside
`build_signal_context`:
- the initial entry state          (1463-1466) → `plan_entry` locals
- `_calc_continuation_entry`       (1477-1561) → `_continuation_entry`
- the rotational-LONG guard        (1570-1580) → `plan_entry`
- the retracement OB gate          (1587-1620) → `_retracement_ob_valid`
- the retracement entry price      (1622-1634) → `plan_entry`
- the WATCH_ONLY / else branches   (1664-1670) → `plan_entry`

Contract note: per Decision #001 ("Contract B") the entry price is a
**structural reference, not an executable order price**. The plan carries that
marker; nothing here implies order placement.

## The Entry / Risk split

V1's entry engine is physically interleaved with risk: both the continuation and
the retracement branch compute `sl`, call `find_liquidity_target`, and apply the
2R fallback *inline*, right next to the entry decision. Architecture §3 assigns
all of that — stop placement, liquidity-target selection, RR — to
`analysis.risk`, and this milestone forbids it.

That split is safe, and the reason is structural: **nothing risk computes ever
feeds back into an entry outcome.** In the continuation branch the `return` at
1530/1557 is already committed by the time `find_liquidity_target` is called; in
the retracement branch `ob_valid` is decided at 1622 before any `sl`/`tp` exists.
So this stage stops at `entry_ready` / `entry_source` / `entry_reason` /
`entry_price`, and publishes `displacement_index` and `pullback_index` — which is
exactly what Risk needs to recover V1's stop levels (the pullback bar's low/high
for a continuation, the selected order block for a retracement) without
recomputing anything.

## Sweep and FVG are NOT entry gates

Verified by grep: `sweep_valid` is read only at 794 (scoring), 1284 (veto exit)
and 1684/1705 (publication); `fvg_valid` only at 795 (scoring), 1285, 1685, 1706
and 1787. **V1's entry engine reads neither.** They are scoring inputs, and no
sweep or FVG requirement is invented here to fill the gap.

## Reused, never re-implemented

- `is_displacement_candle` — `analysis.structure` (Phase 3.3). The continuation
  scan tests *earlier* bars with the same predicate the structure stage applies
  to the last bar.
- `passes_quality_filter`, `selected_block`, `fresh_ob`, `last_close` —
  `analysis.order_block` (Phase 3.5). V1 re-types the OB quality filter inside
  the entry engine (1600-1620) as a second copy of 1414-1420; V2 has one
  implementation and this stage calls it.

## V1 quirks preserved

1. **A successful retracement entry leaves `entry_reason` as "no_entry".** V1's
   `ob_valid` branch (1622-1658) sets `entry_ready`, `entry_source` and the
   price but never touches `entry_reason`, so the initial value from 1466
   survives into `h1_setup`. It is not "fixed" here.
2. **`entry_source` carries the failure reason on the CONTINUATION path.** Line
   1562 assigns `entry_source = entry_reason` unconditionally, so a failed
   continuation publishes `entry_source` of "no_continuation_displacement" or
   "no_micro_pullback" — which is why scoring keys its displacement bonus off
   the exact string "continuation_micro_pullback" (880).
3. **The OB gate re-checks a filter that already passed.** A block is only
   selected when it passed `passes_quality_filter` with the same `current_price`
   and `atr_last`, so the re-check at 1600-1620 always returns True and
   `ob_valid` is effectively just `fresh_ob`. The redundant call is kept because
   removing it would be an optimization, not a migration.
4. **The displacement scan skips the last bar** (`range(last_idx - 1, ...)`) and
   looks back 3, while the pullback scan runs *forward* from the displacement
   bar. The two windows overlap and both are clamped to the frame.
5. **`else` means "not LONG"** in the pullback test and in the retracement entry
   price, exactly as elsewhere in V1.
6. **The veto path is not modelled here.** V1 returns at 1275 with
   `entry_source="veto"` and `entry_reason=<veto_reason>` before the entry engine
   runs; V2's pipeline has no early exit yet, so a vetoed context reaches this
   stage with `entry_mode="NONE"` and produces "non_actionable_regime"/"none".
   The early-exit shape — shared with `sweep_valid`, `fresh_ob` and `fvg_valid`,
   which V1 also hardcodes at 1284-1288 — is assembled in Phase 3.9.

Purity: no I/O, no clock, no state, no caches. ATR is *read* (from
`context.atr_last`); nothing here computes ATR.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.order_block import (
    fresh_ob,
    last_close,
    passes_quality_filter,
    selected_block,
)
from v2.analysis.structure import is_displacement_candle
from v2.domain import EntryPlan, HTFContext, OrderBlock

# V1 continuation parameters (strategy.py:1474, 1505/1532).
CONTINUATION_LOOKBACK = 3
MICRO_RETRACE_ATR_MULT = 0.10
MICRO_RETRACE_FLOOR = 1e-9

# V1's initial entry state (strategy.py:1463-1466).
NO_ENTRY_PRICE = 0.0
NO_ENTRY_SOURCE = "none"
NO_ENTRY_REASON = "no_entry"


def _continuation_entry(
    direction: Optional[str], frame: pd.DataFrame, atr_last: float
) -> Tuple[float, bool, str, Optional[int], Optional[int]]:
    """V1 `_calc_continuation_entry` (strategy.py:1477-1559), minus the risk
    block it interleaves.

    Returns ``(entry_price, entry_ready, entry_reason, disp_idx, pullback_idx)``.
    V1's `sl`/`tp`/`tp_source`/`tp_rr` computation is deliberately absent: it is
    `analysis.risk`'s, and it never influences any value returned here.
    """
    last_idx = len(frame) - 1
    search_start = max(0, last_idx - CONTINUATION_LOOKBACK)

    disp_idx = None
    disp_close = None

    for i in range(last_idx - 1, search_start - 1, -1):
        if is_displacement_candle(frame.iloc[i], direction, atr_last):
            disp_idx = i
            disp_close = float(frame["close"].iloc[i])
            break

    if disp_idx is None:
        return NO_ENTRY_PRICE, False, "no_continuation_displacement", None, None

    pullback_end = min(last_idx, disp_idx + CONTINUATION_LOOKBACK)

    for j in range(disp_idx + 1, pullback_end + 1):
        pb = frame.iloc[j]
        pb_high = float(pb["high"])
        pb_low = float(pb["low"])
        pb_close = float(pb["close"])

        if direction == "LONG":
            micro_retrace = pb_low <= (
                disp_close + max(atr_last * MICRO_RETRACE_ATR_MULT, MICRO_RETRACE_FLOOR)
            )
            reclaim = pb_close >= disp_close
            if micro_retrace and reclaim:
                return pb_close, True, "continuation_micro_pullback", disp_idx, j
        else:
            micro_retrace = pb_high >= (
                disp_close - max(atr_last * MICRO_RETRACE_ATR_MULT, MICRO_RETRACE_FLOOR)
            )
            reclaim = pb_close <= disp_close
            if micro_retrace and reclaim:
                return pb_close, True, "continuation_micro_pullback", disp_idx, j

    return NO_ENTRY_PRICE, False, "no_micro_pullback", disp_idx, None


def retracement_ob_valid(
    direction: Optional[str],
    frame: pd.DataFrame,
    atr_last: float,
    order_blocks: Optional[Sequence[OrderBlock]],
) -> bool:
    """V1's OB gate inside the entry engine (strategy.py:1587-1620).

    Requires a *fresh* selected block that (re-)passes the order-block quality
    filter. The re-check is V1's own second application of the same predicate;
    it delegates to `analysis.order_block` rather than duplicating it.
    """
    block = selected_block(order_blocks)
    if not (fresh_ob(order_blocks) and block is not None):
        return False
    return passes_quality_filter(
        direction, block.top, block.bottom, last_close(frame), atr_last
    )


def _rotational_long_conflict(
    direction: Optional[str], regime_label: str, htf: HTFContext
) -> bool:
    """V1's rotational-LONG guard predicate (strategy.py:1570-1576)."""
    return (
        direction == "LONG"
        and regime_label == "rotational"
        and htf.weekly_bias == "BULLISH"
        and htf.d1_bias == "BEARISH"
        and htf.h4_bias == "BEARISH"
    )


def plan_entry(
    entry_mode: str,
    direction: Optional[str],
    frame: pd.DataFrame,
    atr_last: float,
    *,
    order_blocks: Optional[Sequence[OrderBlock]],
    htf: HTFContext,
    regime_label: str,
) -> EntryPlan:
    """V1's entry engine (strategy.py:1463-1670), verbatim behavior.

    Produces only the entry decision. Stop-loss, take-profit, tp_source and
    tp_rr — which V1 computes in the same block — belong to `analysis.risk`.
    """
    entry_price = NO_ENTRY_PRICE
    entry_ready = False
    entry_source = NO_ENTRY_SOURCE
    entry_reason = NO_ENTRY_REASON
    disp_idx: Optional[int] = None
    pullback_idx: Optional[int] = None

    regime_str = str(regime_label).lower().strip()

    if entry_mode == "CONTINUATION":
        (
            entry_price,
            entry_ready,
            entry_reason,
            disp_idx,
            pullback_idx,
        ) = _continuation_entry(direction, frame, atr_last)
        entry_source = entry_reason

    elif entry_mode == "RETRACEMENT":

        # =========================================================
        # ROTATIONAL LONG GUARD
        # In HTF conflict, rotational LONGs only make sense in discount.
        # =========================================================
        htf_conflict_long = _rotational_long_conflict(direction, regime_str, htf)

        if htf_conflict_long and htf.location != "discount":
            entry_ready = False
            entry_reason = "rotational_long_needs_discount"

        else:
            if retracement_ob_valid(direction, frame, atr_last, order_blocks):
                block = selected_block(order_blocks)

                entry_ready = True
                entry_source = "retracement_ob"

                if direction == "LONG":
                    entry_price = block.top
                else:
                    entry_price = block.bottom

            else:
                entry_ready = False
                entry_reason = "ob_quality_failed"

    elif entry_mode == "WATCH_ONLY":
        entry_ready = False
        entry_reason = "watch_only_regime"

    else:
        entry_ready = False
        entry_reason = "non_actionable_regime"

    return EntryPlan(
        entry_mode=entry_mode,
        entry_price=entry_price,
        entry_ready=entry_ready,
        entry_source=entry_source,
        entry_reason=entry_reason,
        displacement_index=disp_idx,
        pullback_index=pullback_idx,
    )


class EntryStage:
    """Derives the structural entry plan."""

    name = "entry"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        htf, regime = context.htf, context.regime
        if htf is None or regime is None or context.atr_last is None:
            # V1 exits before the entry engine when the HTF map failed, and
            # never reaches it without a regime or the structural `atr_last`.
            return context

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return context

        return context.with_(
            entry=plan_entry(
                regime.entry_mode,
                htf.direction,
                h1,
                context.atr_last,
                order_blocks=context.order_blocks,
                htf=htf,
                regime_label=regime.label,
            )
        )
