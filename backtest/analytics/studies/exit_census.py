"""Exit Feasibility Census — is exit-management research economically viable?

Existing on-disk data only. No re-run, no replay, no tier-1 walker, no capture
schema change. Frozen §B3 `barrier_resolved`, imported from `fill_null` so the
population and `effective_rr` have exactly one definition in the codebase.

## What can and cannot be settled here

Two exit classes in `barrier_resolved` are terminal and `mfe_r` records a touch,
so a partial taken at +xR with the original stop left in place changes the trade
by exactly `f·(x − realized_r)`. That argument establishes only that MFE occurred
**at or before** the exit — not strictly before it. `mfe_r` carries no timestamp,
so where MFE and the terminal exit fall on the same candle the fill is
unobservable, and `tie_break = sl_first` does not adjudicate it because +xR is an
intermediate level rather than the TP.

**D1.** Every ΔE is therefore reported as a bracket: `ΔE_upper` assumes every
partial filled, `ΔE_lower` credits no gain on rows where `holding_candles == 1`
while still charging any cost. The measured ambiguity floor is 14.4% overall and
19.7% in CONT×LONG, and it is a *floor* — a trade holding two or more candles may
still carry its MFE on the exit candle, and that is undetectable. Verdicts bind
on `ΔE_lower`.

    exact         threshold tables · give-back · excursion distributions ·
                  tail census · the ledger on the 85.6% unambiguous subset
    bounded       the ledger on the ambiguous subset, and every verdict by
                  inheritance
    unobservable  MFE/MAE ordering · P(return to entry | reached +1R) · the full
                  path. Break-even is therefore unmeasurable; trailing and
                  volatility-adjusted exits can only inherit the trailing
                  verdict as a stated assumption.

## Why raw means are refused

`r_unit = |fill_price − sl|` is bounded away from zero only by the ordering
predicate, so an interior fill sitting near its stop inflates both excursions
without the market having moved. After excluding `no_race`, `mfe_r` still reaches
+286.53 and `mae_r` −3538.13. One −3538 row moves a 31,209-row mean by −0.113 R,
five times `MATERIAL_R`. Percentiles and the target-fraction normalisation are
immune; the published means are not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from backtest.analytics.studies import fill_null as fn
from backtest.analytics.studies.common import MATERIAL_R, LabelledTrade

__all__ = [
    "BAND",
    "CensusSeries",
    "FRICTION_LEG_BOOK",
    "FRICTION_LEG_CONT_SHORT",
    "PARTIAL_F",
    "PARTIAL_X",
    "SL_CLASS",
    "TAIL_ATR_FLOOR",
    "THRESHOLDS",
    "TP_CLASS",
    "analyse",
    "decide",
    "give_back",
    "main",
    "normalised_excursions",
    "partial_ledger",
    "recoverable_pool",
    "robust_summary",
    "series_of",
    "tail_census",
    "threshold_table",
]

#: Verbatim from `fill_null.BARRIER_REASONS`, partitioned by side. Their union
#: must exhaust it — asserted, so a vocabulary change fails loudly rather than
#: dropping rows from both classes at once.
SL_CLASS = frozenset({"SL", "GAP_SL", "SL_WORST_CASE_M5", "SL_WORST_CASE_NO_M5"})
TP_CLASS = frozenset({"TP", "GAP_TP"})

THRESHOLDS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0)
PARTIAL_X: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)
PARTIAL_F: tuple[float, ...] = (0.25, 0.33, 0.50)

#: The engine's own planned-stop floor (`distances.stop_distance` min = 0.30 ATR).
#: Pre-declared and structural, never a data-driven trim.
TAIL_ATR_FLOOR = 0.30
TRIM = 0.10

#: 136.6 notional/R × 5 bp taker. A partial adds a third leg, so it must clear
#: this much in CONT×SHORT purely to pay for itself.
FRICTION_LEG_CONT_SHORT = 0.0683
#: 88.2 notional/R × 5 bp — book-wide, reported for context only.
FRICTION_LEG_BOOK = 0.0441

#: D5: materially positive but not clearing the extra leg at taker fees. Open
#: interval, so exactly 0.0683 falls through to the KILL test.
BAND = (MATERIAL_R, FRICTION_LEG_CONT_SHORT)

PRIMARY_CELL = "CONTINUATION×SHORT"


# ---------------------------------------------------------------------------
# series
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CensusSeries:
    """One cell's frozen §B3 `barrier_resolved` rows as aligned arrays."""

    cell: str
    n: int
    mfe_r: np.ndarray
    mae_r: np.ndarray
    realized_r: np.ndarray
    effective_rr: np.ndarray
    r_unit: np.ndarray
    atr_last: np.ndarray
    holding_candles: np.ndarray
    is_sl: np.ndarray            # bool
    is_tp: np.ndarray            # bool
    ambiguous: np.ndarray        # bool — holding_candles == 1 (D1)

    @property
    def tail_ok(self) -> np.ndarray:
        """`r_unit ≥ 0.30 · atr_last` — the engine's own planned-stop floor."""
        return self.r_unit >= TAIL_ATR_FLOOR * self.atr_last


