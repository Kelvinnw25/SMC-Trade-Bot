"""Risk stage — stop-loss, take-profit target, and risk/reward.

Pipeline position: 8 of 9 (consumes the entry price; scoring's RR gate consumes
this stage's output).

Produces: `context.risk` (`RiskPlan`).

Ported from V1 (`src/strategy.py`):
- `_get_previous_day_high_low`   (241-251) → `previous_day_high_low`
- `_cluster_liquidity_levels`    (254-277) → `cluster_liquidity_levels`
- `find_liquidity_target`        (279-515) → `find_liquidity_target`
- the continuation SL            (1510/1537) → `compute_stop`
- the retracement OB SL          (1631/1634) → `compute_stop`
- the 2R fallback                (1525-1528 / 1552-1555 / 1651-1658)
                                            → `_two_r_fallback`

## The Entry / Risk split (completing Phase 3.7)

V1 computes `sl`, calls `find_liquidity_target`, and applies the 2R fallback
*inline* inside the entry engine, right next to the entry decision. Phase 3.7
(Entry) took only the entry decision and published `pullback_index` (and the
selected order block via `context.order_blocks`), which is exactly the state this
stage needs to recover V1's stop:

- **Continuation**: the stop is the pullback bar's low/high ± `atr*0.30`. The bar
  is `frame.iloc[entry.pullback_index]` — the same bar V1 used at 1510/1537.
- **Retracement**: the stop is the selected block's far edge ± `atr*0.70`. The
  block is `selected_block(order_blocks)` — the same `ob_meta` V1 used at
  1631/1634.

Because the stop and the target are pure functions of the entry price, the
frame, the swings and the ATR — none of which risk mutates — recomputing them
here yields byte-identical `sl`/`tp`/`tp_source`/`tp_rr` to V1's inline block.
This stage never re-decides whether an entry is ready; it reads
`entry.entry_ready` and stops there.

## Two risk-rewards, and which one is here

`RiskPlan` has both `tp_rr` and `rr`:

- **`tp_rr`** — the reward/risk `find_liquidity_target` reports (rounded to 2),
  computed against the *provided* stop. It is Risk's, and this stage sets it.
- **`rr`** — the scoring-side reward/risk computed inside `score_signal`
  (strategy.py:973-998) from `sl`/`tp`/`entry`, gated against the 1.2 hard floor
  and driving the `rr_X.X` note bonuses. That is Scoring's (Phase 3.9), so
  `RiskPlan.rr` is left `None` here.

## When there is no entry

V1 seeds `sl`/`tp = 0.0` and `tp_source`/`tp_rr = None` (1463-1472) and overwrites
them only on a ready entry. A not-ready entry therefore publishes
`sl=0.0, tp=0.0, tp_source=None, tp_rr=None`, and this stage reproduces that.

## What V1 does — and does not

- `find_liquidity_target` prioritises EQH/EQL clusters, then swing highs/lows,
  then PDH/PDL, scores each by RR against the stop, and is **mode-aware**:
  CONTINUATION prefers the nearest passing target, RETRACEMENT prefers liquidity
  strength. Below the RR floor it still returns the best failing source (with
  `tp=None`), which routes the caller into the 2R fallback.
- **V1 has no implementation** of: trailing stops, break-even logic, partial
  take-profit / scaling out, multiple TP levels, position sizing, per-trade risk
  budgeting, or ATR-based volatility stops beyond the fixed multiples above.
  None are invented here.

## V1 quirks preserved

1. **`find_liquidity_target` re-derives PDH/PDL from the H1 frame** via
   `previous_day_high_low`, independently of the D1-based `htf.pdh`/`pdl`. The two
   can differ; V1 uses the H1-derived pair for targeting, and so does this stage.
2. **The stop passed in wins over the proxy stop.** V1 only falls back to
   `_proxy_stop` when `sl` is None/non-finite/≤0 — which never happens on the
   ready path, since the caller always passes a positive stop. The proxy branch
   is preserved because `find_liquidity_target` is a public method V1 also calls
   with `sl=None` semantics available.
3. **`else` means "not LONG"** in the fallback (SHORT arithmetic), exactly as V1.
4. **`if not tp`** triggers the fallback for both `None` and `0.0`; V1's
   targeting only ever yields `None` or a positive float, so the effect is
   "no target found", preserved verbatim.

Purity: no I/O, no clock, no state, no caches, no numpy (V1's `np.isfinite` on
scalars is `math.isfinite`). ATR is *read* (from `context.atr_last`); nothing
here computes ATR.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.order_block import selected_block
from v2.domain import EntryPlan, OrderBlock, RiskPlan, Swing

# V1 stop multiples (strategy.py:1510/1537 continuation, 1627 retracement).
CONTINUATION_SL_ATR_MULT = 0.30
RETRACEMENT_SL_ATR_MULT = 0.70

# V1 2R fallback multiple (strategy.py:1526/1553/1653).
FALLBACK_RR = 2.0

# V1 `find_liquidity_target` default RR floor (strategy.py:288).
TARGET_MIN_RR = 1.2

# V1 minimum rows for the previous-day lookup (strategy.py:242).
_PDHL_MIN_ROWS = 30

# V1 frame tail used by `find_liquidity_target` (strategy.py:305).
_TARGET_FRAME_TAIL = 180


def _finite(value: float) -> bool:
    """V1's `np.isfinite` on a scalar; math.isfinite is identical here."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def previous_day_high_low(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """V1 `_get_previous_day_high_low` (strategy.py:241-251), verbatim behavior."""
    if df is None or df.empty or len(df) < _PDHL_MIN_ROWS:
        return None
    work = df.loc[:, ["timestamp", "high", "low"]].copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"], utc=False, errors="coerce")
    work = work.dropna(subset=["timestamp", "high", "low"])
    if work.empty:
        return None
    work["day"] = work["timestamp"].dt.floor("d")
    daily = (
        work.groupby("day", sort=True, observed=True)
        .agg(day_high=("high", "max"), day_low=("low", "min"))
        .reset_index()
    )
    if len(daily) < 2:
        return None
    prev_day = daily.iloc[-2]
    return {
        "day": prev_day["day"],
        "pdh": float(prev_day["day_high"]),
        "pdl": float(prev_day["day_low"]),
    }


