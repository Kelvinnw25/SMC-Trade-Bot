"""Episode-level inference for the fill-based Δ — EPISODE_INFERENCE_CONT_SHORT.

The one threat that survives every result so far. Δ has cleared the fill-price
correction, the drift-adjusted null, both sensitivity bound families, and
leave-one-symbol-out. **None of those touch dependence.** The nominal +6.62σ on
CONT×SHORT is a trade-level number on a series with lag-1 autocorrelation +0.135
and a top-5-month share of 72.9% against a shuffled median of 37.2%.

This module asks one question: *does Δ survive when the resampling unit is the
dependent block rather than the individual trade?*

## No new estimator

    d_i = won_i - fill_null(i)
    Δ   = mean(d)

because `Δ = (1/n)Σwon - (1/n)Σnull = (1/n)Σ(won - null)`. Δ is therefore a plain
mean and this is a standard block bootstrap **of a mean**. The statistic is
imported from `fill_null`, not restated, so there is exactly one definition of Δ
in the codebase — pinned by test D-11.

## Why blocks are circular (P1)

With start times drawn on `[t0, t_end - T)` a trade at `t0` is covered by a set of
start times of measure zero, and inclusion probability climbs to a plateau only
after `T`. At 3-month blocks that under-weights ~15% of a 41.3-month corpus —
precisely the regions where an episode would be discounted without trace. Circular
wrapping gives every trade inclusion probability exactly `T`. The Dec→Jan adjacency
this creates is harmless: Δ is a mean, so the statistic has no cross-block or
within-block-ordering terms.

## Why the series is re-sorted by entry time (A2)

`c98_order_key` orders by `exit_T` (C-9.8's equity convention). Blocking must be on
the dependence axis, which is when regime exposure *begins*. Re-sorting cannot move
a mean, so the point estimate is untouched — pinned by test D-5.

## What this module does not claim

All intervals cover the **fill-null Δ (+0.04002)**. The drift-adjusted +0.0383 uses
a different null and is not validated here. Figures remain *benchmark-consistent,
not edge estimates*, and no strategy decision follows from any outcome.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from backtest.analytics.studies import fill_null as fn
from backtest.metrics import BOOTSTRAP_SEED, CONFIDENCE, MIN_SEGMENT_N, N_RESAMPLES

__all__ = [
    "BLOCKS",
    "CALIBRATION_GATE",
    "CALIBRATION_REPS",
    "LOMO_STRENGTHEN",
    "PHASE_WEEKS",
    "PRIMARY_BLOCK",
    "STRENGTHEN_MONTH_SHARE",
    "Series",
    "analyse",
    "calibration",
    "circular_block_ci",
    "concentration",
    "lomo",
    "main",
    "monthly_ess",
    "monthly_panel",
    "series_of",
    "sign_test",
    "verdict",
]

#: Pre-declared block durations, in seconds. 1w is descriptive only: the 168h
#: holding cap lets a late-week entry resolve outside its own block. 2m is the
#: honest conservative headline; 3m (K≈14) is a stress bound with a coarse
#: interval.
BLOCKS: tuple[tuple[str, int], ...] = (
    ("1w", 7 * 86400),
    ("2w", 14 * 86400),
    ("1m", 2_630_016),        # 30.44 d
    ("2m", 5_260_032),
    ("3m", 7_890_048),
)
PRIMARY_BLOCK = "1m"

#: Month-boundary phase offsets, in weeks. The sign test is not phase-invariant
#: and the strengthen threshold sits only two months above the rejection
#: threshold, so a phase-dependent verdict voids that criterion (H1).
PHASE_WEEKS: tuple[int, ...] = (0, 1, 2, 3)

STRENGTHEN_MONTH_SHARE = 0.70
LOMO_STRENGTHEN = 0.020
CALIBRATION_REPS = 200
CALIBRATION_GATE = (0.92, 0.98)
SHUFFLE_REPS = 1000
ESS_MAX_LAG = 10


# ---------------------------------------------------------------------------
# series construction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Series:
    """One cell's `barrier_resolved` trades as entry-time-ordered arrays."""

    cell: str
    boundary: np.ndarray      # int64 epoch seconds, ascending
    d: np.ndarray             # won - fill_null, the per-trade Δ contribution
    won: np.ndarray           # 0/1
    null: np.ndarray          # fill_null
    n: int

    @property
    def delta(self) -> float:
        return float(self.d.mean())

    @property
    def span(self) -> int:
        """Half-open, so every trade maps to an offset in [0, span)."""
        return int(self.boundary[-1] - self.boundary[0]) + 1


