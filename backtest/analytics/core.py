"""Phase 1 analytics — research assembly over a finished, audited bundle.

REPLAY_CONTRACT.md §11 step 5, §12. Milestone M14.

## What this module is for

The Metrics Engine answers "what are the numbers". This layer answers the
questions a *researcher* asks before touching the strategy: how long was the
drawdown, did it recover, is the edge stable month to month, and — the one the
metrics engine structurally cannot answer — what did the strategy **decline**.

## The one rule that shapes everything here

**It does not compute a metric that already exists.** Expectancy, win rate,
profit factor, drawdown depth, the equity path and the streaks are all owned by
`backtest.metrics`, and every one of them is *copied* out of `metrics.json`
rather than recomputed. Two implementations of expectancy would eventually
disagree, and the disagreement would be discovered by someone quoting the wrong
one. So this module reads the official artifact as plain JSON and re-emits it
verbatim — a test asserts the copies are identical, which turns "we did not
duplicate the maths" from a promise into a property.

That is also why the four calendar and session dimensions live in
`backtest/metrics/segments.py` and not here. Registering them there gives every
one of the 44 metrics across those dimensions, with bootstrap CIs and
`inferential` flags, from one implementation. Computing monthly expectancy here
would have been the fifth line of a slippery slope.

## What it therefore does compute

Only what is genuinely absent, and all of it derived from official values:

1. **drawdown episodes and recovery** — the metrics engine records the depth and
   endpoints of the single deepest drawdown; the *distribution* of episodes and
   whether each recovered is not a per-segment metric and has no home there.
2. **calendar consistency** — dispersion across months, which is a statement
   about the segments rather than a metric within one.
3. **the signal funnel** — decisions that never became trades. `OutcomeRecord`
   does not exist for them, so no metric can see them.
4. **setup x direction** — an interaction. The registry segments one dimension at
   a time by design; crossing two is a research question, not a dimension.

## Provenance is in the schema, not the prose

Every block carries `source: "official" | "derived"`. A reader never has to
guess whether a number came from the engine or from this file, and a derived
number that closes against an official one says so (`closes_against`).

## Boundaries

Reader-only. It writes exclusively into its own output directory and never
touches `metrics.json`, `report.md`, `segments.csv` or any replay artifact, so a
bundle stays byte-identical and keeps passing `backtest.cli audit`.

It imports no `v2` module and nothing from `backtest.replay`, both pinned by
test — the same rule `backtest/audit.py` follows. (Both packages still *load*
`v2` transitively, because `backtest/outcome/__init__.py` imports the engine;
the enforceable and enforced rule is that no strategy module is named here.)
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from backtest.analytics import series as _series
from backtest.analytics.exports import CHARTS_DIRNAME, write_csvs
from backtest.analytics.research import evaluate_hypotheses
from backtest.dataset import (
    OUTCOMES_FILENAME,
    DatasetError,
    SignalPopulation,
    read_outcomes,
)
from backtest.lineage import MANIFEST_FILENAME
from backtest.metrics import (
    METRIC_ENGINE_VERSION,
    METRICS_FILENAME,
    MIN_SEGMENT_N,
    MULTIPLE_COMPARISON_THRESHOLD,
    Populations,
)

__all__ = [
    "ANALYTICS_DIRNAME",
    "ANALYTICS_JSON",
    "ANALYTICS_MD",
    "ANALYTICS_SCHEMA_VERSION",
    "AnalyticsError",
    "AnalyticsReport",
    "DRAWDOWN_EPISODE_MIN_R",
    "analyse_run",
]

ANALYTICS_SCHEMA_VERSION = 1
ANALYTICS_ENGINE_VERSION = 1

ANALYTICS_DIRNAME = "analytics"
ANALYTICS_JSON = "analytics.json"
ANALYTICS_MD = "analytics.md"

H1_SECONDS = 3600

#: Episodes shallower than this are counted but not listed. A 31,209-trade curve
#: has thousands of one-trade dips; listing them would bury the four that matter.
DRAWDOWN_EPISODE_MIN_R = 10.0

#: How many rows a ranked table carries.
_TOP_N = 10


class AnalyticsError(Exception):
    """The bundle cannot be analysed — a missing prerequisite, never a finding."""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class AnalyticsReport:
    """The assembled analytics for one run."""

    identity: dict[str, Any] = field(default_factory=dict)
    populations: dict[str, Any] = field(default_factory=dict)
    equity: dict[str, Any] = field(default_factory=dict)
    calendar: dict[str, Any] = field(default_factory=dict)
    sessions: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)
    signal_funnel: dict[str, Any] = field(default_factory=dict)
    caveats: dict[str, Any] = field(default_factory=dict)
    # -- Phase 2 (M15) ---------------------------------------------------
    outcomes: dict[str, Any] = field(default_factory=dict)
    distributions: dict[str, Any] = field(default_factory=dict)
    rolling: dict[str, Any] = field(default_factory=dict)
    symbols: dict[str, Any] = field(default_factory=dict)
    research: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_series: bool = True) -> dict[str, Any]:
        equity = dict(self.equity)
        if not include_series:
            curve = dict(equity.get("curve") or {})
            curve.pop("series", None)
            equity["curve"] = curve
        return {
            "schema_version": ANALYTICS_SCHEMA_VERSION,
            "analytics_engine_version": ANALYTICS_ENGINE_VERSION,
            "identity": self.identity,
            "populations": self.populations,
            "equity": equity,
            "calendar": self.calendar,
            "sessions": self.sessions,
            "strategy": self.strategy,
            "outcomes": self.outcomes,
            "distributions": self.distributions,
            "rolling": self.rolling,
            "symbols": self.symbols,
            "research": self.research,
            "signal_funnel": self.signal_funnel,
            "caveats": self.caveats,
        }

    def write(self, out_dir: Path | str, *, csvs: bool = True
              ) -> dict[str, Path]:
        """Emit every artifact. Creates `out_dir`; touches nothing else."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / ANALYTICS_JSON
        md_path = out / ANALYTICS_MD
        json_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        written = {"analytics": json_path, "report": md_path}
        if csvs:
            for name, path in write_csvs(self, out / CHARTS_DIRNAME).items():
                written[f"csv:{name}"] = path
        return written

    # -- presentation ------------------------------------------------------

    def to_markdown(self) -> str:
        return _markdown(self)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyse_run(run_dir: Path | str) -> AnalyticsReport:
    """Assemble Phase 1 analytics for a finished run.

    Requires the two upstream stages to have run. §11 keeps the pipeline as
    separate commands on purpose, so this refuses rather than quietly invoking
    them: replaying is hours, resolving outcomes is minutes, and a command that
    silently does either is a command that surprises the person running it.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise AnalyticsError(f"{run_dir} is not a directory")

    metrics_path = run_dir / METRICS_FILENAME
    if not metrics_path.exists():
        raise AnalyticsError(
            f"no {METRICS_FILENAME} in {run_dir}. Analytics reads the metrics "
            f"engine's output rather than recomputing it, so the metrics stage "
            f"must run first:\n"
            f"    python -m backtest.cli metrics --run {run_dir}")
    outcomes_path = run_dir / OUTCOMES_FILENAME
    if not outcomes_path.exists():
        raise AnalyticsError(
            f"no {OUTCOMES_FILENAME} in {run_dir}. The Outcome Engine must "
            f"resolve the run's trades first (§11 step 3):\n"
            f"    python -m backtest.cli outcome --corpus <CORPUS> "
            f"--run {run_dir}")

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    manifest = _read_optional_json(run_dir / MANIFEST_FILENAME)
    records = read_outcomes(outcomes_path)
    scored = Populations.of(records).scored
    try:
        signals = SignalPopulation.from_run(run_dir)
    except DatasetError as exc:
        raise AnalyticsError(str(exc)) from exc

    # C-9.8 order: the same sequence the official equity curve walks, so every
    # path statistic below describes the run that actually happened.
    from backtest.metrics import c98_order_key

    ordered = sorted(
        [record for record in scored if record.realized_r is not None],
        key=c98_order_key)

    report = AnalyticsReport()
    report.identity = _identity(metrics, manifest, run_dir)
    report.populations = _populations(metrics, signals)
    report.equity = _equity(metrics)
    report.calendar = _calendar(metrics, scored)
    report.sessions = _sessions(metrics)
    report.strategy = _strategy(metrics, scored, signals)
    report.outcomes = _outcomes(metrics, signals)
    report.distributions = _distributions(ordered)
    report.rolling = _series.rolling_statistics(ordered)
    report.symbols = _symbols(metrics)
    report.research = evaluate_hypotheses(ordered)
    report.signal_funnel = _funnel(metrics, signals)
    report.caveats = _caveats(metrics, manifest)
    return report


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _identity(metrics: dict, manifest: Optional[dict],
              run_dir: Path) -> dict[str, Any]:
    lineage = metrics.get("lineage") or {}
    core = ((manifest or {}).get("core") or {})
    window = lineage.get("window") or {}
    return {
        "source": "official",
        "run_dir": str(run_dir),
        "run_id": _single(lineage.get("run_ids")),
        "strategy_version": _single(lineage.get("strategy_versions")),
        "contract_version": _single(lineage.get("contract_versions")),
        "data_snapshot_id": _single(lineage.get("data_snapshot_ids")),
        "outcome_config_hash": _single(lineage.get("outcome_config_hashes")),
        "metrics_hash": metrics.get("metrics_hash"),
        "metrics_engine_version": metrics.get("metrics_engine_version"),
        "analytics_engine_version": ANALYTICS_ENGINE_VERSION,
        "universe_id": (manifest or {}).get("universe_id"),
        "window": {
            "T_0": window.get("declared_start") or window.get("first_boundary"),
            "T_n": window.get("declared_end") or window.get("last_boundary"),
            "first_boundary": window.get("first_boundary"),
            "last_boundary": window.get("last_boundary"),
            "cycles": (manifest or {}).get("cycles"),
        },
        "burn_in_h1": lineage.get("burn_in_h1"),
        "universe_mode": lineage.get("universe_mode"),
        "equity_convention": "1R",
        "costs": {
            "note": "expectancy_net equals expectancy in this run, so the "
                    "configured fee, slippage and funding are all zero: every "
                    "figure is GROSS of trading costs",
            "gross_of_costs": _is_gross(metrics),
        },
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }


def _is_gross(metrics: dict) -> bool:
    values = (metrics.get("global") or {}).get("values") or {}
    gross = values.get("expectancy")
    net = values.get("expectancy_net")
    return gross is not None and gross == net


# ---------------------------------------------------------------------------
# Populations
# ---------------------------------------------------------------------------

def _populations(metrics: dict, signals: SignalPopulation) -> dict[str, Any]:
    counts = (metrics.get("global") or {}).get("counts") or {}
    return {
        "source": "official",
        "trades": {name: counts.get(name) for name in (
            "all_records", "total_trades", "completed_trades", "scored_trades",
            "pending_trades", "shadow_trades", "burn_in_trades",
            "invalid_levels_trades", "filled_trades")},
        "signals": signals.to_dict(),
        "note": "`trades` counts OutcomeRecords; `signals` counts ledger rows. "
                "A SKIP or WATCH decision produces a ledger row and no trade, so "
                "the two populations are not nested subsets of one number.",
    }


# ---------------------------------------------------------------------------
# Equity — official values plus the episode analysis
# ---------------------------------------------------------------------------

def _equity(metrics: dict) -> dict[str, Any]:
    curve = (metrics.get("global") or {}).get("equity") or {}
    series = [(int(t), float(v)) for t, v in (curve.get("series") or [])]
    episodes, in_drawdown = _series.drawdown_episodes(series)
    listed = [e for e in episodes if e["depth_r"] >= DRAWDOWN_EPISODE_MIN_R]
    listed.sort(key=lambda e: -e["depth_r"])
    unrecovered = [e for e in episodes if not e["recovered"]]
    summary = _series.drawdown_summary(episodes, in_drawdown)

    return {
        "official": {
            "source": "official",
            "final_r": curve.get("final_r"),
            "total_r": curve.get("final_r"),
            "max_drawdown_r": curve.get("max_drawdown_r"),
            "max_runup_r": curve.get("max_runup_r"),
            "max_consecutive_wins": curve.get("max_consecutive_wins"),
            "max_consecutive_losses": curve.get("max_consecutive_losses"),
            "trades": curve.get("trades"),
            "max_drawdown_start_exit_T": curve.get("max_drawdown_start_exit_T"),
            "max_drawdown_end_exit_T": curve.get("max_drawdown_end_exit_T"),
            "note": "total_r and final_r are the same quantity under the 1R "
                    "convention: the curve is a sum of R, never compounded "
                    "(A25 — no position sizing exists).",
        },
        "curve": {
            "source": "official",
            "order_key": "(exit_T, cycle_index, universe_rank, symbol)",
            "clause": "C-9.8",
            "points": len(series),
            "series": [[t, v] for t, v in series],
        },
        "drawdown_episodes": {
            "source": "derived",
            "closes_against": "equity.official.max_drawdown_r",
            "definition": "peak-to-peak. An episode opens when the curve falls "
                          "below its running peak and closes when that peak is "
                          "regained; an episode still open at the last trade is "
                          "`recovered: false` and has no recovery time.",
            "min_listed_r": DRAWDOWN_EPISODE_MIN_R,
            "episodes_total": len(episodes),
            "episodes_listed": len(listed),
            "deepest_r": max((e["depth_r"] for e in episodes), default=0.0),
            "unrecovered": len(unrecovered),
            "episodes": listed[:_TOP_N],
            "all": episodes,
            "summary": summary,
        },
        "recovery": {
            "source": "derived",
            "time_in_drawdown_share": in_drawdown,
            "unrecovered_episodes": len(unrecovered),
            "longest_drawdown_seconds": max(
                (e["duration_seconds"] for e in episodes), default=0),
            "longest_recovery_seconds": max(
                (e["recovery_seconds"] for e in episodes
                 if e["recovery_seconds"] is not None), default=None),
            "median_recovery_seconds": _median(
                [e["recovery_seconds"] for e in episodes
                 if e["recovery_seconds"] is not None]),
            "average_depth_r": summary["average_depth_r"],
        },
    }


# ---------------------------------------------------------------------------
# Phase 2 blocks (M15)
# ---------------------------------------------------------------------------

#: How the requested outcome vocabulary maps onto what the artifacts record.
#: Stated as data rather than prose so a reader can see immediately which of
#: their words exist in the dataset and which do not.
_OUTCOME_MAP = {
    "TP": {"dimension": "exit_reason", "keys": ["TP", "GAP_TP"]},
    "SL": {"dimension": "exit_reason",
           "keys": ["SL", "GAP_SL", "SL_WORST_CASE_M5", "SL_WORST_CASE_NO_M5"]},
    "TIMEOUT": {"dimension": "exit_reason",
                "keys": ["MAX_HOLDING", "END_OF_DATA"]},
    "KILL": {"dimension": "exit_reason", "keys": ["HTF_KILL"]},
    "DECAY": {"dimension": "cancel_reason", "keys": ["DECAY"]},
}

#: Requested breakdowns with no counterpart in the outcome artifacts.
_OUTCOME_UNAVAILABLE = {
    "PARTIAL_REFRESH": (
        "a PartialRefreshMutation changes an ActiveSignal in place and closes "
        "no trade, so it produces no OutcomeRecord and no exit_reason. It is "
        "recorded in lifecycle_events.jsonl, which the capture layer owns and "
        "this phase is not permitted to modify a reader for. Counted here as "
        "unavailable rather than guessed."),
}


def _outcomes(metrics: dict, signals: SignalPopulation) -> dict[str, Any]:
    """Exit and cancel accounting, grouped into the requested vocabulary."""
    exit_segments = {row["key"]: row
                     for row in _segments_of(metrics, "exit_reason")}
    cancel_segments = {row["key"]: row
                       for row in _segments_of(metrics, "cancel_reason")}
    grouped = []
    for label, spec in _OUTCOME_MAP.items():
        source = (exit_segments if spec["dimension"] == "exit_reason"
                  else cancel_segments)
        members = [source[key] for key in spec["keys"] if key in source]
        total_n = sum(row["n"] for row in members)
        # Weighted by n: a mean of means would over-weight a 62-trade bucket
        # against a 19,951-trade one.
        weighted = (sum((row["expectancy"] or 0.0) * row["n"]
                        for row in members) / total_n) if total_n else None
        grouped.append({
            "outcome": label, "dimension": spec["dimension"],
            "members": spec["keys"], "present": [row["key"] for row in members],
            "n": total_n, "expectancy": weighted,
            "net_r": (weighted * total_n) if weighted is not None else None,
        })
    return {
        "source": "official",
        "note": "grouped from the official exit_reason and cancel_reason "
                "dimensions; the grouping is a reporting convention and the "
                "raw dimensions are reported beside it",
        "grouped": grouped,
        "by_exit_reason": _segments_of(metrics, "exit_reason"),
        "by_cancel_reason": _segments_of(metrics, "cancel_reason"),
        "unavailable": _OUTCOME_UNAVAILABLE,
        "rejection_reasons": {
            "source": "derived",
            "basis": "ledger `reason`, over every analysed row",
            "note": "why analysis declined to produce an entry. These rows "
                    "have no trade and therefore no expectancy — counts only.",
            "counts": signals.by_reason,
            "by_entry_mode": signals.by_mode_reason,
        },
    }


def _distributions(ordered: list[Any]) -> dict[str, Any]:
    return {
        "r_multiple": _series.r_distribution(ordered),
        "duration": _series.duration_distribution(ordered),
        "holding": _series.holding_statistics(ordered),
        "streaks": _series.streaks(ordered),
    }


def _symbols(metrics: dict) -> dict[str, Any]:
    segments = _segments_of(metrics, "symbol")
    return {
        "leaderboard": _series.leaderboard(segments),
        "contributors": _series.contributors(segments),
    }


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _calendar(metrics: dict, scored: Iterable[Any]) -> dict[str, Any]:
    """Official calendar segments, plus the realisation-basis series and the
    consistency statistics computed over it.

    Two bases, both reported, neither silently substituted for the other. The
    registry buckets on `boundary` (when the trade was taken); the equity path is
    ordered by `exit_T` (when the R landed). Only the second closes against
    `final_r`, so only the second may be summed.
    """
    monthly = _segments_of(metrics, "month")
    yearly = _segments_of(metrics, "year")
    quarterly = _segments_of(metrics, "quarter")

    realised = _by_period(scored, "%Y-%m")
    realised_year = _by_period(scored, "%Y")

    return {
        "monthly": {"source": "official", "dimension": "month",
                    "basis": "boundary (instantiation)", "segments": monthly},
        "yearly": {"source": "official", "dimension": "year",
                   "basis": "boundary (instantiation)", "segments": yearly},
        "quarterly": {"source": "official", "dimension": "quarter",
                      "basis": "boundary (instantiation)",
                      "segments": quarterly},
        "equity_calendar": {
            "source": "derived",
            "basis": "exit_T (realisation)",
            "closes_against": "equity.official.final_r",
            "note": "the only calendar whose net_r sums to the equity curve, "
                    "because the curve is ordered by exit_T (C-9.8)",
            "monthly": realised,
            "yearly": realised_year,
            "net_r_total": sum(row["net_r"] for row in realised),
        },
        "consistency": _consistency(realised),
    }


def _by_period(scored: Iterable[Any], fmt: str) -> list[dict[str, Any]]:
    """Group scored trades by a `strftime` bucket of `exit_T`, and sum R.

    Counts and sums only. Nothing here is a metric the engine defines — `net_r`
    is the equity path re-partitioned, which is exactly the quantity the official
    per-segment `expectancy` cannot give (it is a mean, and means do not sum to a
    curve).
    """
    buckets: dict[str, dict[str, Any]] = {}
    for record in scored:
        stamp = getattr(record, "exit_T", None)
        value = getattr(record, "realized_r", None)
        if stamp is None or value is None:
            continue
        key = _utc(int(stamp)).strftime(fmt)
        row = buckets.setdefault(key, {"key": key, "n": 0, "net_r": 0.0,
                                       "wins": 0})
        row["n"] += 1
        row["net_r"] += float(value)
        if getattr(record, "state", None) == "WIN":
            row["wins"] += 1
    rows = []
    for key in sorted(buckets):
        row = buckets[key]
        # Deliberately unrounded. These are the only values in the artifact that
        # must *sum* to an official one, and rounding 43 of them to 6 places puts
        # ~2e-6 of accumulated error between this series and `final_r`. Rounding
        # is a presentation concern and lives in the markdown writer.
        rows.append({
            "key": key,
            "n": row["n"],
            "net_r": row["net_r"],
            "avg_r": (row["net_r"] / row["n"]) if row["n"] else None,
            "win_rate": (row["wins"] / row["n"]) if row["n"] else None,
        })
    return rows


def _consistency(monthly: list[dict[str, Any]]) -> dict[str, Any]:
    """Dispersion across months — a statement *about* the segments.

    `signal_to_noise` is the mean monthly net R over its standard deviation. It
    is reported because a positive mean with a far larger sigma is not an edge
    anyone can trade, and the ratio makes that visible in one number.
    """
    if not monthly:
        return {"source": "derived", "months_total": 0}
    values = [row["net_r"] for row in monthly]
    positive = [v for v in values if v > 0]
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    best = max(monthly, key=lambda row: row["net_r"])
    worst = min(monthly, key=lambda row: row["net_r"])
    return {
        "source": "derived",
        "basis": "exit_T (realisation)",
        "months_total": len(monthly),
        "months_positive": len(positive),
        "positive_share": round(len(positive) / len(monthly), 6),
        "monthly_net_r_mean": round(mean, 6),
        "monthly_net_r_stdev": round(stdev, 6),
        "signal_to_noise": round(mean / stdev, 6) if stdev else None,
        "best": {"key": best["key"], "net_r": best["net_r"], "n": best["n"]},
        "worst": {"key": worst["key"], "net_r": worst["net_r"],
                  "n": worst["n"]},
        "longest_positive_run": _longest_run(values, positive_run=True),
        "longest_negative_run": _longest_run(values, positive_run=False),
    }


def _longest_run(values: list[float], *, positive_run: bool) -> int:
    best = current = 0
    for value in values:
        if (value > 0) if positive_run else (value < 0):
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _sessions(metrics: dict) -> dict[str, Any]:
    from backtest.metrics.segments import SESSIONS

    return {
        "by_hour": {"source": "official", "dimension": "hour_of_day",
                    "basis": "boundary (instantiation)",
                    "segments": _segments_of(metrics, "hour_of_day")},
        "by_session": {
            "source": "official", "dimension": "session",
            "basis": "boundary (instantiation)",
            "definition": {name: f"{start:02d}:00-{end:02d}:00 UTC"
                           for name, start, end in SESSIONS},
            "note": "a reporting grouping of the clock; nothing in the strategy "
                    "knows these names",
            "segments": _segments_of(metrics, "session")},
        "by_weekday": {"source": "official", "dimension": "weekday",
                       "segments": _segments_of(metrics, "weekday")},
        "sniper_gate": {
            "source": "official", "dimension": "session_active",
            "definition": "Mon-Fri 07:00-22:00 UTC "
                          "(v2/app/orchestrator.py:376) — the strategy's own "
                          "time gate, not a reporting convention",
            "clause": "C-5.4",
            "segments": _segments_of(metrics, "session_active")},
    }


# ---------------------------------------------------------------------------
# Strategy breakdown
# ---------------------------------------------------------------------------

#: The execution modes the strategy can emit
#: ([regime.py:152](v2/analysis/regime.py#L152)). Listed in full so a mode that
#: never fires is reported as absent rather than silently missing from a table.
ENTRY_MODES = ("CONTINUATION", "RETRACEMENT", "WATCH_ONLY", "NONE")


def _strategy(metrics: dict, scored: Iterable[Any],
              signals: SignalPopulation) -> dict[str, Any]:
    setup = _segments_of(metrics, "entry_mode")
    present = {row["key"] for row in setup}
    missing = {}
    for mode in ENTRY_MODES:
        if mode in present:
            continue
        in_ledger = signals.by_entry_mode.get(mode, 0)
        missing[mode] = {
            "n": 0,
            "ledger_rows": in_ledger,
            "unreachable_in_bundle": in_ledger == 0,
            "why": ("the strategy emitted this mode but no trade was "
                    "instantiated from it" if in_ledger else
                    "the strategy never emitted this mode in this run"),
        }

    return {
        "by_setup": {"source": "official", "dimension": "entry_mode",
                     "segments": setup, "modes_absent": missing},
        "by_direction": {"source": "official", "dimension": "direction",
                         "segments": _segments_of(metrics, "direction")},
        "by_tier": {"source": "official", "dimension": "alert_tier",
                    "segments": _segments_of(metrics, "alert_tier")},
        "by_score_band": {"source": "official", "dimension": "score_band",
                          "segments": _segments_of(metrics, "score_band")},
        "score_distribution": {
            "source": "derived",
            "basis": "ledger score, all analysed rows",
            "note": "the ledger scores every analysed symbol-cycle, including "
                    "the SKIPs that never became trades; the official "
                    "score_band segments cover instantiated trades only",
            "counts": signals.by_score},
        "by_setup_direction": {
            "source": "derived",
            "note": "an interaction. The registry segments one dimension at a "
                    "time by design; crossing two is a research question.",
            "cells": _setup_direction(scored)},
        "collinear_dimensions": _collinearity(metrics),
    }


def _setup_direction(scored: Iterable[Any]) -> list[dict[str, Any]]:
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for record in scored:
        mode = getattr(record, "entry_mode", None)
        direction = getattr(record, "direction", None)
        value = getattr(record, "realized_r", None)
        if not mode or not direction or value is None:
            continue
        cell = cells.setdefault((str(mode), str(direction)),
                                {"n": 0, "net_r": 0.0, "wins": 0})
        cell["n"] += 1
        cell["net_r"] += float(value)
        if getattr(record, "state", None) == "WIN":
            cell["wins"] += 1
    rows = []
    for (mode, direction) in sorted(cells):
        cell = cells[(mode, direction)]
        rows.append({
            "entry_mode": mode, "direction": direction, "n": cell["n"],
            "net_r": cell["net_r"],
            "avg_r": cell["net_r"] / cell["n"],
            "win_rate": cell["wins"] / cell["n"],
        })
    return rows


def _collinearity(metrics: dict) -> list[dict[str, Any]]:
    """Dimensions whose segment sizes match exactly, flagged as *candidates*.

    Equal counts are evidence of an identical partition, not proof of one — two
    dimensions can coincidentally split a population the same way. The finding is
    reported as `identical_counts`, and the wording says candidate, because
    proving set identity would mean holding every record's key for every
    dimension and this module will not carry that to make a footnote stronger.
    """
    dimensions = ((metrics.get("segmentation") or {}).get("dimensions") or {})
    signature: dict[tuple, list[str]] = {}
    for name, entry in dimensions.items():
        segments = entry.get("segments") or {}
        counts = tuple(sorted(
            (seg.get("counts") or {}).get("scored_trades", 0)
            for seg in segments.values()
            if (seg.get("counts") or {}).get("scored_trades", 0)))
        if len(counts) < 2:
            continue
        signature.setdefault(counts, []).append(name)
    return [
        {"identical_counts": list(counts), "dimensions": sorted(names),
         "candidate_identical_partition": True}
        for counts, names in sorted(signature.items())
        if len(names) > 1
    ]


# ---------------------------------------------------------------------------
# Signal funnel
# ---------------------------------------------------------------------------

def _funnel(metrics: dict, signals: SignalPopulation) -> dict[str, Any]:
    counts = (metrics.get("global") or {}).get("counts") or {}
    stages = [
        {"stage": "analysed", "n": signals.ledger_rows,
         "source": "signal_ledger.jsonl"},
        {"stage": "actionable", "n": signals.actionable,
         "source": "signal_ledger.jsonl",
         "note": "decision in VALID / A+"},
        {"stage": "instantiated", "n": counts.get("all_records"),
         "source": "outcomes.jsonl"},
        {"stage": "evaluated", "n": counts.get("total_trades"),
         "source": "outcomes.jsonl",
         "note": "shadows and burn-in removed (C-2.2 / C-5.7.1)"},
        {"stage": "filled", "n": counts.get("filled_trades"),
         "source": "outcomes.jsonl"},
        {"stage": "scored", "n": counts.get("scored_trades"),
         "source": "outcomes.jsonl"},
    ]
    return {
        "source": "derived",
        "note": "stage counts come from two different artifacts and are NOT a "
                "strict nesting: an instantiated trade may be a shadow, and the "
                "ledger counts analyses while outcomes count instantiations.",
        "stages": stages,
        "by_decision": dict(sorted(signals.by_decision.items())),
        "by_mode_decision": dict(sorted(signals.by_mode_decision.items())),
        "suppression": {
            "source": "official",
            "note": "research tiers held out of the headline (C-2.2)",
            "shadows": {
                kind: (shadow.get("counts") or {}).get("all_records")
                for kind, shadow in sorted(
                    (metrics.get("shadows") or {}).items())},
            "shadow_expectancy": {
                kind: (shadow.get("values") or {}).get("expectancy")
                for kind, shadow in sorted(
                    (metrics.get("shadows") or {}).items())},
        },
    }


# ---------------------------------------------------------------------------
# Caveats
# ---------------------------------------------------------------------------

def _caveats(metrics: dict, manifest: Optional[dict]) -> dict[str, Any]:
    segmentation = metrics.get("segmentation") or {}
    health = metrics.get("data_health") or {}
    dataset = metrics.get("dataset") or {}
    intervals = ((metrics.get("global") or {}).get("intervals") or {})
    expectancy = intervals.get("expectancy") or {}
    low, high = expectancy.get("ci_low"), expectancy.get("ci_high")
    return {
        "source": "official",
        "multiple_comparisons": {
            "segments_total": segmentation.get("total_segments"),
            "threshold": MULTIPLE_COMPARISON_THRESHOLD,
            "caution": segmentation.get("multiple_comparison_caution"),
            "min_segment_n": MIN_SEGMENT_N,
            "note": "§12 — a segment may motivate a hypothesis, never justify a "
                    "change. With this many segments some will look significant "
                    "by chance.",
        },
        "edge_significance": {
            "expectancy": expectancy.get("point"),
            "ci_low": low, "ci_high": high,
            "straddles_zero": (None if low is None
                               else bool(low <= 0.0 <= high)),
            "note": "an interval containing zero means the run does not "
                    "demonstrate an edge, in either direction",
        },
        "data_health_clean": health.get("clean"),
        "join_rate": dataset.get("join_rate"),
        "declared_biases": (manifest or {}).get("declared_biases"),
        "in_sample": True,
        "note": "one contiguous window with no declared holdout; every figure "
                "is in-sample (§11 step 8).",
    }


# ---------------------------------------------------------------------------
# Shared readers
# ---------------------------------------------------------------------------

def _segments_of(metrics: dict, dimension: str) -> list[dict[str, Any]]:
    """One dimension's segments, copied verbatim out of `metrics.json`.

    Values are **not** recomputed, reformatted or rounded — a test compares these
    against the source object, so any transformation here would be a defect
    rather than a presentation choice.
    """
    entry = (((metrics.get("segmentation") or {}).get("dimensions") or {})
             .get(dimension))
    if not entry:
        return []
    rows: list[dict[str, Any]] = []
    for key, segment in sorted((entry.get("segments") or {}).items()):
        counts = segment.get("counts") or {}
        values = segment.get("values") or {}
        interval = (segment.get("intervals") or {}).get("expectancy") or {}
        rows.append({
            "key": key,
            "n": counts.get("scored_trades", 0),
            "win_rate": values.get("win_rate"),
            "expectancy": values.get("expectancy"),
            "avg_r": values.get("expectancy"),
            "profit_factor": values.get("profit_factor"),
            "payoff_ratio": values.get("payoff_ratio"),
            "avg_holding_candles": values.get("avg_holding_candles"),
            "ci_low": interval.get("ci_low"),
            "ci_high": interval.get("ci_high"),
            "inferential": (segment.get("flags") or {}).get("inferential"),
        })
    return rows


def _read_optional_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _single(values: Optional[list]) -> Optional[str]:
    if not values:
        return None
    return str(values[0]) if len(values) == 1 else ", ".join(str(v) for v in values)


def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _iso(epoch: int) -> str:
    return _utc(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _fmt(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


def _hours(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    return f"{seconds / H1_SECONDS:,.0f} h"


def _segment_table(rows: list[dict[str, Any]], *, limit: Optional[int] = None,
                   sort_by_expectancy: bool = False) -> str:
    if not rows:
        return "_no segments_\n\n"
    if sort_by_expectancy:
        rows = sorted(rows, key=lambda r: (r["expectancy"] is None,
                                           r["expectancy"]))
    if limit:
        rows = rows[:limit]
    out = ["| Segment | n | win | expectancy | 95% CI | PF | inferential |",
           "|---|---:|---:|---:|---|---:|---|"]
    for row in rows:
        ci = "—"
        if row.get("ci_low") is not None:
            ci = f"[{row['ci_low']:+.4f}, {row['ci_high']:+.4f}]"
        out.append(
            f"| `{row['key']}` | {row['n']} | {_fmt(row['win_rate'])} | "
            f"{_fmt(row['expectancy'])} | {ci} | "
            f"{_fmt(row['profit_factor'])} | {_fmt(row['inferential'])} |")
    return "\n".join(out) + "\n\n"


def _interval(bounds: Optional[list]) -> str:
    if not bounds or bounds[0] is None:
        return "—"
    excludes = bounds[0] > 0 or bounds[1] < 0
    return f"[{bounds[0]:+.4f}, {bounds[1]:+.4f}]{' ✓' if excludes else ''}"


def _describe_line(unit: str, summary: dict, points: dict) -> str:
    return (f"n {summary.get('n')} · mean {_fmt(summary.get('mean'))} {unit} · "
            f"σ {_fmt(summary.get('stdev'))} · min {_fmt(summary.get('min'))} · "
            f"p25 {_fmt(points.get('p25'))} · p50 {_fmt(points.get('p50'))} · "
            f"p75 {_fmt(points.get('p75'))} · p95 {_fmt(points.get('p95'))} · "
            f"max {_fmt(summary.get('max'))}\n")


def _sparkline(series: list[list[float]], width: int = 72) -> str:
    """The equity path as one line of block characters — no chart dependency."""
    if not series:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    values = [point[1] for point in series]
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    low, high = min(sampled), max(sampled)
    span = (high - low) or 1.0
    return "".join(
        blocks[min(len(blocks) - 1, int((value - low) / span * len(blocks)))]
        for value in sampled)


def _markdown(report: AnalyticsReport) -> str:
    out: list[str] = []
    write = out.append
    identity = report.identity
    equity = report.equity

    write("# Phase 1 Analytics\n\n")
    write(f"`run_id` **{identity.get('run_id')}** · "
          f"`strategy_version` {identity.get('strategy_version')} · "
          f"`metrics_hash` **{identity.get('metrics_hash')}** · "
          f"analytics engine v{ANALYTICS_ENGINE_VERSION} / metrics engine "
          f"v{identity.get('metrics_engine_version')}\n\n")
    write("> Every value is labelled `official` (copied verbatim from "
          "`metrics.json`) or `derived` (computed here from official values). "
          "Nothing the metrics engine defines is recomputed.\n\n")

    # -- identity --------------------------------------------------------
    window = identity.get("window") or {}
    write("## Identity\n\n| Field | Value |\n|---|---|\n")
    for name, value in (
        ("window", f"{window.get('T_0')} → {window.get('T_n')}"),
        ("cycles", window.get("cycles")),
        ("data_snapshot_id", identity.get("data_snapshot_id")),
        ("universe_id", identity.get("universe_id")),
        ("outcome_config_hash", identity.get("outcome_config_hash")),
        ("burn_in_h1", identity.get("burn_in_h1")),
        ("universe_mode", identity.get("universe_mode")),
        ("equity_convention", identity.get("equity_convention")),
        ("gross of costs", (identity.get("costs") or {}).get("gross_of_costs")),
        ("generated_at", identity.get("generated_at")),
    ):
        write(f"| {name} | {_fmt(value)} |\n")

    # -- populations -----------------------------------------------------
    populations = report.populations
    write("\n## Populations\n\n")
    write("**Trades** (`official`) — ")
    write(", ".join(f"`{k}` {v}" for k, v in
                    (populations.get("trades") or {}).items() if v is not None))
    write("\n\n**Signals** (`official`, ledger) — ")
    signals = populations.get("signals") or {}
    write(f"`ledger_rows` {signals.get('ledger_rows')}, ")
    write(", ".join(f"`{k}` {v}" for k, v in
                    (signals.get("by_decision") or {}).items()))
    write("\n\n")

    # -- equity ----------------------------------------------------------
    official = equity.get("official") or {}
    write("## A · Equity\n\n| Field | Value |\n|---|---:|\n")
    for name, value in (
        ("final R / total R", official.get("final_r")),
        ("max drawdown (R)", official.get("max_drawdown_r")),
        ("max run-up (R)", official.get("max_runup_r")),
        ("max consecutive wins", official.get("max_consecutive_wins")),
        ("max consecutive losses", official.get("max_consecutive_losses")),
        ("trades on the curve", official.get("trades")),
    ):
        write(f"| {name} | {_fmt(value)} |\n")
    write(f"\n`{_sparkline((equity.get('curve') or {}).get('series') or [])}`\n\n")

    episodes = equity.get("drawdown_episodes") or {}
    recovery = equity.get("recovery") or {}
    write(f"### Drawdown episodes — `derived`\n\n")
    write(f"{episodes.get('episodes_total')} episodes, "
          f"{episodes.get('unrecovered')} unrecovered · deepest "
          f"{_fmt(episodes.get('deepest_r'), 2)} R · time in drawdown "
          f"{_fmt(recovery.get('time_in_drawdown_share'))} · longest "
          f"{_hours(recovery.get('longest_drawdown_seconds'))} · median "
          f"recovery {_hours(recovery.get('median_recovery_seconds'))}\n\n")
    listed = episodes.get("episodes") or []
    if listed:
        write("| # | depth R | start | trough | end | duration | recovered | trades |\n")
        write("|---:|---:|---|---|---|---:|---|---:|\n")
        for index, episode in enumerate(listed, 1):
            write(f"| {index} | {_fmt(episode['depth_r'], 2)} | "
                  f"{episode['start_iso'][:10]} | {episode['trough_iso'][:10]} | "
                  f"{(episode['end_iso'] or '—')[:10]} | "
                  f"{_hours(episode['duration_seconds'])} | "
                  f"{_fmt(episode['recovered'])} | {episode['trades']} |\n")
        write("\n")

    # -- calendar --------------------------------------------------------
    calendar = report.calendar
    consistency = calendar.get("consistency") or {}
    write("## B · Calendar\n\n")
    write(f"**Consistency** (`derived`, exit_T basis) — "
          f"{consistency.get('months_positive')}/"
          f"{consistency.get('months_total')} months positive "
          f"({_fmt(consistency.get('positive_share'))}) · mean "
          f"{_fmt(consistency.get('monthly_net_r_mean'), 2)} R · σ "
          f"{_fmt(consistency.get('monthly_net_r_stdev'), 2)} R · "
          f"signal/noise {_fmt(consistency.get('signal_to_noise'))} · "
          f"longest +run {consistency.get('longest_positive_run')} · "
          f"longest −run {consistency.get('longest_negative_run')}\n\n")
    best, worst = consistency.get("best") or {}, consistency.get("worst") or {}
    write(f"best **{best.get('key')}** {_fmt(best.get('net_r'), 1)} R · "
          f"worst **{worst.get('key')}** {_fmt(worst.get('net_r'), 1)} R\n\n")

    write("### Yearly — `official` (`year`, instantiation basis)\n\n")
    write(_segment_table((calendar.get("yearly") or {}).get("segments") or []))
    write("### Yearly net R — `derived` (exit_T basis, sums to final R)\n\n")
    write("| Year | n | net R | avg R | win |\n|---|---:|---:|---:|---:|\n")
    for row in (calendar.get("equity_calendar") or {}).get("yearly") or []:
        write(f"| {row['key']} | {row['n']} | {_fmt(row['net_r'], 1)} | "
              f"{_fmt(row['avg_r'])} | {_fmt(row['win_rate'])} |\n")
    write("\n### Monthly net R — `derived` (exit_T basis)\n\n")
    write("| Month | n | net R | avg R | win |\n|---|---:|---:|---:|---:|\n")
    for row in (calendar.get("equity_calendar") or {}).get("monthly") or []:
        write(f"| {row['key']} | {row['n']} | {_fmt(row['net_r'], 1)} | "
              f"{_fmt(row['avg_r'])} | {_fmt(row['win_rate'])} |\n")
    write("\n### Quarterly — `official`\n\n")
    write(_segment_table((calendar.get("quarterly") or {}).get("segments") or []))

    # -- sessions --------------------------------------------------------
    sessions = report.sessions
    write("## C · Sessions\n\n")
    definition = (sessions.get("by_session") or {}).get("definition") or {}
    write("Session edges (`reporting convention`): "
          + ", ".join(f"`{k}` {v}" for k, v in definition.items()) + "\n\n")
    write("### By session — `official`\n\n")
    write(_segment_table((sessions.get("by_session") or {}).get("segments") or []))
    write("### SNIPER gate — `official` (strategy configuration)\n\n")
    write(_segment_table((sessions.get("sniper_gate") or {}).get("segments") or []))
    write("### By UTC hour — `official`\n\n")
    write(_segment_table((sessions.get("by_hour") or {}).get("segments") or []))
    write("### By weekday — `official`\n\n")
    write(_segment_table((sessions.get("by_weekday") or {}).get("segments") or []))

    # -- strategy --------------------------------------------------------
    strategy = report.strategy
    write("## D · Strategy breakdown\n\n### Setup — `official`\n\n")
    write(_segment_table((strategy.get("by_setup") or {}).get("segments") or []))
    absent = (strategy.get("by_setup") or {}).get("modes_absent") or {}
    for mode, detail in sorted(absent.items()):
        write(f"> `{mode}` — **n = 0**. {detail['why']} "
              f"(ledger rows: {detail['ledger_rows']}).\n")
    if absent:
        write("\n")
    write("### Direction — `official`\n\n")
    write(_segment_table((strategy.get("by_direction") or {}).get("segments") or []))
    write("### Setup × direction — `derived`\n\n")
    write("| Setup | Direction | n | net R | avg R | win |\n")
    write("|---|---|---:|---:|---:|---:|\n")
    for cell in (strategy.get("by_setup_direction") or {}).get("cells") or []:
        write(f"| {cell['entry_mode']} | {cell['direction']} | {cell['n']} | "
              f"{_fmt(cell['net_r'], 1)} | {_fmt(cell['avg_r'])} | "
              f"{_fmt(cell['win_rate'])} |\n")
    write("\n### Alert tier — `official`\n\n")
    write(_segment_table((strategy.get("by_tier") or {}).get("segments") or []))
    write("### Score band — `official`\n\n")
    write(_segment_table((strategy.get("by_score_band") or {}).get("segments") or []))
    write("### Score distribution across all analysed rows — `derived`\n\n")
    counts = (strategy.get("score_distribution") or {}).get("counts") or {}
    write(", ".join(f"`{k}` {v}" for k, v in counts.items()) + "\n\n")

    collinear = strategy.get("collinear_dimensions") or []
    if collinear:
        write("### Collinearity candidates — `derived`\n\n")
        write("Dimensions whose segment sizes match exactly. Equal counts are "
              "evidence of one partition presented four ways, not proof.\n\n")
        for entry in collinear:
            write(f"- {', '.join(f'`{n}`' for n in entry['dimensions'])} "
                  f"— sizes {entry['identical_counts']}\n")
        write("\n")

    # -- outcomes --------------------------------------------------------
    outcomes = report.outcomes
    write("## Outcome breakdown\n\n")
    write("| Outcome | source dimension | n | expectancy | net R |\n")
    write("|---|---|---:|---:|---:|\n")
    for row in outcomes.get("grouped") or []:
        write(f"| {row['outcome']} | `{row['dimension']}` | {row['n']} | "
              f"{_fmt(row['expectancy'])} | {_fmt(row['net_r'], 1)} |\n")
    for name, why in (outcomes.get("unavailable") or {}).items():
        write(f"\n> `{name}` — **not derivable from the outcome artifacts.** "
              f"{why}\n")
    write("\n### Raw exit reasons — `official`\n\n")
    write(_segment_table(outcomes.get("by_exit_reason") or []))
    write("### Cancel reasons — `official`\n\n")
    write(_segment_table(outcomes.get("by_cancel_reason") or []))
    rejection = outcomes.get("rejection_reasons") or {}
    write("### Rejection reasons — `derived` (ledger, counts only)\n\n")
    write("| Reason | rows |\n|---|---:|\n")
    for reason, count in sorted((rejection.get("counts") or {}).items(),
                                key=lambda item: -item[1]):
        write(f"| `{reason}` | {count} |\n")
    write("\n")

    # -- distributions ---------------------------------------------------
    distributions = report.distributions
    r_block = distributions.get("r_multiple") or {}
    write("## R-multiple distribution — `derived`\n\n")
    write("| Band | n | share | sum R |\n|---|---:|---:|---:|\n")
    for band in r_block.get("bands") or []:
        if not band["n"]:
            continue
        write(f"| `{band['band']}` | {band['n']} | {_fmt(band['share'])} | "
              f"{_fmt(band['sum_r'], 1)} |\n")
    write("\n" + _describe_line("R", r_block.get("summary") or {},
                                r_block.get("percentiles") or {}))

    duration = distributions.get("duration") or {}
    write("\n## Trade duration — `derived` (H1 candles = hours)\n\n")
    write("| Band | n | share | sum R |\n|---|---:|---:|---:|\n")
    for band in duration.get("bands") or []:
        if not band["n"]:
            continue
        write(f"| `{band['band']}` | {band['n']} | {_fmt(band['share'])} | "
              f"{_fmt(band['sum_r'], 1)} |\n")
    holding = distributions.get("holding") or {}
    write("\n| Population | n | mean | p50 | p90 | p99 | max |\n")
    write("|---|---:|---:|---:|---:|---:|---:|\n")
    for name in ("all", "wins", "losses"):
        block = holding.get(name) or {}
        write(f"| {name} | {_fmt(block.get('n'))} | "
              f"{_fmt(block.get('mean'), 2)} | {_fmt(block.get('p50'), 1)} | "
              f"{_fmt(block.get('p90'), 1)} | {_fmt(block.get('p99'), 1)} | "
              f"{_fmt(block.get('max'), 1)} |\n")

    streak_block = distributions.get("streaks") or {}
    write("\n## Streaks — `derived`\n\n| Side | runs | longest | mean | "
          "by length |\n|---|---:|---:|---:|---|\n")
    for side in ("wins", "losses"):
        block = streak_block.get(side) or {}
        lengths = ", ".join(f"`{k}` {v}" for k, v in
                            (block.get("by_length") or {}).items())
        write(f"| {side} | {block.get('runs')} | {block.get('longest')} | "
              f"{_fmt(block.get('mean_length'), 2)} | {lengths} |\n")
    write("\n")

    # -- rolling ---------------------------------------------------------
    rolling = report.rolling
    write("## Rolling statistics — `derived`\n\n")
    write(f"> {rolling.get('sharpe_definition')}\n\n")
    write("| Window | points | expectancy min | max | windows positive |\n")
    write("|---:|---:|---:|---:|---:|\n")
    for key, block in sorted((rolling.get("windows") or {}).items(),
                             key=lambda item: int(item[0])):
        if not block.get("points"):
            write(f"| {key} | 0 | — | — | — |\n")
            continue
        low, high = block["expectancy_range"]
        write(f"| {key} | {block['points']} | {_fmt(low)} | {_fmt(high)} | "
              f"{_fmt(block['share_of_windows_positive'])} |\n")
    write("\n")

    # -- symbols ---------------------------------------------------------
    symbols = report.symbols
    board = symbols.get("leaderboard") or {}
    write(f"## Symbol leaderboard — `derived` (minimum "
          f"{board.get('minimum_n')} trades to qualify: "
          f"{board.get('symbols_qualified')} of {board.get('symbols_total')})"
          f"\n\n")
    write(f"> {board.get('caution')}\n\n")
    for title, key in (("Best by total R", "best_by_total_r"),
                       ("Worst by total R", "worst_by_total_r"),
                       ("Best by expectancy (qualified)",
                        "best_by_expectancy_qualified"),
                       ("Worst by expectancy (qualified)",
                        "worst_by_expectancy_qualified")):
        write(f"**{title}** — ")
        write(", ".join(
            f"`{row['symbol']}` {row['total_r']:+.1f}R (n={row['n']})"
            for row in board.get(key) or []))
        write("\n\n")

    contribution = symbols.get("contributors") or {}
    write(f"**Concentration** — {contribution.get('positive_symbols')} symbols "
          f"positive ({_fmt(contribution.get('gross_positive_r'), 1)} R), "
          f"{contribution.get('negative_symbols')} negative "
          f"({_fmt(contribution.get('gross_negative_r'), 1)} R)\n\n")

    # -- hypotheses ------------------------------------------------------
    research = report.research
    method = research.get("method") or {}
    write("## Hypothesis tests — `derived`\n\n")
    write(f"> {method.get('family_size')} questions, declared in code before "
          f"any data is read. Correction: {method.get('correction')}. "
          f"Significance rule: {method.get('significance_rule')}.\n\n")
    write("| Hypothesis | left n | right n | effect (R) | Cohen d | "
          "Welch CI | bootstrap CI | p adj | significant |\n")
    write("|---|---:|---:|---:|---:|---|---|---:|---|\n")
    for result in research.get("results") or []:
        block = result["expectancy"]
        welch = _interval(block["welch_ci"])
        boot = _interval(block["bootstrap_ci"])
        write(f"| `{result['name']}` | {block['left']['n']} | "
              f"{block['right']['n']} | {_fmt(block['effect'])} | "
              f"{_fmt(block['cohens_d'])} | {welch} | {boot} | "
              f"{_fmt(block['p_adjusted'])} | "
              f"{'**yes**' if block['significant'] else 'no'} |\n")
    write("\n")
    for result in research.get("results") or []:
        write(f"**{result['question']}**\n\n")
        write(f"- {result['expectancy']['interpretation']}\n")
        write(f"- win rate: {result['win_rate']['interpretation']}\n\n")

    # -- funnel ----------------------------------------------------------
    funnel = report.signal_funnel
    write("## Signal funnel — `derived`\n\n")
    write("| Stage | n | source |\n|---|---:|---|\n")
    for stage in funnel.get("stages") or []:
        write(f"| {stage['stage']} | {_fmt(stage['n'])} | `{stage['source']}` |\n")
    write("\n**By decision** — ")
    write(", ".join(f"`{k}` {v}" for k, v in
                    (funnel.get("by_decision") or {}).items()))
    suppression = funnel.get("suppression") or {}
    write("\n\n**Suppressed research tiers** (`official`) — ")
    write(", ".join(
        f"`{k}` n={v} E={_fmt((suppression.get('shadow_expectancy') or {}).get(k))}"
        for k, v in (suppression.get("shadows") or {}).items()))
    write("\n\n")

    # -- caveats ---------------------------------------------------------
    caveats = report.caveats
    edge = caveats.get("edge_significance") or {}
    multiple = caveats.get("multiple_comparisons") or {}
    write("## Caveats\n\n")
    write(f"- **Edge significance** — expectancy {_fmt(edge.get('expectancy'))}, "
          f"95% CI [{_fmt(edge.get('ci_low'))}, {_fmt(edge.get('ci_high'))}]"
          f"{' — **straddles zero**' if edge.get('straddles_zero') else ''}. "
          f"{edge.get('note')}\n")
    write(f"- **Multiple comparisons** — {multiple.get('segments_total')} "
          f"segments against a caution threshold of {multiple.get('threshold')}"
          f"; `min_segment_n` {multiple.get('min_segment_n')}. "
          f"{multiple.get('note')}\n")
    write(f"- **In-sample** — {caveats.get('note')}\n")
    write(f"- **Data health clean** — "
          f"{_fmt(caveats.get('data_health_clean'))} · join rate "
          f"{_fmt(caveats.get('join_rate'))}\n")
    for bias in caveats.get("declared_biases") or []:
        write(f"- **Declared bias (C-5.5)** — {bias}\n")
    return "".join(out)