def cluster_liquidity_levels(
    levels: Sequence[Swing], tolerance: float
) -> List[Dict[str, Any]]:
    """V1 `_cluster_liquidity_levels` (strategy.py:254-277), verbatim behavior.

    Takes domain `Swing`s where V1 took ``{"index","price"}`` dicts; the values
    and the merge rule (adjacent levels within `tolerance`) are identical.
    """
    if not levels:
        return []
    ordered = sorted(levels, key=lambda s: float(s.price))
    clusters: List[List[Swing]] = [[ordered[0]]]
    for item in ordered[1:]:
        last_cluster = clusters[-1]
        last_price = float(last_cluster[-1].price)
        cur_price = float(item.price)
        if abs(cur_price - last_price) <= tolerance:
            last_cluster.append(item)
        else:
            clusters.append([item])
    result = []
    for cluster in clusters:
        prices = [float(s.price) for s in cluster]
        indices = [int(s.index) for s in cluster]
        result.append(
            {
                "level": sum(prices) / len(prices),
                "count": len(cluster),
                "first_index": min(indices),
                "last_index": max(indices),
                "prices": prices,
            }
        )
    return result


def find_liquidity_target(
    frame: pd.DataFrame,
    direction: str,
    entry_price: float,
    swing_highs: Sequence[Swing],
    swing_lows: Sequence[Swing],
    atr: float,
    *,
    sl: Optional[float] = None,
    min_rr: float = TARGET_MIN_RR,
    entry_mode: str = "RETRACEMENT",
) -> Dict[str, Any]:
    """V1 `find_liquidity_target` (strategy.py:279-515), verbatim behavior.

    RR-aware liquidity target selector. EQH/EQL cluster → swing → PDH/PDL, scored
    by RR against the stop, mode-aware. Returns `tp=None` when nothing clears the
    RR floor so the caller can fall back to 2R. Takes domain `Swing`s.
    """
    work = frame.copy().reset_index(drop=True).tail(_TARGET_FRAME_TAIL).reset_index(
        drop=True
    )
    direction = str(direction).upper().strip()
    entry_mode = str(entry_mode).upper().strip()
    entry = float(entry_price)

    if not _finite(entry) or entry <= 0:
        return {"tp": None, "source": None, "rr": None, "reason": "invalid_entry"}

    if entry_mode not in {"CONTINUATION", "RETRACEMENT"}:
        entry_mode = "RETRACEMENT"

    atr = float(atr) if atr is not None and _finite(float(atr)) else 0.0

    daily_levels = previous_day_high_low(work)
    pdh = (
        float(daily_levels["pdh"])
        if daily_levels and daily_levels.get("pdh") is not None
        else None
    )
    pdl = (
        float(daily_levels["pdl"])
        if daily_levels and daily_levels.get("pdl") is not None
        else None
    )

    tolerance = max(atr * 0.15, entry * 0.0015)
    candidates: List[Dict[str, Any]] = []

    def add_candidate(level: float, source: str, score: float):
        if level is None:
            return
        level = float(level)
        if not _finite(level):
            return
        if direction == "LONG" and level <= entry:
            return
        if direction == "SHORT" and level >= entry:
            return
        candidates.append(
            {
                "level": level,
                "source": source,
                "score": float(score),
                "distance": abs(level - entry),
            }
        )

    # -----------------------------
    # Collect liquidity candidates
    # -----------------------------
    if direction == "LONG":
        for c in cluster_liquidity_levels(swing_highs, tolerance):
            level = float(c["level"])
            if level > entry:
                add_candidate(level, "EQH_cluster", 3.0 + (1.0 if c["count"] >= 2 else 0.0))

        for sh in swing_highs:
            level = float(sh.price)
            if level > entry:
                add_candidate(level, "SWING_HIGH", 2.0)

        if pdh is not None and pdh > entry:
            add_candidate(pdh, "PDH", 3.5)

    elif direction == "SHORT":
        for c in cluster_liquidity_levels(swing_lows, tolerance):
            level = float(c["level"])
            if level < entry:
                add_candidate(level, "EQL_cluster", 3.0 + (1.0 if c["count"] >= 2 else 0.0))

        for slw in swing_lows:
            level = float(slw.price)
            if level < entry:
                add_candidate(level, "SWING_LOW", 2.0)

        if pdl is not None and pdl < entry:
            add_candidate(pdl, "PDL", 3.5)

    else:
        return {"tp": None, "source": None, "rr": None, "reason": "invalid_direction"}

    if not candidates:
        return {"tp": None, "source": None, "rr": None, "reason": "no_candidates"}

    # -----------------------------
    # Estimate stop for RR scoring
    # -----------------------------
    def _proxy_stop() -> Optional[float]:
        stop_buffer = max(atr * 0.35, entry * 0.0015)

        if direction == "LONG":
            refs = [
                float(x.price)
                for x in swing_lows
                if float(x.price) < entry and _finite(float(x.price))
            ]
            if pdl is not None and pdl < entry:
                refs.append(float(pdl))
            if refs:
                base = max(refs)
                return base - stop_buffer
            return entry - max(atr * 1.2, entry * 0.003)

        refs = [
            float(x.price)
            for x in swing_highs
            if float(x.price) > entry and _finite(float(x.price))
        ]
        if pdh is not None and pdh > entry:
            refs.append(float(pdh))
        if refs:
            base = min(refs)
            return base + stop_buffer
        return entry + max(atr * 1.2, entry * 0.003)

    if sl is not None and _finite(float(sl)) and float(sl) > 0:
        stop = float(sl)
    else:
        stop = _proxy_stop()

    if not _finite(stop) or stop <= 0:
        return {"tp": None, "source": None, "rr": None, "reason": "invalid_stop"}

    risk = abs(entry - stop)
    if not _finite(risk) or risk <= 0:
        return {"tp": None, "source": None, "rr": None, "reason": "invalid_risk"}

    # -----------------------------
    # Score candidates with RR
    # -----------------------------
    evaluated = []
    for c in candidates:
        if direction == "LONG":
            tp_level = c["level"] - (atr * 0.10)
            if tp_level <= entry:
                tp_level = c["level"]
        else:
            tp_level = c["level"] + (atr * 0.10)
            if tp_level >= entry:
                tp_level = c["level"]

        if not _finite(tp_level) or tp_level <= 0:
            continue

        reward = abs(tp_level - entry)
        if not _finite(reward) or reward <= 0:
            continue

        rr = reward / risk
        if not _finite(rr):
            continue

        # Small mode-aware bias:
        # continuation prefers closer liquidity; retracement keeps liquidity-strength bias.
        mode_score = float(c["score"])
        if entry_mode == "CONTINUATION":
            mode_score = mode_score - 0.25  # tiny normalization so distance dominates more cleanly

        evaluated.append(
            {
                "tp": float(tp_level),
                "source": c["source"],
                "score": mode_score,
                "distance": float(c["distance"]),
                "rr": float(rr),
            }
        )

    if not evaluated:
        return {
            "tp": None,
            "source": None,
            "rr": None,
            "reason": "no_valid_rr_candidates",
            "stop_used": float(stop),
            "entry_mode": entry_mode,
        }

    valid = [x for x in evaluated if x["rr"] >= float(min_rr)]

    if valid:
        if entry_mode == "CONTINUATION":
            # For continuation, prefer the nearest clean liquidity that still passes RR floor.
            best = sorted(
                valid,
                key=lambda x: (
                    x["distance"],
                    -x["score"],
                    -x["rr"],
                    x["tp"] if direction == "LONG" else -x["tp"],
                ),
            )[0]
        else:
            # Retracement stays liquidity-strength first.
            best = sorted(
                valid,
                key=lambda x: (
                    -x["score"],
                    x["distance"],
                    -x["rr"],
                    x["tp"] if direction == "LONG" else -x["tp"],
                ),
            )[0]

        return {
            "tp": float(best["tp"]),
            "source": best["source"],
            "rr": round(float(best["rr"]), 2),
            "stop_used": float(stop),
            "reason": "rr_pass",
            "entry_mode": entry_mode,
        }

    best_fail = sorted(
        evaluated,
        key=lambda x: (
            x["distance"] if entry_mode == "CONTINUATION" else -x["score"],
            -x["score"] if entry_mode == "CONTINUATION" else x["distance"],
            -x["rr"],
            x["tp"] if direction == "LONG" else -x["tp"],
        ),
    )[0]

    return {
        "tp": None,
        "source": best_fail["source"],
        "rr": round(float(best_fail["rr"]), 2),
        "stop_used": float(stop),
        "reason": "rr_below_floor",
        "entry_mode": entry_mode,
    }