def _sort_key(record: Any) -> tuple[int, int, int, str]:
    """Entry-time order, mirroring `c98_order_key`'s tie-break structure so the
    ordering is total and deterministic for any input."""
    return (
        int(record.boundary),
        -1 if record.cycle_index is None else int(record.cycle_index),
        -1 if record.universe_rank is None else int(record.universe_rank),
        record.symbol,
    )


def series_of(cell: str, trades: Iterable[Any]) -> Series:
    """Frozen §B3 `barrier_resolved`, re-sorted by entry time."""
    pops = fn.partition(trades)
    pops.check()
    rows = sorted(pops.barrier_resolved, key=_sort_key)
    # `fn._is_win` and `fn.fill_null` are used rather than reimplemented: one
    # definition of Δ in the codebase, pinned by D-11.
    won = np.array([1.0 if fn._is_win(t) else 0.0 for t in rows])
    null = np.array([fn.fill_null(t) for t in rows])
    return Series(cell, np.array([int(t.boundary) for t in rows], dtype=np.int64),
                  won - null, won, null, len(rows))


# ---------------------------------------------------------------------------
# P1 — circular moving-block bootstrap
# ---------------------------------------------------------------------------

def circular_block_ci(boundary: np.ndarray, d: np.ndarray, block_seconds: int, *,
                      seed: int = BOOTSTRAP_SEED,
                      n_resamples: int = N_RESAMPLES,
                      confidence: tuple[float, float] = CONFIDENCE
                      ) -> dict[str, Any]:
    """Percentile CI for `mean(d)` under circular moving blocks on the time axis.

    Blocks are drawn on calendar time rather than trade index because trade
    density varies 5–313 per month, so fixed-index blocks would not correspond to
    fixed durations. `K = round(span/T)` blocks are drawn with replacement and Δ*
    is pooled as `Σd / Σcount`, which reproduces n in expectation and needs no
    arbitrary truncation rule. The random denominator makes Δ* a ratio estimator;
    that is correct here — the observed Δ is itself count-weighted, and denominator
    variability legitimately reflects how many trades a resampled corpus would hold.
    """
    n = int(d.size)
    if n == 0:
        return {"block": None, "point": None, "ci_low": None, "ci_high": None,
                "k_blocks": 0, "excludes_zero": False, "n": 0}

    span = int(boundary[-1] - boundary[0]) + 1
    offset = (boundary - boundary[0]).astype(np.int64)
    prefix = np.concatenate([[0.0], np.cumsum(d)])
    k_blocks = max(1, int(round(span / block_seconds)))

    rng = np.random.default_rng(seed)
    starts = rng.integers(0, span, size=(n_resamples, k_blocks), dtype=np.int64)
    ends = starts + int(block_seconds)
    wraps = ends > span

    lo1 = np.searchsorted(offset, starts, side="left")
    hi1 = np.searchsorted(offset, np.minimum(ends, span), side="left")
    sum_d = prefix[hi1] - prefix[lo1]
    count = (hi1 - lo1).astype(np.float64)

    if wraps.any():
        tail = np.where(wraps, ends - span, 0)
        hi2 = np.searchsorted(offset, tail, side="left")
        hi2 = np.where(wraps, hi2, 0)
        sum_d = sum_d + prefix[hi2]
        count = count + hi2.astype(np.float64)

    total_d = sum_d.sum(axis=1)
    total_n = count.sum(axis=1)
    # I2: a resample drawing only empty blocks is vanishingly improbable but must
    # not produce a nan that silently corrupts a percentile.
    good = total_n > 0
    deltas = total_d[good] / total_n[good]
    low, high = np.percentile(deltas, confidence)
    point = float(d.mean())
    return {
        "block": block_seconds,
        "point": point,
        "ci_low": float(low),
        "ci_high": float(high),
        "k_blocks": k_blocks,
        "n_resamples": int(deltas.size),
        "excludes_zero": bool(low > 0 or high < 0),
        "n": n,
        "seed": seed,
    }