def series_of(cell: str, trades: Iterable[LabelledTrade]) -> CensusSeries:
    rows = list(trades)
    records = [t.record for t in rows]
    pops = fn.partition(records)
    pops.check()
    keep = {id(r) for r in pops.barrier_resolved}
    rows = [t for t in rows if id(t.record) in keep]

    unknown = {t.record.exit_reason for t in rows} - (SL_CLASS | TP_CLASS)
    if unknown:
        raise fn.FillNullError(
            f"{cell}: exit reasons outside the SL/TP partition: "
            f"{sorted(map(str, unknown))}. Their union must exhaust "
            f"fill_null.BARRIER_REASONS or rows vanish from both classes.")

    reasons = [t.record.exit_reason for t in rows]
    arr = lambda values: np.asarray(values, dtype=np.float64)  # noqa: E731
    return CensusSeries(
        cell=cell,
        n=len(rows),
        mfe_r=arr([t.record.mfe_r for t in rows]),
        mae_r=arr([t.record.mae_r for t in rows]),
        realized_r=arr([t.record.realized_r for t in rows]),
        effective_rr=arr([fn.effective_rr(t.record) for t in rows]),
        r_unit=arr([abs(float(t.record.fill_price) - float(t.record.sl))
                    for t in rows]),
        atr_last=arr([t.labels.get("atr_last") or np.nan for t in rows]),
        holding_candles=arr([t.record.holding_candles for t in rows]),
        is_sl=np.array([r in SL_CLASS for r in reasons]),
        is_tp=np.array([r in TP_CLASS for r in reasons]),
        ambiguous=arr([t.record.holding_candles for t in rows]) == 1.0,
    )


# ---------------------------------------------------------------------------
# B — robust statistics only
# ---------------------------------------------------------------------------

def robust_summary(values: Sequence[float], *,
                   include_raw_mean: bool = False) -> dict[str, Any]:
    """Median, quartiles, upper tail and a 10% trimmed mean.

    No bare `mean` key is ever emitted. A raw mean appears only under
    `raw_mean_TAIL_CONTAMINATED`, and only when the caller asks for it.
    """
    data = np.asarray(values, dtype=np.float64)
    data = data[np.isfinite(data)]
    if data.size == 0:
        return {"n": 0, "median": None, "p25": None, "p75": None,
                "p90": None, "p95": None, "trimmed_mean_10pct": None}
    ordered = np.sort(data)
    cut = int(ordered.size * TRIM)
    kept = ordered[cut:ordered.size - cut] if ordered.size - 2 * cut > 0 \
        else ordered
    out: dict[str, Any] = {
        "n": int(data.size),
        "median": float(np.median(data)),
        "p25": float(np.percentile(data, 25)),
        "p75": float(np.percentile(data, 75)),
        "p90": float(np.percentile(data, 90)),
        "p95": float(np.percentile(data, 95)),
        "trimmed_mean_10pct": float(kept.mean()),
    }
    if include_raw_mean:
        out["raw_mean_TAIL_CONTAMINATED"] = float(data.mean())
    return out


# ---------------------------------------------------------------------------
# A.1 — threshold table
# ---------------------------------------------------------------------------

def threshold_table(series: CensusSeries,
                    thresholds: Sequence[float] = THRESHOLDS
                    ) -> list[dict[str, Any]]:
    """`P(MFE ≥ xR)`, unrestricted and restricted to `r_unit ≥ 0.30·atr_last`.

    The restriction is mandatory: a fill just inside its stop inflates `mfe_r`
    without the market moving, and that bias falls on the **high** thresholds
    specifically, so `P(MFE ≥ 3R)` is upward-biased by exactly those rows.
    """
    ok = series.tail_ok
    rows = []
    for x in thresholds:
        reached = series.mfe_r >= x
        rows.append({
            "threshold_r": x,
            "n": series.n,
            "p_unrestricted": float(reached.mean()),
            "n_tail_restricted": int(ok.sum()),
            "p_tail_restricted": (float(reached[ok].mean())
                                  if ok.any() else None),
        })
    return rows