def compute_stop(
    entry: EntryPlan,
    direction: Optional[str],
    frame: pd.DataFrame,
    atr_last: float,
    order_blocks: Optional[Sequence[OrderBlock]],
) -> float:
    """The stop V1 places inside the entry engine, recovered from published state.

    Continuation (1510/1537): the pullback bar's low/high ± `atr*0.30`.
    Retracement (1631/1634): the selected block's far edge ± `atr*0.70`.
    Only called on a ready entry, where `pullback_index` / the selected block are
    guaranteed present.
    """
    if entry.entry_mode == "CONTINUATION":
        pb = frame.iloc[entry.pullback_index]
        if direction == "LONG":
            return float(pb["low"]) - (atr_last * CONTINUATION_SL_ATR_MULT)
        return float(pb["high"]) + (atr_last * CONTINUATION_SL_ATR_MULT)

    block = selected_block(order_blocks)
    if direction == "LONG":
        return block.bottom - (atr_last * RETRACEMENT_SL_ATR_MULT)
    return block.top + (atr_last * RETRACEMENT_SL_ATR_MULT)


def _two_r_fallback(direction: Optional[str], entry_price: float, sl: float) -> float:
    """V1's 2R fallback target (strategy.py:1526/1553/1653), verbatim."""
    if direction == "LONG":
        return entry_price + (entry_price - sl) * FALLBACK_RR
    return entry_price - (sl - entry_price) * FALLBACK_RR