# ---------------------------------------------------------------------------
# monthly panel, sign test, ESS
# ---------------------------------------------------------------------------

def _month_key(epoch: int, phase_seconds: int = 0) -> str:
    moment = datetime.fromtimestamp(int(epoch) - phase_seconds, tz=timezone.utc)
    return f"{moment.year}-{moment.month:02d}"


def monthly_panel(series: Series, *, phase_weeks: int = 0,
                  min_n: int = MIN_SEGMENT_N) -> list[dict[str, Any]]:
    """Per-month Δ, bucketed on `boundary` (entry time, knowable at entry).

    H2: qualification at `min_n` is mildly outcome-adjacent — quiet months hold
    fewer trades and plausibly differ in Δ. One month of 42 is excluded here, so
    the impact is negligible, but the mechanism is real.
    """
    phase = phase_weeks * 7 * 86400
    buckets: dict[str, list[int]] = {}
    for i, epoch in enumerate(series.boundary):
        buckets.setdefault(_month_key(int(epoch), phase), []).append(i)
    rows = []
    for month in sorted(buckets):
        idx = np.array(buckets[month])
        rows.append({
            "month": month,
            "n": int(idx.size),
            "win_rate": float(series.won[idx].mean()),
            "mean_null": float(series.null[idx].mean()),
            "delta": float(series.d[idx].mean()),
            "sum_d": float(series.d[idx].sum()),
            "qualifying": bool(idx.size >= min_n),
        })
    return rows