# ---------------------------------------------------------------------------
# A.2 — give-back, with the D2 guard
# ---------------------------------------------------------------------------

def give_back(series: CensusSeries) -> dict[str, Any]:
    """`P(stop after reaching +1R)` — joint, conditional, and excess over the
    geometry the trade already implies.

    D2: the reference `(RR−1)/(RR+1)` is gambler's ruin from +1R with barriers at
    −1R and +RR, and is **negative for `effective_rr < 1`**. Those rows are
    structurally different — a target closer than the stop means any row reaching
    +1R has already passed its target — so they are excluded from the reference
    and from `excess`, and reported separately. The conditional used for `excess`
    is computed on the *same* restricted set, so the comparison is like for like.
    """
    reached = series.mfe_r >= 1.0
    n_reached = int(reached.sum())
    usable = reached & (series.effective_rr > 1.0)
    degenerate = reached & (series.effective_rr <= 1.0)

    conditional = (float(series.is_sl[reached].mean())
                   if n_reached else None)
    if usable.any():
        rr = series.effective_rr[usable]
        reference = float(((rr - 1.0) / (rr + 1.0)).mean())
        conditional_restricted = float(series.is_sl[usable].mean())
        excess = conditional_restricted - reference
    else:
        reference = conditional_restricted = excess = None

    return {
        "p_reached_1r": float(reached.mean()),
        "joint_reached_and_stopped": float((reached & series.is_sl).mean()),
        "conditional_unrestricted": conditional,
        "n_reached": n_reached,
        "n_usable_rr_gt_1": int(usable.sum()),
        "n_excluded_rr_le_1": int((series.effective_rr <= 1.0).sum()),
        "n_degenerate_reached_and_rr_le_1": int(degenerate.sum()),
        "conditional_restricted": conditional_restricted,
        "geometric_reference": reference,
        "excess": excess,
    }


# ---------------------------------------------------------------------------
# A.3 — three normalisations
# ---------------------------------------------------------------------------

def normalised_excursions(series: CensusSeries) -> dict[str, Any]:
    """R units (tail-prone), target fraction and ATR units (both tail-immune).

    `mfe_r / effective_rr` cancels `r_unit`, so it is immune to the collapse that
    contaminates the R-unit figures. It answers "how far toward the target".
    """
    scale = series.r_unit / series.atr_last
    return {
        "r_units": {
            "mfe": robust_summary(series.mfe_r, include_raw_mean=True),
            "mae": robust_summary(series.mae_r, include_raw_mean=True),
        },
        "target_fraction": {
            "mfe": robust_summary(series.mfe_r / series.effective_rr),
            "mae": robust_summary(series.mae_r / series.effective_rr),
        },
        "atr_units": {
            "mfe": robust_summary(series.mfe_r * scale),
            "mae": robust_summary(series.mae_r * scale),
        },
    }


# ---------------------------------------------------------------------------
# A+ — recoverable pool (D6) and the bracketed partial ledger (D1)
# ---------------------------------------------------------------------------

def recoverable_pool(series: CensusSeries,
                     thresholds: Sequence[float] = THRESHOLDS
                     ) -> dict[str, Any]:
    """`mfe_r ≥ x ∧ realized_r ≤ 0`, per threshold.

    D6 disclosure: on `barrier_resolved` this set is **identical** to the
    stopped-with-`mfe ≥ x` set that supplies the ledger's gain term, because every
    TP-class row returns `+effective_rr > 0`. It is one measurement viewed twice
    and must not be counted as independent evidence in the KILL conjunction.
    """
    rows = []
    for x in thresholds:
        pool = (series.mfe_r >= x) & (series.realized_r <= 0.0)
        gain_set = (series.mfe_r >= x) & series.is_sl
        rows.append({
            "threshold_r": x,
            "n_recoverable": int(pool.sum()),
            "share_recoverable": float(pool.mean()),
            "identical_to_gain_set": bool(np.array_equal(pool, gain_set)),
        })
    return {
        "rows": rows,
        "disclosure": "D6: identical to the ledger's gain set on this "
                      "population; not independent evidence",
    }


