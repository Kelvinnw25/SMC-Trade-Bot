"""Order Block stage — order-block candidate set and selection.

Pipeline position: 5 of 9.

Produces: `context.order_blocks` (`OrderBlock` tuple — the full candidate set
with the selected one marked, per Architecture §2, so the selection stays
auditable rather than collapsing to a single value).

Ported from V1 (`src/strategy.py`), all inside `build_signal_context`:
- the candidate window                     (1353-1358) → `candidate_window`
- `_register_ob_candidate`                 (1368-1397) → `_candidate` + the
                                                          selection rule
- the LONG candidate loop                  (1407-1422) → `detect_order_blocks`
- the SHORT/else candidate loop            (1430-1445) → `detect_order_blocks`
- the inline quality filter                (1414-1420,
                                            1437-1443) → `passes_quality_filter`
- `current_price`                          (1399)      → `last_close`
- the `fresh_ob` collapse                  (1447-1451) → `fresh_ob`

## What V1 actually has — and does not

Verified by direct inspection of `src/strategy.py`:

- **Detection** is a single backwards scan over a fixed 10-bar window for one
  origin candle: for a LONG, the most recent **down** candle (a demand zone,
  `top=open` / `bottom=low`); for a SHORT, the most recent **up** candle (a
  supply zone, `top=high` / `bottom=open`). Every such candle in the window is
  registered as a candidate.
- **Mitigation** is V1's `fresh_status`: the zone is fresh while no *later* close
  has traded through it (below the bottom for a demand zone, above the top for a
  supply zone). V1 has no partial-mitigation model, no consumed/tested state and
  no re-validation over time — freshness is recomputed from scratch every cycle.
- **Selection** is first-match: the first candidate encountered (scanning from
  the newest bar backwards) that passes the quality filter wins, and it is never
  reconsidered. Frozen by Decision #002.
- **V1 has no implementation** of: breaker blocks, mitigation blocks, propulsion
  blocks, an impulse/displacement-origin requirement for the OB, multi-timeframe
  or D1 order blocks, OB stacking/merging, or any OB persistence across cycles.
  Those concepts do not exist in the legacy system and are **not invented here**.
  The origin candle is chosen purely by candle colour within the window — V1
  never checks that an impulse followed it.

## V1 quirks preserved

1. **The `else` branch is not "SHORT" — it is "not LONG".** A `direction` of
   None or anything else runs the supply-zone scan, exactly as V1 does. (V1 only
   reaches this code with a direction, since it exits earlier at `no_direction`,
   strategy.py:1238; V2's pipeline has no early exit yet, so the branch is
   reachable and behaves identically.)
2. **Freshness is not part of selection.** A candidate is selected on the quality
   filter alone; `fresh_ob` is read off the *already selected* block afterwards.
   A stale block therefore still wins the slot and blocks every fresher one
   behind it, yielding `fresh_ob=False`. That is V1's behavior, not a defect to
   fix here.
3. **`atr_last <= 0` disables the quality filter entirely**, so every candidate
   is registered with `passed_quality_filter=False`, nothing is selected, and
   `size_atr` / `depth_from_price` are None.
4. **`current_price` is the last *closed* H1 close** (strategy.py:1399), not the
   forming-candle `live_price`.
5. **`window_bounds` (1355-1358) is computed and then never published.** V1's
   audit consumer reads `debug["ob_audit"]["window_bounds"]` (main.py:509), a key
   `build_signal_context` never sets — so that audit field, along with
   `selected_ob_index` / `selected_ob_rank` / `selected_ob_top` /
   `selected_ob_bottom` / `selected_ob_size` / `current_price` (main.py:512-519),
   is always None in the emitted record. `candidate_window` is exposed here for
   testability; nothing publishes it, which keeps that quirk intact.

Must not: compute the entry price from the selected block, or gate an entry on
it — that is the entry stage's responsibility (Phase 3.6).

Purity: no I/O, no clock, no state, no caches. ATR is *read* (from
`context.atr_last`, which the structure stage took from the frame's existing
`atr` column); nothing here computes ATR.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.domain import OrderBlock

# V1 candidate window (strategy.py:1353-1354): the 10 bars ending one bar before
# the last, never reaching index 0.
OB_WINDOW_LOOKBACK = 10

# V1 quality-filter thresholds (strategy.py:1417-1419, 1440-1442).
OB_MIN_SIZE_ATR_MULT = 0.25
OB_NOT_LOST_ATR_MULT = 0.20
OB_MIN_DISTANCE_ATR_MULT = 0.15


def candidate_window(last_idx: int) -> Tuple[int, int]:
    """V1's OB scan bounds (strategy.py:1353-1354) as ``(start, end)``, both
    inclusive. The scan runs from `end` down to `start`."""
    return max(1, last_idx - OB_WINDOW_LOOKBACK), last_idx - 1


def last_close(frame: pd.DataFrame) -> float:
    """V1's `current_price` (strategy.py:1399) — the last **closed** H1 close."""
    return float(frame["close"].iloc[-1])