def plan_risk(
    entry: EntryPlan,
    direction: Optional[str],
    frame: pd.DataFrame,
    atr_last: float,
    swing_highs: Sequence[Swing],
    swing_lows: Sequence[Swing],
    order_blocks: Optional[Sequence[OrderBlock]],
) -> RiskPlan:
    """V1's stop/target derivation (the risk half of the entry engine), verbatim.

    Produces `sl`/`tp`/`tp_source`/`tp_rr`. The scoring-side `rr` (1.2-floor gate)
    is Scoring's, so `RiskPlan.rr` stays None.
    """
    if not entry.entry_ready:
        # V1's initial state (1463-1472), never overwritten without a ready entry.
        return RiskPlan(sl=0.0, tp=0.0, tp_source=None, tp_rr=None)

    sl = compute_stop(entry, direction, frame, atr_last, order_blocks)

    tp_info = find_liquidity_target(
        frame,
        direction,
        entry.entry_price,
        swing_highs,
        swing_lows,
        atr_last,
        sl=sl,
        entry_mode=entry.entry_mode,
    )

    tp = tp_info.get("tp")
    tp_source = tp_info.get("source")
    tp_rr = tp_info.get("rr")

    if not tp:
        tp = _two_r_fallback(direction, entry.entry_price, sl)
        tp_source = "fallback_2r"
        tp_rr = FALLBACK_RR

    # BUG-1 fix (post-migration, STRATEGY_NOTES.md / REPLAY_CONTRACT.md C-1.3):
    # V1 dropped `tp_source`/`tp_rr` on the CONTINUATION path by accident (a
    # missing `nonlocal` in its nested `_calc_continuation_entry`, strategy.py:
    # 1522-1528 — the assignments never escaped, so the outer Nones were
    # published). The values were always computed; discarding them was a scoping
    # bug, not a design. Both fields are metadata only — nothing branches on
    # them (they flow to alert text, the OB audit, and the signal ledger), so
    # publishing them changes no score, decision, level, or lifecycle outcome.
    return RiskPlan(sl=float(sl), tp=float(tp), tp_source=tp_source, tp_rr=tp_rr)


class RiskStage:
    """Derives the stop-loss, take-profit target, and risk/reward."""

    name = "risk"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        entry = context.entry
        if entry is None or context.atr_last is None:
            # V1 computes no stop/target without an entry decision.
            return context

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return context

        return context.with_(
            risk=plan_risk(
                entry,
                context.htf.direction,
                h1,
                context.atr_last,
                context.swing_highs or (),
                context.swing_lows or (),
                context.order_blocks,
            )
        )