def partial_ledger(series: CensusSeries, xs: Sequence[float] = PARTIAL_X,
                   fs: Sequence[float] = PARTIAL_F) -> list[dict[str, Any]]:
    """ΔE for a partial of fraction `f` at `+xR`, stop left in place.

    Per row, `contribution = f·(x − realized_r)` where `mfe_r ≥ x`, else 0. One
    expression covers both exit classes: a stopped row returns exactly −1.0 on
    this population (verified 100% in all four cells), giving `f·(x + 1)`; a
    target row returns `+effective_rr`, giving `f·(x − effective_rr)`.

    D1 bracket, computed over the whole cell so the denominator never changes:

        ΔE_upper  every partial assumed filled
        ΔE_lower  ambiguous rows (holding_candles == 1) credited no gain but
                  still charged any cost
    """
    rows = []
    for x in xs:
        reached = series.mfe_r >= x
        for f in fs:
            contribution = np.where(reached, f * (x - series.realized_r), 0.0)
            lower = np.where(series.ambiguous,
                             np.minimum(contribution, 0.0), contribution)
            gain = contribution[reached & series.is_sl]
            cost = contribution[reached & series.is_tp]
            rows.append({
                "x": x,
                "f": f,
                "delta_e_upper": float(contribution.mean()),
                "delta_e_lower": float(lower.mean()),
                "gain_term": float(gain.sum() / series.n) if gain.size else 0.0,
                "cost_term": float(cost.sum() / series.n) if cost.size else 0.0,
                "n_reached": int(reached.sum()),
                "n_reached_stopped": int((reached & series.is_sl).sum()),
                "n_reached_targeted": int((reached & series.is_tp).sum()),
                "n_ambiguous_reached": int((reached & series.ambiguous).sum()),
            })
    return rows


def tail_census(series: CensusSeries) -> dict[str, Any]:
    """`r_unit / atr_last`, and the share below the engine's own stop floor."""
    scale = series.r_unit / series.atr_last
    return {
        "r_unit_over_atr": robust_summary(scale),
        "n_below_floor": int((~series.tail_ok).sum()),
        "share_below_floor": float((~series.tail_ok).mean()),
        "floor": TAIL_ATR_FLOOR,
    }


# ---------------------------------------------------------------------------
# C — pre-declared decision framework
# ---------------------------------------------------------------------------