def sign_test(panel: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Exact one-sided binomial on the count of positive qualifying months.

    Anti-conservative: months are not independent, so this overstates evidence.
    Contained by the conjunction structure — it can only produce a false pass on
    one of three strengthen criteria. The ≥2-month block CI binds.
    """
    qualifying = [row for row in panel if row["qualifying"]]
    m = len(qualifying)
    positive = sum(1 for row in qualifying if row["delta"] > 0)
    if m == 0:
        return {"months": 0, "positive": 0, "share": None, "p_value": None,
                "rejects": False, "reject_threshold": None}
    p_value = sum(math.comb(m, j) for j in range(positive, m + 1)) / 2 ** m
    threshold = next(
        (k for k in range(m + 1)
         if sum(math.comb(m, j) for j in range(k, m + 1)) / 2 ** m < 0.05),
        m + 1)
    return {
        "months": m,
        "positive": positive,
        "share": positive / m,
        "p_value": float(p_value),
        "rejects": bool(p_value < 0.05),
        "reject_threshold": int(threshold),
        "strengthen_threshold": int(math.ceil(STRENGTHEN_MONTH_SHARE * m)),
    }


def monthly_ess(panel: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """P2 — diagnostic only, no threshold attached.

    Computed on the *regularly spaced* monthly Δ series. The trade-index series is
    irregularly spaced (5–313 trades/month), so lag k there denotes no fixed time
    interval and its autocorrelation is uninterpretable. At ~42 points
    `SE(rho_1) ≈ 0.154`, so this is a noisy estimate and is reported as such.
    """
    values = np.array([row["delta"] for row in panel if row["qualifying"]])
    m = int(values.size)
    if m < 4:
        return {"months": m, "rho_1": None, "rho_1_se": None,
                "lags_used": 0, "ess": None, "note": "too few months"}
    x = values - values.mean()
    denom = float((x * x).sum())
    if denom <= 0:
        return {"months": m, "rho_1": None, "rho_1_se": None,
                "lags_used": 0, "ess": float(m), "note": "zero variance"}
    rhos = [float((x[:-k] * x[k:]).sum() / denom)
            for k in range(1, min(ESS_MAX_LAG, m - 1) + 1)]
    used = []
    for rho in rhos:                      # pre-declared: stop at first rho <= 0
        if rho <= 0:
            break
        used.append(rho)
    factor = 1.0 + 2.0 * sum(used)
    return {
        "months": m,
        "rho_1": rhos[0],
        "rho_1_se": float(1.0 / math.sqrt(m)),
        "lags_used": len(used),
        "ess": float(m / factor) if factor > 0 else float(m),
        "note": "diagnostic only; no threshold attached (P2)",
    }


# ---------------------------------------------------------------------------
# P4 — concentration, decomposed
# ---------------------------------------------------------------------------

def _top_k_share(contributions: np.ndarray, k: int) -> Optional[float]:
    positive = contributions[contributions > 0]
    if positive.size == 0:
        return None
    total = float(positive.sum())
    if total <= 0:
        return None
    return float(np.sort(positive)[::-1][:k].sum() / total)


def concentration(series: Series, panel: Sequence[dict[str, Any]], *,
                  seed: int = BOOTSTRAP_SEED, reps: int = SHUFFLE_REPS
                  ) -> dict[str, Any]:
    """Top-k month share against a size-preserving shuffle null.

    P4: the shuffle breaks the trade→month link, and so breaks the *geometry*→month
    link as well as the outcome→month link. Apparent concentration could therefore
    be geometry clustering rather than outcome clustering, so the same statistic is
    reported for `d`, for the win indicator alone, and for `fill_null` alone. The
    three columns separate the sources.
    """
    sizes = np.array([row["n"] for row in panel])
    cuts = np.cumsum(sizes)[:-1]
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {"reps": reps, "series": {}}
    for name, values in (("d", series.d), ("won", series.won),
                         ("fill_null", series.null)):
        observed = np.array([values[idx].sum()
                             for idx in _month_slices(series, panel)])
        # One permutation serves all five k, which is both cheaper and more
        # coherent than redrawing per k.
        draws = np.empty((reps, 5))
        for rep in range(reps):
            contrib = np.array([part.sum() for part
                                in np.split(rng.permutation(values), cuts)])
            for k in range(1, 6):
                share = _top_k_share(contrib, k)
                draws[rep, k - 1] = np.nan if share is None else share
        rows = []
        for k in range(1, 6):
            obs = _top_k_share(observed, k)
            column = draws[:, k - 1]
            p95 = float(np.nanpercentile(column, 95))
            rows.append({
                "k": k,
                "observed_share": obs,
                "shuffle_median": float(np.nanmedian(column)),
                "shuffle_p95": p95,
                "exceeds_shuffle_p95": None if obs is None else bool(obs > p95),
            })
        out["series"][name] = rows
    return out


def _month_slices(series: Series,
                  panel: Sequence[dict[str, Any]]) -> list[np.ndarray]:
    sizes = [row["n"] for row in panel]
    bounds = np.cumsum([0] + sizes)
    return [np.arange(bounds[i], bounds[i + 1]) for i in range(len(sizes))]


# ---------------------------------------------------------------------------
# E1 — leave-one-month-out
# ---------------------------------------------------------------------------

def lomo(series: Series, panel: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Symmetric single-month deletion, every month, never top-N-by-outcome.

    E1: the kill-side threshold (`min Δ ≤ 0`) is **not evaluable** on this data.
    It would require the deleted month to satisfy `Σd_m ≥ Σd`, i.e. mean d ≈ 0.84
    across the largest month — a ~100% win rate at mean effective RR ≥ 5.4 against
    a population median of 1.95. It is recorded as not evaluable, never as passed.
    The strengthen-side threshold (`min Δ > +0.020`) is reachable and binding.
    """
    total_d, n = float(series.d.sum()), series.n
    rows = []
    for row in panel:
        rest_n = n - row["n"]
        if rest_n <= 0:
            continue
        rows.append({"month": row["month"],
                     "delta_without": (total_d - row["sum_d"]) / rest_n})
    deltas = sorted(r["delta_without"] for r in rows)
    worst = min(rows, key=lambda r: r["delta_without"]) if rows else None
    largest_month = max(panel, key=lambda r: r["n"]) if panel else None
    return {
        "months": len(rows),
        "min": deltas[0] if deltas else None,
        "median": deltas[len(deltas) // 2] if deltas else None,
        "max": deltas[-1] if deltas else None,
        "most_influential_month": worst["month"] if worst else None,
        "strengthen_threshold": LOMO_STRENGTHEN,
        "kill_side_evaluable": False,
        "kill_side_note": (
            f"min Δ ≤ 0 requires Σd_m ≥ {total_d:.1f} from a single month; the "
            f"largest holds {largest_month['n'] if largest_month else 0} trades, "
            f"so it is not evaluable on this data (E1)"),
    }


# ---------------------------------------------------------------------------
# P3 — Bernoulli calibration control
# ---------------------------------------------------------------------------

def calibration(series: Series, *, reps: int = CALIBRATION_REPS,
                seed: int = BOOTSTRAP_SEED,
                n_resamples: int = N_RESAMPLES) -> dict[str, Any]:
    """Draw `won_i ~ Bernoulli(fill_null_i)` on the real geometry and timestamps.

    Δ_true = 0 by construction while spacing and block structure are preserved
    exactly, so this measures whether the CI procedure is correctly sized at all.
    CONT×LONG cannot do this job: its Δ = −0.0108 is a real estimate, not a known
    null, so its interval excluding zero would be correct rather than diagnostic.

    I1: the coverage gate binds at the **primary (1-month) blocking only**. At 3m,
    K≈14 makes the percentile interval coarse enough that coverage could fall below
    the gate for reasons of block count rather than machinery defect.
    """
    block = dict(BLOCKS)[PRIMARY_BLOCK]
    rng = np.random.default_rng(seed)
    covered = rejected = 0
    for rep in range(reps):
        won = (rng.random(series.n) < series.null).astype(np.float64)
        d = won - series.null
        interval = circular_block_ci(series.boundary, d, block,
                                     seed=seed + 1 + rep,
                                     n_resamples=n_resamples)
        if interval["ci_low"] <= 0.0 <= interval["ci_high"]:
            covered += 1
        synthetic = Series(series.cell, series.boundary, d, won, series.null,
                           series.n)
        if sign_test(monthly_panel(synthetic))["rejects"]:
            rejected += 1
    coverage = covered / reps
    low, high = CALIBRATION_GATE
    return {
        "reps": reps,
        "blocking": PRIMARY_BLOCK,
        "coverage": coverage,
        "sign_test_rejection_rate": rejected / reps,
        "gate": list(CALIBRATION_GATE),
        "passes": bool(low <= coverage <= high),
    }


# ---------------------------------------------------------------------------
# E3 — verdict engine
# ---------------------------------------------------------------------------

def verdict(intervals: dict[str, dict], sign: dict, lomo_result: dict,
            calib: dict) -> dict[str, Any]:
    """KILL / STRENGTHEN / INCONCLUSIVE, evaluated mechanically.

    Three outcomes, not two. At 41 qualifying months, rejection needs ≥27 positive
    and the strengthen threshold needs ≥29, so 27–28 falls in a pre-declared
    inconclusive band rather than being adjudicated on the day (E3). Strengthen is
    a conjunction of **three**, ESS having been demoted to a diagnostic (E2).
    """
    if not calib["passes"]:
        return {"verdict": "VOID", "reason":
                f"calibration coverage {calib['coverage']:.3f} outside "
                f"{calib['gate']} at {calib['blocking']} blocking; the CI "
                f"procedure is not correctly sized and the primary result is void",
                "kill": {}, "strengthen": {}}

    primary = intervals[PRIMARY_BLOCK]
    kill = {
        "K1_primary_ci_includes_zero": not primary["excludes_zero"],
        "K2_sign_test_fails_to_reject": not sign["rejects"],
        "K3_lomo_min_le_zero": "NOT_EVALUABLE",
    }
    strengthen = {
        "S1_ci_excludes_zero_all_blocks": all(
            i["excludes_zero"] for i in intervals.values()),
        "S2_month_share_ge_70pct": bool(
            sign["share"] is not None and sign["share"] >= STRENGTHEN_MONTH_SHARE),
        "S3_lomo_min_gt_0020": bool(
            lomo_result["min"] is not None
            and lomo_result["min"] > LOMO_STRENGTHEN),
    }
    if any(v is True for v in kill.values()):
        result = "KILL"
    elif all(strengthen.values()):
        result = "STRENGTHEN"
    else:
        result = "INCONCLUSIVE"
    return {"verdict": result, "kill": kill, "strengthen": strengthen,
            "reason": None}


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def analyse(trades: Iterable[Any], cell: str, *, calibrate: bool = True,
            reps: int = CALIBRATION_REPS,
            n_resamples: int = N_RESAMPLES) -> dict[str, Any]:
    series = series_of(cell, trades)
    panel = monthly_panel(series)
    intervals = {name: circular_block_ci(series.boundary, series.d, seconds,
                                         n_resamples=n_resamples)
                 for name, seconds in BLOCKS}
    sign = sign_test(panel)
    phases = {f"+{w}w": sign_test(monthly_panel(series, phase_weeks=w))
              for w in PHASE_WEEKS}
    lomo_result = lomo(series, panel)
    calib = (calibration(series, reps=reps, n_resamples=n_resamples)
             if calibrate else
             {"passes": True, "coverage": None, "gate": list(CALIBRATION_GATE),
              "blocking": PRIMARY_BLOCK, "reps": 0,
              "sign_test_rejection_rate": None, "skipped": True})
    return {
        "cell": cell,
        "n": series.n,
        "delta": series.delta,
        "estimand": "fill-null Δ; the drift-adjusted Δ is a different null and is "
                    "not validated here",
        "labelling": "benchmark-consistent, not edge estimates",
        "intervals": intervals,
        "sign_test": sign,
        "sign_test_by_phase": phases,
        "phase_stable": len({s["rejects"] for s in phases.values()}) == 1,
        "monthly_panel": panel,
        "ess": monthly_ess(panel),
        "concentration": concentration(series, panel),
        "lomo": lomo_result,
        "calibration": calib,
        "verdict": verdict(intervals, sign, lomo_result, calib),
    }


def main(run_dir: Any = None, out_path: Any = None) -> dict[str, Any]:
    import json
    from pathlib import Path

    from backtest.analytics.studies.common import labelled_trades

    if run_dir is None:
        # Never guess — see the identical note in `exit_census.main`. The old
        # default read the first run under `data/runs/official/runs`, which
        # after a contract amendment is a superseded `outcome_config_hash`.
        raise ValueError(
            "episode_inference.main requires an explicit run_dir. Pass one, or "
            "use the command line: python -m backtest.analytics.studies."
            "episode_inference --run <run_dir>")
    trades = [t.record for t in labelled_trades(run_dir)]

    payload: dict[str, Any] = {
        "experiment": "EPISODE_INFERENCE_CONT_SHORT",
        "spec": "BENCHMARK_FIX_SPEC.md v4 populations; approved execution plan "
                "with patches P1-P6 and caveats E1-E3",
        "run": str(getattr(run_dir, "name", run_dir)),
        "cells": {},
    }
    for cell, mode, direction, role in (
            ("CONTINUATION×SHORT", "CONTINUATION", "SHORT", "primary"),
            ("CONTINUATION×LONG", "CONTINUATION", "LONG", "contrast")):
        rows = [t for t in trades
                if t.entry_mode == mode and t.direction == direction]
        result = analyse(rows, cell)
        result["role"] = role
        payload["cells"][cell] = result
        _print(result)

    if out_path is not None:
        Path(out_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=float) + "\n",
            encoding="utf-8")
    return payload


def _print(result: dict[str, Any]) -> None:
    print("=" * 100)
    print(f"{result['cell']}  ({result['role'] if 'role' in result else '—'})  "
          f"n={result['n']}  Δ={result['delta']:+.5f}")
    print("=" * 100)
    print(f"  {'block':>6} {'K':>5} {'CI low':>11} {'CI high':>11}  excludes 0")
    for name, _s in BLOCKS:
        i = result["intervals"][name]
        mark = "YES" if i["excludes_zero"] else "no"
        tag = "  (descriptive only)" if name == "1w" else (
            "  (headline)" if name == "2m" else
            "  (stress bound)" if name == "3m" else
            "  (primary)" if name == PRIMARY_BLOCK else "")
        print(f"  {name:>6} {i['k_blocks']:>5} {i['ci_low']:>+11.5f} "
              f"{i['ci_high']:>+11.5f}  {mark:>10}{tag}")

    s = result["sign_test"]
    print(f"\n  sign test: {s['positive']}/{s['months']} months positive "
          f"({s['share']:.3f}), p={s['p_value']:.5f}, rejects={s['rejects']}  "
          f"[reject≥{s['reject_threshold']}, strengthen≥{s['strengthen_threshold']}]")
    print("  by phase: " + "  ".join(
        f"{k}:{v['positive']}/{v['months']}({'R' if v['rejects'] else '-'})"
        for k, v in result["sign_test_by_phase"].items())
        + f"   phase-stable={result['phase_stable']}")

    lm = result["lomo"]
    print(f"\n  LOMO: min={lm['min']:+.5f} median={lm['median']:+.5f} "
          f"max={lm['max']:+.5f}  worst month={lm['most_influential_month']}  "
          f"(kill-side NOT EVALUABLE, E1)")

    e = result["ess"]
    print(f"  ESS (diagnostic): {e['ess']:.1f} of {e['months']} months, "
          f"rho_1={e['rho_1']:+.3f} ± {e['rho_1_se']:.3f}, "
          f"lags={e['lags_used']}")

    print("\n  concentration, top-k month share vs shuffle p95:")
    for name in ("d", "won", "fill_null"):
        row = result["concentration"]["series"][name][4]
        obs = "—" if row["observed_share"] is None else f"{row['observed_share']:.3f}"
        print(f"    {name:>10}  top-5 observed={obs}  "
              f"shuffle median={row['shuffle_median']:.3f}  "
              f"p95={row['shuffle_p95']:.3f}  "
              f"exceeds={row['exceeds_shuffle_p95']}")

    c = result["calibration"]
    if not c.get("skipped"):
        print(f"\n  calibration ({c['reps']} reps @ {c['blocking']}): "
              f"coverage={c['coverage']:.3f} gate={c['gate']} "
              f"sign-test rejection={c['sign_test_rejection_rate']:.3f}  "
              f"passes={c['passes']}")

    v = result["verdict"]
    print(f"\n  KILL      : {v['kill']}")
    print(f"  STRENGTHEN: {v['strengthen']}")
    print(f"  >>> VERDICT: {v['verdict']}" + (f" — {v['reason']}" if v["reason"] else ""))
    print()


def _cli(argv=None) -> int:
    """`--run` is required, so the run under study is always stated."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="episode_inference",
        description="Block-bootstrap inference over the CONT×SHORT episode Δ.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default="episode_inference.json")
    args = parser.parse_args(argv)
    main(args.run, out_path=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