def passes_quality_filter(
    direction: Optional[str],
    top: float,
    bottom: float,
    current_price: float,
    atr_last: float,
) -> bool:
    """V1's inline OB quality filter (strategy.py:1414-1420 / 1437-1443).

    Rejects microscopic zones, zones price has already run away from, and zones
    sitting too close to price. A non-positive `atr_last` rejects everything.
    As in V1, any non-"LONG" direction takes the supply-zone test.
    """
    if not atr_last > 0:
        return False

    size = abs(top - bottom)
    if not size >= (atr_last * OB_MIN_SIZE_ATR_MULT):
        return False

    mid = (top + bottom) / 2.0

    if direction == "LONG":
        # Reject if price already deeply below OB
        not_lost = current_price >= (bottom - atr_last * OB_NOT_LOST_ATR_MULT)
    else:
        # Reject if price already deeply above OB
        not_lost = current_price <= (top + atr_last * OB_NOT_LOST_ATR_MULT)

    # Reject if OB is too close to current price
    distance_ok = abs(current_price - mid) >= (atr_last * OB_MIN_DISTANCE_ATR_MULT)

    return not_lost and distance_ok


def _candidate(
    index: int,
    top: float,
    bottom: float,
    *,
    rank_from_last: int,
    fresh_status: bool,
    passed_quality_filter: bool,
    is_selected: bool,
    current_price: float,
    atr_last: float,
) -> OrderBlock:
    """V1's `_register_ob_candidate` candidate dict (strategy.py:1371-1385) as an
    immutable `OrderBlock`."""
    size = abs(top - bottom)
    mid = (top + bottom) / 2.0
    return OrderBlock(
        index=index,
        rank_from_last=rank_from_last,
        top=top,
        bottom=bottom,
        size=size,
        size_atr=(size / atr_last) if atr_last > 0 else None,
        depth_from_price=(
            (abs(current_price - mid) / atr_last) if atr_last > 0 else None
        ),
        fresh_status=fresh_status,
        passed_quality_filter=passed_quality_filter,
        is_selected=is_selected,
    )


def detect_order_blocks(
    direction: Optional[str],
    frame: Optional[pd.DataFrame],
    current_price: float,
    atr_last: float,
) -> Tuple[OrderBlock, ...]:
    """V1's OB candidate registration (strategy.py:1360-1445), verbatim behavior.

    Scans the candidate window from the newest bar backwards, registering every
    origin candle of the traded direction, and marks the first one that passes
    the quality filter as selected (Decision #002 — first match, frozen).
    """
    if frame is None or frame.empty:
        return ()

    last_idx = len(frame) - 1
    window_start_index, window_end_index = candidate_window(last_idx)

    blocks: list = []
    selected = False

    if direction == "LONG":
        for i in range(window_end_index, window_start_index - 1, -1):
            if float(frame["close"].iloc[i]) < float(frame["open"].iloc[i]):
                candidate_top = float(frame["open"].iloc[i])
                candidate_bottom = float(frame["low"].iloc[i])

                fresh_status = not (
                    frame.iloc[i + 1 :]["close"] < candidate_bottom
                ).any()

                passed = passes_quality_filter(
                    direction, candidate_top, candidate_bottom, current_price, atr_last
                )
                is_selected = passed and not selected
                selected = selected or is_selected

                blocks.append(
                    _candidate(
                        i,
                        candidate_top,
                        candidate_bottom,
                        rank_from_last=len(blocks) + 1,
                        fresh_status=fresh_status,
                        passed_quality_filter=passed,
                        is_selected=is_selected,
                        current_price=current_price,
                        atr_last=atr_last,
                    )
                )

    else:
        for i in range(window_end_index, window_start_index - 1, -1):
            if float(frame["close"].iloc[i]) > float(frame["open"].iloc[i]):
                candidate_top = float(frame["high"].iloc[i])
                candidate_bottom = float(frame["open"].iloc[i])

                fresh_status = not (frame.iloc[i + 1 :]["close"] > candidate_top).any()

                passed = passes_quality_filter(
                    direction, candidate_top, candidate_bottom, current_price, atr_last
                )
                is_selected = passed and not selected
                selected = selected or is_selected

                blocks.append(
                    _candidate(
                        i,
                        candidate_top,
                        candidate_bottom,
                        rank_from_last=len(blocks) + 1,
                        fresh_status=fresh_status,
                        passed_quality_filter=passed,
                        is_selected=is_selected,
                        current_price=current_price,
                        atr_last=atr_last,
                    )
                )

    return tuple(blocks)


def selected_block(blocks: Optional[Sequence[OrderBlock]]) -> Optional[OrderBlock]:
    """The block V1 kept in `ob_meta` / `ob_top` / `ob_bottom`, or None."""
    if not blocks:
        return None
    for block in blocks:
        if block.is_selected:
            return block
    return None


def fresh_ob(blocks: Optional[Sequence[OrderBlock]]) -> bool:
    """V1's `fresh_ob` collapse (strategy.py:1447-1451).

    The selected block's freshness, or False when nothing was selected. It is a
    derivation of the candidate set, so it is computed on demand rather than
    stored as separate state.
    """
    block = selected_block(blocks)
    return bool(block.fresh_status) if block is not None else False


class OrderBlockStage:
    """Builds the order-block candidate set and marks the selection."""

    name = "order_block"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        htf = context.htf
        if htf is None or context.atr_last is None:
            # V1 exits before the OB scan when the HTF map failed, and never
            # reaches it without the structural `atr_last`.
            return context

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return context

        return context.with_(
            order_blocks=detect_order_blocks(
                htf.direction, h1, last_close(h1), context.atr_last
            )
        )