def decide(cells: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """FEE_TIER_BAND → JUSTIFY_REWALK → KILL → INCONCLUSIVE (D5 order).

    Verdicts bind on `ΔE_lower` (D1): a decision to commission a re-walk must not
    rest on the optimistic end of an unobservable.
    """
    primary = cells[PRIMARY_CELL]
    lowers = [row["delta_e_lower"] for row in primary["partial_ledger"]]
    max_lower = max(lowers)

    shares = {name: next(r["share_recoverable"]
                         for r in cell["recoverable_pool"]["rows"]
                         if r["threshold_r"] == 1.0)
              for name, cell in cells.items()}
    excesses = {name: cell["give_back"]["excess"] for name, cell in cells.items()}

    # J3: strictly increasing in x for some f, and materially positive (D3).
    # Strict, not non-decreasing: a *flat* surface satisfies `b >= a` at every
    # step, and a flat surface is the opposite of "the opportunity is in the
    # tail" that J3 exists to detect.
    monotone = False
    for f in PARTIAL_F:
        seq = [row["delta_e_lower"] for row in primary["partial_ledger"]
               if row["f"] == f]
        if len(seq) > 1 and all(b > a for a, b in zip(seq, seq[1:])):
            monotone = True
    j3 = monotone and max_lower >= MATERIAL_R

    j1 = max_lower > FRICTION_LEG_CONT_SHORT                      # D4: strict
    j2 = sum(1 for name in cells
             if shares[name] >= 0.25
             and (excesses[name] is not None and excesses[name] >= 0.05)) >= 2

    k1 = max_lower <= FRICTION_LEG_CONT_SHORT
    k2 = all(share < 0.15 for share in shares.values())
    k3 = all(e is not None and e <= MATERIAL_R for e in excesses.values())

    band = BAND[0] < max_lower < BAND[1]

    if band:
        verdict = "INCONCLUSIVE_FEE_TIER_BAND"
    elif j1 or j2 or j3:
        verdict = "JUSTIFY_REWALK"
    elif k1 and k2 and k3:
        verdict = "KILL"
    else:
        verdict = "INCONCLUSIVE"

    return {
        "verdict": verdict,
        "primary_cell": PRIMARY_CELL,
        "max_delta_e_lower": max_lower,
        "friction_leg": FRICTION_LEG_CONT_SHORT,
        "band": list(BAND),
        "fee_tier_band": band,
        "justify": {"J1_delta_gt_friction": j1,
                    "J2_pool_and_excess_in_2_cells": j2,
                    "J3_monotone_and_material": j3},
        "kill": {"K1_delta_le_friction": k1,
                 "K2_pool_lt_15pct_all_cells": k2,
                 "K3_excess_le_material_all_cells": k3},
        "recoverable_share_at_1r": shares,
        "excess_by_cell": excesses,
        "d6_note": "K1 and K2 are one measurement viewed twice; not independent",
    }


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

CELLS = (
    ("CONTINUATION×SHORT", "CONTINUATION", "SHORT"),
    ("CONTINUATION×LONG", "CONTINUATION", "LONG"),
    ("RETRACEMENT×SHORT", "RETRACEMENT", "SHORT"),
    ("RETRACEMENT×LONG", "RETRACEMENT", "LONG"),
)


def analyse(cell: str, trades: Iterable[LabelledTrade]) -> dict[str, Any]:
    series = series_of(cell, trades)
    return {
        "cell": cell,
        "n": series.n,
        "n_ambiguous": int(series.ambiguous.sum()),
        "share_ambiguous": float(series.ambiguous.mean()),
        "n_stopped": int(series.is_sl.sum()),
        "n_targeted": int(series.is_tp.sum()),
        "threshold_table": threshold_table(series),
        "give_back": give_back(series),
        "excursions": normalised_excursions(series),
        "recoverable_pool": recoverable_pool(series),
        "partial_ledger": partial_ledger(series),
        "tail_census": tail_census(series),
    }


def main(run_dir: Any = None, out_path: Any = None) -> dict[str, Any]:
    import json
    from pathlib import Path

    from backtest.analytics.studies.common import labelled_trades

    if run_dir is None:
        # Never guess. The former default picked the first run under
        # `data/runs/official/runs`, so after a contract amendment moved the
        # current bundle elsewhere this silently produced output from a
        # superseded `outcome_config_hash` — output that looks entirely normal
        # and carries the wrong lineage stamps. C-4.12 forbids mixing configs;
        # a study that cannot say which run it read must refuse to run.
        raise ValueError(
            "exit_census.main requires an explicit run_dir. Pass one, or use "
            "the command line: python -m backtest.analytics.studies."
            "exit_census --run <run_dir>")
    trades = labelled_trades(run_dir)

    cells: dict[str, dict[str, Any]] = {}
    for cell, mode, direction in CELLS:
        rows = [t for t in trades if t.record.entry_mode == mode
                and t.record.direction == direction]
        cells[cell] = analyse(cell, rows)
        _print(cells[cell])

    verdict = decide(cells)
    _print_verdict(verdict)
    payload = {
        "study": "exit_feasibility_census",
        "population": "frozen §B3 barrier_resolved",
        "classification": {
            "exact": "threshold tables · give-back · excursion distributions · "
                     "tail census · ledger on the unambiguous subset",
            "bounded": "ledger on the ambiguous subset; every verdict by "
                       "inheritance (D1)",
            "unobservable": "MFE/MAE ordering; P(return to entry | reached "
                            "+1R); the full path",
        },
        "cells": cells,
        "decision": verdict,
    }
    if out_path is not None:
        Path(out_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=float) + "\n",
            encoding="utf-8")
    return payload


def _n(value: Any, places: int = 4) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _print(c: dict[str, Any]) -> None:
    print("=" * 104)
    print(f"{c['cell']}   n={c['n']}   stopped={c['n_stopped']}  "
          f"targeted={c['n_targeted']}   ambiguous(holding==1)="
          f"{c['n_ambiguous']} ({c['share_ambiguous']*100:.1f}%)")
    print("=" * 104)

    print("  P(MFE >= xR):")
    print(f"    {'x':>5} {'unrestricted':>13} {'tail-restricted':>16} {'n_tail':>8}")
    for r in c["threshold_table"]:
        print(f"    {r['threshold_r']:>5.1f} {r['p_unrestricted']:>13.4f} "
              f"{_n(r['p_tail_restricted']):>16} {r['n_tail_restricted']:>8}")

    g = c["give_back"]
    print(f"\n  give-back: P(reach +1R)={g['p_reached_1r']:.4f}  "
          f"joint={g['joint_reached_and_stopped']:.4f}  "
          f"conditional(all)={_n(g['conditional_unrestricted'])}")
    print(f"    RR>1 subset n={g['n_usable_rr_gt_1']}  "
          f"conditional={_n(g['conditional_restricted'])}  "
          f"geometric ref={_n(g['geometric_reference'])}  "
          f"EXCESS={_n(g['excess'])}")
    print(f"    excluded RR<=1: {g['n_excluded_rr_le_1']} "
          f"(of which reached +1R: {g['n_degenerate_reached_and_rr_le_1']})")

    print("\n  recoverable pool (D6: == gain set):")
    for r in c["recoverable_pool"]["rows"]:
        print(f"    x={r['threshold_r']:>4.1f}  n={r['n_recoverable']:>6}  "
              f"share={r['share_recoverable']:.4f}  "
              f"identical={r['identical_to_gain_set']}")

    print("\n  partial-TP ΔE bracket (verdicts bind on LOWER):")
    print(f"    {'x':>5} {'f':>5} {'ΔE_lower':>10} {'ΔE_upper':>10} "
          f"{'gain':>9} {'cost':>9} {'n_amb':>7}")
    for r in c["partial_ledger"]:
        print(f"    {r['x']:>5.1f} {r['f']:>5.2f} {r['delta_e_lower']:>+10.5f} "
              f"{r['delta_e_upper']:>+10.5f} {r['gain_term']:>+9.4f} "
              f"{r['cost_term']:>+9.4f} {r['n_ambiguous_reached']:>7}")

    e = c["excursions"]
    print("\n  excursions (robust):")
    for unit in ("r_units", "target_fraction", "atr_units"):
        for side in ("mfe", "mae"):
            s = e[unit][side]
            raw = s.get("raw_mean_TAIL_CONTAMINATED")
            extra = f"  raw_mean={raw:+.3f} [CONTAMINATED]" if raw is not None else ""
            print(f"    {unit:>16} {side}: med={_n(s['median']):>9} "
                  f"p25={_n(s['p25']):>9} p75={_n(s['p75']):>9} "
                  f"p90={_n(s['p90']):>9} p95={_n(s['p95']):>9} "
                  f"trim10={_n(s['trimmed_mean_10pct']):>9}{extra}")

    t = c["tail_census"]
    print(f"\n  tail census: r_unit/ATR med={_n(t['r_unit_over_atr']['median'])} "
          f"p25={_n(t['r_unit_over_atr']['p25'])} "
          f"below {t['floor']} floor: {t['n_below_floor']} "
          f"({t['share_below_floor']*100:.1f}%)")
    print()


def _print_verdict(v: dict[str, Any]) -> None:
    print("=" * 104)
    print("DECISION  (FEE_TIER_BAND -> JUSTIFY_REWALK -> KILL -> INCONCLUSIVE)")
    print("=" * 104)
    print(f"  primary cell            : {v['primary_cell']}")
    print(f"  max ΔE_lower            : {v['max_delta_e_lower']:+.5f}")
    print(f"  friction per extra leg  : {v['friction_leg']:+.5f}")
    print(f"  fee-tier band {v['band']}: {v['fee_tier_band']}")
    print(f"  JUSTIFY : {v['justify']}")
    print(f"  KILL    : {v['kill']}")
    print(f"  pool @1R: " + "  ".join(
        f"{k.split('×')[0][:4]}×{k.split('×')[1][:1]}={s:.3f}"
        for k, s in v["recoverable_share_at_1r"].items()))
    print(f"  excess  : " + "  ".join(
        f"{k.split('×')[0][:4]}×{k.split('×')[1][:1]}={_n(e)}"
        for k, e in v["excess_by_cell"].items()))
    print(f"\n  >>> VERDICT: {v['verdict']}")


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """`--run` is required, so the run under study is always stated."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="exit_census",
        description="Exit/target census of a finished replay bundle.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default="exit_census.json")
    args = parser.parse_args(argv)
    main(args.run, out_path=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
