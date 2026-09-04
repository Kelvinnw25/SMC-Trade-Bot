"""Lifecycle Duration — does persistence carry information after exposure time?

Opt-in and unregistered: absent from `studies.STUDIES`, absent from the CLI.

## The question

`lifecycle_analysis` observed that fill rate rises with `increment_miss_count`
(0.576 -> 0.869 -> 0.910 -> 0.914) and with `partial_refresh_count`. This
module asks whether that survives conditioning on how long the episode was
actually at risk, and reports which of two descriptions the data is more
consistent with:

    H1  PERSISTENCE_SIGNAL       persistence carries information of its own
    H2  EXPOSURE_TIME_PLAUSIBLE  persistence only proxies time at risk

Neither is tested. There is no significance machinery here and no causal
adjustment; the verdict is a **descriptive label** produced by a rule frozen
in this module before any number was computed (`specification_hash`).

## Why exposure time is the obvious rival explanation

A pending limit order that stays alive longer has more candles in which its
entry can be touched. So "longer episode -> higher fill rate" is close to
mechanical, and any counter that grows with episode length inherits that
relationship without carrying information of its own.

`increment_miss_count` is exactly such a counter. It is H1-gated
([reconciler.py:575](v2/lifecycle/reconciler.py#L575)): at most one increment
per closed H1 candle. So it is bounded by episode age in hours, and the
age x miss table is **structurally triangular** — an episode shorter than one
H1 candle cannot have accrued a miss at all.

## Two structural facts this module reports rather than works around

**Two declared age bins are empty.** Maximum observed episode duration is 39
hours; `3-7d` and `>7d` contain zero episodes. This is a configuration
ceiling, not a data gap (`untapped_ttl_seconds` 24h, `tapped_ttl_seconds` 6h,
`miss_count_limit` 3). Both bins are **kept in the specification** and emitted
with `structurally_empty: true`. Re-cutting the bins after seeing the data
would be exactly the data-driven binning this study prohibits, so the empty
rows stay and carry their reason with them.

**The `<1h` bin cannot be matched.** It holds only `miss_count == 0`, by the
H1 gate above, and fills at roughly 0.1%. It is reported in full in every
descriptive age table, and flagged `non_comparable` and excluded from the
matched comparison, because it has no non-zero miss group to be compared
against. Its arithmetic contribution to the unconditional gradient is
reported explicitly rather than being quietly removed.

## What is never done here

No p-value, no significance claim, no ranking, no comparison verdict between
strategies, no recommendation, no strategy change. No cross-mode pooling: a
fill rate is never computed over CONTINUATION and RETRACEMENT rows together,
and the overall verdict is assembled by tallying the four per-cell verdicts,
never by pooling the four cells' rows.

## Source of truth

`episode_reconstruction.json` (the written artifact) plus the exact
`signal_id` outcome join that module already proved 1:1. The lifecycle event
stream is not re-parsed and the episode reconstruction is not rebuilt.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional, Sequence

from backtest.analytics.studies.episode_reconstruction import (
    DIRNAME as EPISODE_RECONSTRUCTION_DIRNAME,
    END_REASONS,
    Episode,
    specification_hash as parent_specification_hash,
)
from backtest.analytics.studies.performance_census import CELLS, _gross, _mean

__all__ = [
    "AGE_BINS",
    "AGE_BIN_NAMES",
    "CELLS",
    "DIRNAME",
    "END_REASON_ORDER",
    "EVIDENCE_CLASS",
    "MIN_STRATUM_N",
    "MISS_LEVELS",
    "NON_COMPARABLE_AGE_BINS",
    "PROHIBITED_WORDS",
    "REFRESH_LEVELS",
    "SCOPE_STATEMENT",
    "TIER_BUCKETS",
    "VERDICTS",
    "Book",
    "Row",
    "analyse",
    "csv_rows",
    "load",
    "render",
    "specification_hash",
    "write_lifecycle_duration",
]

DIRNAME = "lifecycle_duration"
ENGINE_VERSION = 1

EVIDENCE_CLASS = "DESCRIPTIVE"

SCOPE_STATEMENT = (
    "Descriptive duration-conditioned lifecycle accounting only. Reports "
    "whether the persistence/fill relationship survives conditioning on "
    "episode age. No statistical inference, no causal adjustment, no "
    "ranking, no strategy conclusion."
)

#: Pre-declared age bins, in hours, half-open `[lo, hi)`. `None` is unbounded.
#: Kept exactly as declared: the last two are structurally empty under this
#: bundle's lifecycle configuration, and are emitted with `n = 0` rather than
#: removed, because re-cutting bins after seeing the data would be the
#: data-driven binning this study prohibits.
AGE_BINS: tuple = (
    ("<1h", 0.0, 1.0),
    ("1-4h", 1.0, 4.0),
    ("4-12h", 4.0, 12.0),
    ("12-24h", 12.0, 24.0),
    ("1-3d", 24.0, 72.0),
    ("3-7d", 72.0, 168.0),
    (">7d", 168.0, None),
)

AGE_BIN_NAMES: tuple = tuple(name for name, _lo, _hi in AGE_BINS)

#: Age bins that cannot host a matched comparison. `<1h` holds only
#: `miss_count == 0` because the miss counter is H1-gated, so it has no
#: non-zero group to compare against. Excluded from matching, never from the
#: descriptive tables.
NON_COMPARABLE_AGE_BINS: frozenset = frozenset({"<1h"})

#: `miss_count_limit = 3` caps the counter, so these four levels are exhaustive.
MISS_LEVELS: tuple = ("0", "1", "2", "3")

#: The refresh counter is unbounded, so the last level absorbs the tail.
REFRESH_LEVELS: tuple = ("0", "1", "2+")

END_REASON_ORDER: tuple = (
    "DECAY", "REPLACED", "SUPERSEDED", "TTL", "KILL", "STILL_RESIDENT_AT_END",
)

TIER_BUCKETS: tuple = ("WATCH", "VALID", "A+", "OTHER")

#: A matched stratum below this many episodes in **either** compared group is
#: reported but never contributes to the verdict. Reuses the project's frozen
#: side floor (`performance_census.MIN_SIDE_N`) rather than inventing one.
MIN_STRATUM_N = 120

#: Fewer comparable strata than this and the verdict is `INDETERMINATE`.
MIN_COMPARABLE_STRATA = 2

#: Verdict thresholds on the median retained fraction — the share of the
#: unconditional fill-rate gradient that survives inside age strata. Frozen
#: before computation; there is no runtime knob for either.
RETAINED_HIGH = 0.60
RETAINED_LOW = 0.25

VERDICTS: tuple = (
    "PERSISTENCE_SIGNAL", "EXPOSURE_TIME_PLAUSIBLE", "MIXED", "INDETERMINATE",
)

PROHIBITED_WORDS: tuple = (
    "recommend", "best", "worst", "outperform", "edge", "significant",
    "p-value", "p value", "superior", "causal effect",
)

GOVERNANCE_NOTES: tuple = (
    "This module reports a descriptive label, not a test. No inferential "
    "statistic, no significance claim, and no causal adjustment is computed "
    "anywhere.",
    "increment_miss_count is H1-gated, so it is bounded above by episode age "
    "in hours; the age x miss table is structurally triangular and this is a "
    "property of the lifecycle engine, not of the market.",
    "The <1h age bin holds only miss_count == 0 and is flagged "
    "non_comparable: it is reported in full but excluded from matched "
    "comparison because it has no non-zero group to match against.",
    "The 3-7d and >7d age bins are structurally empty under this bundle's "
    "TTL configuration; they are retained as declared rather than re-cut, "
    "because re-cutting after seeing the data would be data-driven binning.",
    "STILL_RESIDENT_AT_END episodes are right-censored: their duration is "
    "measured to the last observed boundary, not to a real ending.",
    "Cross-mode comparison is prohibited. No fill rate pools CONTINUATION "
    "with RETRACEMENT rows, and the overall verdict tallies the four "
    "per-cell verdicts rather than pooling their episodes.",
)


class LifecycleDurationError(Exception):
    """The study cannot be assembled, or a frozen rule was violated."""


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------

def _age_bin(duration_hours: float) -> Optional[str]:
    for name, low, high in AGE_BINS:
        if duration_hours >= low and (high is None or duration_hours < high):
            return name
    return None


def _miss_level(count: int) -> str:
    index = min(int(count), len(MISS_LEVELS) - 1)
    return MISS_LEVELS[index]


def _refresh_level(count: int) -> str:
    index = min(int(count), len(REFRESH_LEVELS) - 1)
    return REFRESH_LEVELS[index]


def _tier_bucket(tier: str) -> str:
    return tier if tier in TIER_BUCKETS else "OTHER"


def _cell_of(direction: str, entry_mode: str) -> Optional[str]:
    for name, mode, cell_direction in CELLS:
        if entry_mode == mode and direction == cell_direction:
            return name
    return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One episode, its duration, and its fill-basis outcome facts."""

    episode: Episode
    filled: bool
    realized_r: Optional[float]
    fill_T: Optional[int]

    @property
    def duration_hours(self) -> float:
        return (self.episode.end_boundary - self.episode.start_boundary) / 3600.0

    @property
    def age_bin(self) -> Optional[str]:
        return _age_bin(self.duration_hours)

    @property
    def cell(self) -> Optional[str]:
        return _cell_of(self.episode.direction, self.episode.entry_mode)

    @property
    def miss_level(self) -> str:
        return _miss_level(self.episode.increment_miss_count)

    @property
    def refresh_level(self) -> str:
        return _refresh_level(self.episode.partial_refresh_count)

    @property
    def tier_bucket(self) -> str:
        return _tier_bucket(self.episode.tier_at_start)

    @property
    def hours_to_fill(self) -> Optional[float]:
        """Start boundary to fill, in hours. `None` when never filled."""
        if self.fill_T is None:
            return None
        return (self.fill_T - self.episode.start_boundary) / 3600.0


@dataclass(frozen=True)
class Book:
    rows: tuple
    run_id: str
    source: str
    strategy_version: Optional[str]
    outcome_config_hash: Optional[str]
    data_snapshot_id: Optional[str]
    episode_reconstruction_specification_hash: str
    metrics_hash: Optional[str]
    upstream_reconciliation: dict


def _read_metrics_hash(run_dir: Path) -> Optional[str]:
    path = run_dir / "metrics.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("metrics_hash")
    return str(value) if value is not None else None


def load(run_dir: Path | str) -> Book:
    """Assemble the joined episode population from a finished bundle."""
    from backtest.dataset import OUTCOMES_FILENAME, read_outcomes

    run_dir = Path(run_dir)
    artifact_path = (run_dir / "analytics" / "research"
                     / EPISODE_RECONSTRUCTION_DIRNAME
                     / "episode_reconstruction.json")
    if not artifact_path.exists():
        raise LifecycleDurationError(
            f"no episode reconstruction artifact at {artifact_path}. This "
            f"module reads that written artifact as its source of episode "
            f"identity and does not rebuild it.")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    outcomes: dict = {}
    for record in read_outcomes(run_dir / OUTCOMES_FILENAME):
        if not record.signal_id:
            continue
        if record.signal_id in outcomes:
            raise LifecycleDurationError(
                f"outcomes.jsonl carries duplicate signal_id "
                f"{record.signal_id!r}; the join requires 1:1 identity")
        outcomes[record.signal_id] = (
            bool(record.filled), record.realized_r, record.fill_T)

    rows = []
    for raw in artifact.get("episodes", []):
        episode = Episode(**raw)
        filled, realized_r, fill_T = outcomes.get(
            episode.signal_id, (False, None, None))
        rows.append(Row(episode=episode, filled=filled, realized_r=realized_r,
                        fill_T=fill_T))

    return Book(
        rows=tuple(rows),
        run_id=artifact.get("run_id", ""),
        source=artifact.get("source", ""),
        strategy_version=artifact.get("strategy_version"),
        outcome_config_hash=artifact.get("outcome_config_hash"),
        data_snapshot_id=artifact.get("data_snapshot_id"),
        episode_reconstruction_specification_hash=artifact.get(
            "reconstruction_specification_hash", ""),
        metrics_hash=_read_metrics_hash(run_dir),
        upstream_reconciliation=dict(artifact.get("reconciliation", {})),
    )


# ---------------------------------------------------------------------------
# Shared group summary
# ---------------------------------------------------------------------------

def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def _group_summary(subset: Sequence[Row], *, with_end_reasons: bool = True,
                   ) -> dict[str, Any]:
    """Every figure this module reports about one group of episodes.

    `fill_rate` and `unfilled_n` always accompany the R figures, so an
    unfilled episode stays visible instead of vanishing from a denominator.
    """
    n = len(subset)
    filled = [row for row in subset if row.filled]
    scored_values = [row.realized_r for row in subset
                     if row.realized_r is not None]
    win, loss = _gross(scored_values)
    durations = [row.duration_hours for row in subset]

    record: dict[str, Any] = {
        "n": n,
        "filled_n": len(filled),
        "unfilled_n": n - len(filled),
        "scored_n": len(scored_values),
        "fill_rate": (len(filled) / n) if n else None,
        "total_r": float(sum(scored_values)) if scored_values else None,
        "mean_r": _mean(scored_values),
        "median_r": median(scored_values) if scored_values else None,
        "gross_positive_r": win,
        "gross_negative_r": loss,
        "median_duration_hours": median(durations) if durations else None,
        "p25_duration_hours": _quantile(durations, 0.25),
        "p75_duration_hours": _quantile(durations, 0.75),
        "mean_increment_miss_count": _mean(
            [row.episode.increment_miss_count for row in subset]),
        "mean_partial_refresh_count": _mean(
            [row.episode.partial_refresh_count for row in subset]),
    }
    if with_end_reasons:
        record["end_reason_composition"] = {
            reason: sum(1 for row in subset
                        if row.episode.end_reason == reason)
            for reason in END_REASON_ORDER}
    return record


# ---------------------------------------------------------------------------
# Section 2 — age distribution
# ---------------------------------------------------------------------------

def _age_distribution(rows: Sequence[Row]) -> dict[str, Any]:
    total = len(rows)
    groups: dict = {name: [] for name in AGE_BIN_NAMES}
    unassigned = 0
    for row in rows:
        name = row.age_bin
        if name is None:
            unassigned += 1
            continue
        groups[name].append(row)

    table = []
    for name, low, high in AGE_BINS:
        subset = groups[name]
        record = _group_summary(subset)
        record.update({
            "age_bin": name,
            "lower_hours": low,
            "upper_hours": high,
            "share_of_episodes": (len(subset) / total) if total else None,
            "structurally_empty": len(subset) == 0,
            "non_comparable": name in NON_COMPARABLE_AGE_BINS,
        })
        table.append(record)

    durations = [row.duration_hours for row in rows]
    censored = sum(1 for row in rows
                   if row.episode.end_reason == "STILL_RESIDENT_AT_END")
    return {
        "rows": table,
        "unassigned": unassigned,
        "observed_min_hours": min(durations) if durations else None,
        "observed_max_hours": max(durations) if durations else None,
        "right_censored_n": censored,
        "empty_bins": [name for name in AGE_BIN_NAMES
                       if not groups[name]],
    }


# ---------------------------------------------------------------------------
# Sections 3-4 — age x miss and age x refresh
# ---------------------------------------------------------------------------

def _two_way(rows: Sequence[Row], *, levels: Sequence[str],
             level_of) -> list:
    """The fixed `age_bin x level` table, one record per occupied or declared
    combination. Every combination is emitted, including empty ones."""
    groups: dict = {(age, level): []
                    for age in AGE_BIN_NAMES for level in levels}
    for row in rows:
        age = row.age_bin
        if age is None:
            continue
        groups[(age, level_of(row))].append(row)

    out = []
    for age in AGE_BIN_NAMES:
        for level in levels:
            subset = groups[(age, level)]
            record = _group_summary(subset)
            record.update({
                "age_bin": age,
                "level": level,
                "non_comparable_age_bin": age in NON_COMPARABLE_AGE_BINS,
            })
            out.append(record)
    return out


def _marginal(rows: Sequence[Row], *, levels: Sequence[str],
              level_of) -> list:
    groups: dict = {level: [] for level in levels}
    for row in rows:
        groups[level_of(row)].append(row)
    out = []
    for level in levels:
        record = _group_summary(groups[level])
        record["level"] = level
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Section 5 — DECAY-specific
# ---------------------------------------------------------------------------

def _decay_analysis(rows: Sequence[Row]) -> dict[str, Any]:
    """DECAY is the dominant natural exit, so it gets its own per-cell view."""
    out: dict[str, Any] = {}
    for name, mode, direction in CELLS:
        subset = [row for row in rows
                  if row.episode.entry_mode == mode
                  and row.episode.direction == direction
                  and row.episode.end_reason == "DECAY"]
        record = _group_summary(subset, with_end_reasons=False)
        record["cell"] = name
        out[name] = record
    return out


# ---------------------------------------------------------------------------
# Section 6 — outcome timing, per cell
# ---------------------------------------------------------------------------

def _outcome_timing(rows: Sequence[Row]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, mode, direction in CELLS:
        subset = [row for row in rows
                  if row.episode.entry_mode == mode
                  and row.episode.direction == direction
                  and row.filled]
        hours = [row.hours_to_fill for row in subset
                 if row.hours_to_fill is not None]
        scored_values = [row.realized_r for row in subset
                         if row.realized_r is not None]
        out[name] = {
            "cell": name,
            "filled_n": len(subset),
            "hours_to_fill_known_n": len(hours),
            "median_hours_to_fill": median(hours) if hours else None,
            "p25_hours_to_fill": _quantile(hours, 0.25),
            "p75_hours_to_fill": _quantile(hours, 0.75),
            "median_episode_duration_hours": (
                median([row.duration_hours for row in subset])
                if subset else None),
            "mean_increment_miss_count_at_end": _mean(
                [row.episode.increment_miss_count for row in subset]),
            "mean_partial_refresh_count_at_end": _mean(
                [row.episode.partial_refresh_count for row in subset]),
            "scored_n": len(scored_values),
            "total_r": float(sum(scored_values)) if scored_values else None,
            "mean_r": _mean(scored_values),
            "median_r": median(scored_values) if scored_values else None,
        }
    return out


# ---------------------------------------------------------------------------
# Section 7 — matched descriptive comparison
# ---------------------------------------------------------------------------

def _matched_strata(rows: Sequence[Row], *, levels: Sequence[str],
                    level_of) -> list:
    """Fixed matching: parent cell x age bin x start-tier bucket.

    Nothing is optimised and no propensity model is fitted. The strata are the
    declared cross-product, and a stratum is simply reported as under-floor
    when either compared group is below `MIN_STRATUM_N`.
    """
    low_level, high_level = levels[0], levels[-1]
    groups: dict = {}
    for row in rows:
        cell, age = row.cell, row.age_bin
        if cell is None or age is None:
            continue
        key = (cell, age, row.tier_bucket)
        groups.setdefault(key, {level: [] for level in levels})
        groups[key][level_of(row)].append(row)

    out = []
    for (cell, age, tier), buckets in sorted(groups.items()):
        low, high = buckets[low_level], buckets[high_level]
        low_rate = (sum(1 for row in low if row.filled) / len(low)
                    if low else None)
        high_rate = (sum(1 for row in high if row.filled) / len(high)
                     if high else None)
        comparable = (
            age not in NON_COMPARABLE_AGE_BINS
            and len(low) >= MIN_STRATUM_N
            and len(high) >= MIN_STRATUM_N)
        out.append({
            "cell": cell,
            "age_bin": age,
            "tier_at_start": tier,
            "low_level": low_level,
            "high_level": high_level,
            "low_n": len(low),
            "high_n": len(high),
            "low_fill_rate": low_rate,
            "high_fill_rate": high_rate,
            "fill_rate_delta": (
                None if low_rate is None or high_rate is None
                else high_rate - low_rate),
            "comparable": comparable,
            "excluded_reason": (
                "non_comparable_age_bin"
                if age in NON_COMPARABLE_AGE_BINS
                else ("below_min_stratum_n"
                      if not comparable else None)),
        })
    return out


def _cell_marginal_delta(rows: Sequence[Row], cell: str, *,
                         levels: Sequence[str], level_of,
                         ) -> Optional[float]:
    """The unconditional fill-rate gradient **inside one cell**. Never pools
    across cells — that would be the prohibited cross-mode comparison."""
    subset = [row for row in rows if row.cell == cell]
    low = [row for row in subset if level_of(row) == levels[0]]
    high = [row for row in subset if level_of(row) == levels[-1]]
    if not low or not high:
        return None
    return (sum(1 for row in high if row.filled) / len(high)
            - sum(1 for row in low if row.filled) / len(low))


def _verdict_for(strata: Sequence[dict], marginal_delta: Optional[float],
                 ) -> dict[str, Any]:
    """The frozen verdict rule. Deterministic, thresholds pre-declared."""
    comparable = [s for s in strata if s["comparable"]
                  and s["fill_rate_delta"] is not None]
    retained = []
    preserved = reversed_count = 0
    if marginal_delta not in (None, 0.0):
        for stratum in comparable:
            fraction = stratum["fill_rate_delta"] / marginal_delta
            retained.append(fraction)
            if fraction > 0:
                preserved += 1
            elif fraction < 0:
                reversed_count += 1

    median_retained = median(retained) if retained else None

    if len(comparable) < MIN_COMPARABLE_STRATA or median_retained is None:
        verdict = "INDETERMINATE"
    elif preserved > 0 and reversed_count > 0:
        verdict = "MIXED"
    elif median_retained >= RETAINED_HIGH:
        verdict = "PERSISTENCE_SIGNAL"
    elif median_retained <= RETAINED_LOW:
        verdict = "EXPOSURE_TIME_PLAUSIBLE"
    else:
        verdict = "MIXED"

    return {
        "verdict": verdict,
        "marginal_fill_rate_delta": marginal_delta,
        "comparable_strata": len(comparable),
        "strata_sign_preserved": preserved,
        "strata_sign_reversed": reversed_count,
        "median_retained_fraction": median_retained,
        # `EXPOSURE_TIME_PLAUSIBLE` covers two distinguishable situations:
        # the gradient attenuating toward zero, and the gradient *inverting*
        # inside strata. Both mean the unconditional association did not
        # survive conditioning, so both map to that verdict of the four
        # declared — but a reader should be able to tell them apart, so the
        # distinction is carried as data rather than collapsed.
        "sign_reversed_dominant": bool(
            reversed_count > 0 and preserved == 0),
        "retained_high_threshold": RETAINED_HIGH,
        "retained_low_threshold": RETAINED_LOW,
        "min_stratum_n": MIN_STRATUM_N,
        "min_comparable_strata": MIN_COMPARABLE_STRATA,
    }


def _matched_analysis(rows: Sequence[Row], *, levels: Sequence[str],
                      level_of) -> dict[str, Any]:
    strata = _matched_strata(rows, levels=levels, level_of=level_of)
    per_cell: dict[str, Any] = {}
    for name, _mode, _direction in CELLS:
        cell_strata = [s for s in strata if s["cell"] == name]
        marginal = _cell_marginal_delta(rows, name, levels=levels,
                                        level_of=level_of)
        per_cell[name] = {
            "strata": cell_strata,
            **_verdict_for(cell_strata, marginal),
        }

    # The overall verdict tallies the four per-cell verdicts. It never pools
    # their episodes, which would be a cross-mode fill rate.
    tally: dict[str, int] = {verdict: 0 for verdict in VERDICTS}
    for block in per_cell.values():
        tally[block["verdict"]] += 1
    decided = [v for v in VERDICTS if v != "INDETERMINATE" and tally[v] > 0]
    if not decided:
        overall = "INDETERMINATE"
    elif len(decided) == 1:
        overall = decided[0]
    else:
        overall = "MIXED"

    return {"per_cell": per_cell, "cell_verdict_tally": tally,
            "overall_verdict": overall}


# ---------------------------------------------------------------------------
# Section 4b — survivor composition
# ---------------------------------------------------------------------------

def _survivor_composition(rows: Sequence[Row]) -> list:
    groups: dict = {level: [] for level in MISS_LEVELS}
    for row in rows:
        groups[row.miss_level].append(row)
    out = []
    for level in MISS_LEVELS:
        record = _group_summary(groups[level])
        record["miss_level"] = level
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# Per-cell conditional fill rates (Section 2 requirement)
# ---------------------------------------------------------------------------

def _cell_conditional(rows: Sequence[Row]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, mode, direction in CELLS:
        subset = [row for row in rows if row.episode.entry_mode == mode
                  and row.episode.direction == direction]
        out[name] = {
            "n": len(subset),
            "by_miss": _marginal(subset, levels=MISS_LEVELS,
                                 level_of=lambda r: r.miss_level),
            "by_refresh": _marginal(subset, levels=REFRESH_LEVELS,
                                    level_of=lambda r: r.refresh_level),
            "by_age": [
                {**_group_summary([row for row in subset
                                   if row.age_bin == age]), "age_bin": age}
                for age in AGE_BIN_NAMES],
            "age_by_miss": _two_way(subset, levels=MISS_LEVELS,
                                    level_of=lambda r: r.miss_level),
            "age_by_refresh": _two_way(subset, levels=REFRESH_LEVELS,
                                       level_of=lambda r: r.refresh_level),
        }
    return out


# ---------------------------------------------------------------------------
# Specification hash
# ---------------------------------------------------------------------------

def _specification_payload() -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "age_bins": [[name, low, high] for name, low, high in AGE_BINS],
        "non_comparable_age_bins": sorted(NON_COMPARABLE_AGE_BINS),
        "miss_levels": list(MISS_LEVELS),
        "refresh_levels": list(REFRESH_LEVELS),
        "end_reason_order": list(END_REASON_ORDER),
        "tier_buckets": list(TIER_BUCKETS),
        "cells": [name for name, _mode, _direction in CELLS],
        "min_stratum_n": MIN_STRATUM_N,
        "min_comparable_strata": MIN_COMPARABLE_STRATA,
        "retained_high": RETAINED_HIGH,
        "retained_low": RETAINED_LOW,
        "verdicts": list(VERDICTS),
        "prohibited_words": list(PROHIBITED_WORDS),
        "parent_specification_hash": parent_specification_hash(),
        "parent_end_reasons": list(END_REASONS),
    }


def specification_hash() -> str:
    canonical = json.dumps(_specification_payload(), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(run_dir: Path | str) -> dict[str, Any]:
    book = load(run_dir)
    rows = book.rows

    return {
        "run_id": book.run_id,
        "source": book.source,
        "metrics_hash": book.metrics_hash,
        "outcome_config_hash": book.outcome_config_hash,
        "data_snapshot_id": book.data_snapshot_id,
        "strategy_version": book.strategy_version,
        "episode_reconstruction_specification_hash":
            book.episode_reconstruction_specification_hash,
        "lifecycle_duration_specification_hash": specification_hash(),
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "governance_notes": list(GOVERNANCE_NOTES),
        "population": {
            "episodes": len(rows),
            "filled": sum(1 for row in rows if row.filled),
            "scored": sum(1 for row in rows if row.realized_r is not None),
            "upstream_reconciliation": dict(book.upstream_reconciliation),
        },
        "age_distribution": _age_distribution(rows),
        "marginal_by_miss": _marginal(rows, levels=MISS_LEVELS,
                                      level_of=lambda r: r.miss_level),
        "marginal_by_refresh": _marginal(rows, levels=REFRESH_LEVELS,
                                         level_of=lambda r: r.refresh_level),
        "age_by_miss": _two_way(rows, levels=MISS_LEVELS,
                                level_of=lambda r: r.miss_level),
        "age_by_refresh": _two_way(rows, levels=REFRESH_LEVELS,
                                   level_of=lambda r: r.refresh_level),
        "survivor_composition": _survivor_composition(rows),
        "decay_analysis": _decay_analysis(rows),
        "outcome_timing": _outcome_timing(rows),
        "parent_cells": _cell_conditional(rows),
        "matched_miss": _matched_analysis(rows, levels=MISS_LEVELS,
                                          level_of=lambda r: r.miss_level),
        "matched_refresh": _matched_analysis(
            rows, levels=REFRESH_LEVELS,
            level_of=lambda r: r.refresh_level),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


def _level_table(records: Sequence[dict], key: str) -> list:
    lines = [
        f"| {key} | n | filled | unfilled | fill_rate | total_r | mean_r | "
        f"median_r | median_dur_h |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            f"| {record.get(key)} | {record['n']} | {record['filled_n']} | "
            f"{record['unfilled_n']} | {_num(record['fill_rate'])} | "
            f"{_num(record['total_r'])} | {_num(record['mean_r'])} | "
            f"{_num(record['median_r'])} | "
            f"{_num(record['median_duration_hours'])} |")
    return lines


def _two_way_table(records: Sequence[dict], levels: Sequence[str]) -> list:
    lines = ["| age_bin | " + " | ".join(f"{lvl} n / fill" for lvl in levels)
             + " |",
             "|---|" + "---|" * len(levels)]
    by_age: dict = {}
    for record in records:
        by_age.setdefault(record["age_bin"], {})[record["level"]] = record
    for age in AGE_BIN_NAMES:
        cells = []
        for level in levels:
            record = by_age.get(age, {}).get(level)
            if record is None or record["n"] == 0:
                cells.append("0 / n/a")
            else:
                cells.append(f"{record['n']} / {_num(record['fill_rate'])}")
        marker = " *" if age in NON_COMPARABLE_AGE_BINS else ""
        lines.append(f"| {age}{marker} | " + " | ".join(cells) + " |")
    return lines


def _matched_block(block: dict, label: str) -> list:
    lines = [f"### {label}", ""]
    lines.append(f"- overall verdict: **{block['overall_verdict']}**")
    lines.append(f"- per-cell verdict tally: {block['cell_verdict_tally']}")
    lines.append("")
    lines.append(
        "| cell | verdict | marginal delta | comparable strata | "
        "sign kept | sign reversed | median retained | reversal dominant |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cell, cell_block in block["per_cell"].items():
        lines.append(
            f"| {cell} | {cell_block['verdict']} | "
            f"{_num(cell_block['marginal_fill_rate_delta'])} | "
            f"{cell_block['comparable_strata']} | "
            f"{cell_block['strata_sign_preserved']} | "
            f"{cell_block['strata_sign_reversed']} | "
            f"{_num(cell_block['median_retained_fraction'])} | "
            f"{cell_block['sign_reversed_dominant']} |")
    lines.append("")
    lines.append(
        "Comparable strata only (parent cell x age bin x start tier, both "
        f"groups at or above n={MIN_STRATUM_N}):")
    lines.append("")
    lines.append(
        "| cell | age_bin | tier | low n | high n | low fill | high fill | "
        "delta |")
    lines.append("|---|---|---|---|---|---|---|---|")
    any_row = False
    for cell_block in block["per_cell"].values():
        for stratum in cell_block["strata"]:
            if not stratum["comparable"]:
                continue
            any_row = True
            lines.append(
                f"| {stratum['cell']} | {stratum['age_bin']} | "
                f"{stratum['tier_at_start']} | {stratum['low_n']} | "
                f"{stratum['high_n']} | {_num(stratum['low_fill_rate'])} | "
                f"{_num(stratum['high_fill_rate'])} | "
                f"{_num(stratum['fill_rate_delta'])} |")
    if not any_row:
        lines.append("| — | — | — | — | — | — | — | — |")
    lines.append("")
    return lines


def render(payload: dict[str, Any]) -> str:
    lines: list = []

    # 1 — scope + governance
    lines.append("# Lifecycle Duration — persistence vs exposure time")
    lines.append("")
    lines.append(f"- run_id: `{payload.get('run_id', '')}`")
    lines.append(f"- source: `{payload.get('source', '')}`")
    lines.append(f"- strategy_version: `{payload.get('strategy_version')}`")
    lines.append(f"- metrics_hash: `{payload.get('metrics_hash')}`")
    lines.append(f"- outcome_config_hash: `{payload.get('outcome_config_hash')}`")
    lines.append(f"- data_snapshot_id: `{payload.get('data_snapshot_id')}`")
    lines.append(
        f"- episode_reconstruction_specification_hash: "
        f"`{payload.get('episode_reconstruction_specification_hash')}`")
    lines.append(
        f"- lifecycle_duration_specification_hash: "
        f"`{payload.get('lifecycle_duration_specification_hash')}`")
    lines.append(f"- evidence_class: `{payload.get('evidence_class')}`")
    lines.append(f"- generated_at: `{payload.get('generated_at', '')}`")
    lines.append("")
    lines.append(f"> {payload.get('scope', '')}")
    lines.append("")
    for note in payload.get("governance_notes", []):
        lines.append(f"- {note}")
    lines.append("")

    population = payload.get("population", {})
    lines.append(
        f"Population: {population.get('episodes')} episodes, "
        f"{population.get('filled')} filled, {population.get('scored')} scored.")
    lines.append("")

    # 2 — age distribution
    lines.append("## 2. Episode age distribution")
    lines.append("")
    age = payload.get("age_distribution", {})
    lines.append(
        f"Observed duration range: {_num(age.get('observed_min_hours'))}h to "
        f"{_num(age.get('observed_max_hours'))}h. "
        f"Right-censored (still resident at end of data): "
        f"{age.get('right_censored_n')}. "
        f"Structurally empty declared bins: {age.get('empty_bins')}.")
    lines.append("")
    lines.append(
        "| age_bin | n | share | filled | unfilled | fill_rate | mean_miss | "
        "mean_refresh | empty | non_comparable |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for record in age.get("rows", []):
        lines.append(
            f"| {record['age_bin']} | {record['n']} | "
            f"{_num(record['share_of_episodes'])} | {record['filled_n']} | "
            f"{record['unfilled_n']} | {_num(record['fill_rate'])} | "
            f"{_num(record['mean_increment_miss_count'])} | "
            f"{_num(record['mean_partial_refresh_count'])} | "
            f"{record['structurally_empty']} | {record['non_comparable']} |")
    lines.append("")

    # 3 — miss x age
    lines.append("## 3. IncrementMiss x age")
    lines.append("")
    lines.append("Unconditional (marginal) by miss level:")
    lines.append("")
    lines.extend(_level_table(payload.get("marginal_by_miss", []), "level"))
    lines.append("")
    lines.append("Conditioned on age bin (n / fill rate). `*` = non-comparable:")
    lines.append("")
    lines.extend(_two_way_table(payload.get("age_by_miss", []), MISS_LEVELS))
    lines.append("")
    lines.append("Survivor composition by miss level:")
    lines.append("")
    lines.append(
        "| miss | n | median_dur_h | p25_dur_h | p75_dur_h | mean_refresh | "
        "fill_rate |")
    lines.append("|---|---|---|---|---|---|---|")
    for record in payload.get("survivor_composition", []):
        lines.append(
            f"| {record['miss_level']} | {record['n']} | "
            f"{_num(record['median_duration_hours'])} | "
            f"{_num(record['p25_duration_hours'])} | "
            f"{_num(record['p75_duration_hours'])} | "
            f"{_num(record['mean_partial_refresh_count'])} | "
            f"{_num(record['fill_rate'])} |")
    lines.append("")

    # 4 — refresh x age
    lines.append("## 4. PartialRefresh x age")
    lines.append("")
    lines.append("Unconditional (marginal) by refresh level:")
    lines.append("")
    lines.extend(_level_table(payload.get("marginal_by_refresh", []), "level"))
    lines.append("")
    lines.append("Conditioned on age bin (n / fill rate). `*` = non-comparable:")
    lines.append("")
    lines.extend(_two_way_table(payload.get("age_by_refresh", []),
                                REFRESH_LEVELS))
    lines.append("")

    # 5 — DECAY
    lines.append("## 5. DECAY analysis")
    lines.append("")
    lines.append(
        "| cell | n | fill_rate | median_dur_h | p25_dur_h | p75_dur_h | "
        "mean_miss | mean_refresh | mean_r |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for cell, record in payload.get("decay_analysis", {}).items():
        lines.append(
            f"| {cell} | {record['n']} | {_num(record['fill_rate'])} | "
            f"{_num(record['median_duration_hours'])} | "
            f"{_num(record['p25_duration_hours'])} | "
            f"{_num(record['p75_duration_hours'])} | "
            f"{_num(record['mean_increment_miss_count'])} | "
            f"{_num(record['mean_partial_refresh_count'])} | "
            f"{_num(record['mean_r'])} |")
    lines.append("")

    # 6 — outcome timing
    lines.append("## 6. Outcome-duration analysis (filled episodes only)")
    lines.append("")
    lines.append(
        "| cell | filled n | median_h_to_fill | p25 | p75 | median_dur_h | "
        "mean_miss | mean_refresh | mean_r | median_r |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cell, record in payload.get("outcome_timing", {}).items():
        lines.append(
            f"| {cell} | {record['filled_n']} | "
            f"{_num(record['median_hours_to_fill'])} | "
            f"{_num(record['p25_hours_to_fill'])} | "
            f"{_num(record['p75_hours_to_fill'])} | "
            f"{_num(record['median_episode_duration_hours'])} | "
            f"{_num(record['mean_increment_miss_count_at_end'])} | "
            f"{_num(record['mean_partial_refresh_count_at_end'])} | "
            f"{_num(record['mean_r'])} | {_num(record['median_r'])} |")
    lines.append("")

    # 7 — matched comparison
    lines.append("## 7. Matched descriptive comparison")
    lines.append("")
    lines.append(
        "Matching is the fixed cross-product parent cell x age bin x "
        "start-tier bucket. Nothing is optimised and no propensity model is "
        "fitted. The retained fraction is the within-stratum fill-rate "
        "difference divided by the same cell's unconditional difference.")
    lines.append("")
    lines.extend(_matched_block(payload.get("matched_miss", {}),
                                "IncrementMiss (level 0 vs 3)"))
    lines.extend(_matched_block(payload.get("matched_refresh", {}),
                                "PartialRefresh (level 0 vs 2+)"))

    # 8 — interpretation
    lines.append("## 8. Interpretation")
    lines.append("")
    miss_verdict = payload.get("matched_miss", {}).get("overall_verdict")
    refresh_verdict = payload.get("matched_refresh", {}).get("overall_verdict")
    lines.append(f"- IncrementMiss: **{miss_verdict}**")
    lines.append(f"- PartialRefresh: **{refresh_verdict}**")
    lines.append("")
    lines.append(
        "These labels are produced by the frozen rule above, applied to "
        "within-stratum fill-rate differences. They describe which of the "
        "two candidate explanations the observed pattern is more consistent "
        "with. They are not a test, they carry no confidence statement, and "
        "they do not establish that any counter causes any outcome.")
    lines.append("")

    # 9 — limitations
    lines.append("## 9. Limitations")
    lines.append("")
    lines.append(
        "- Longer episodes mechanically expose a pending entry to more "
        "candles, so a positive duration/fill relationship is close to "
        "tautological and is not evidence about the strategy.")
    lines.append(
        "- increment_miss_count is bounded above by episode age in hours "
        "(H1 gate) and capped at 3 by configuration, so it is partially "
        "collinear with duration by construction; the two cannot be fully "
        "separated with the fields available here.")
    lines.append(
        "- The `<1h` bin admits only miss level 0, so no matched comparison "
        "exists there; it is reported but never contributes to a verdict.")
    lines.append(
        "- Age bins do not equalise exposure uniformly. In `1-4h` the miss-0 "
        "and miss-3 groups still differ in mean duration, so any residual "
        "difference there remains confounded; in `4-12h` their mean durations "
        "nearly coincide, which is the only stratum where exposure is close "
        "to matched. Per-bin duration quantiles are emitted so a reader can "
        "check this rather than take the bin label on trust.")
    lines.append(
        "- Even at matched duration, miss level is not a pure persistence "
        "measure: it records how often the strategy declined to act on that "
        "symbol, so a miss-0 episode alive for hours was being refreshed or "
        "re-qualified each cycle while a miss-3 episode was not. The two "
        "groups therefore differ in what the strategy was seeing, not only "
        "in how long they survived, and this study cannot separate those.")
    lines.append(
        "- Two declared age bins hold zero episodes under this bundle's TTL "
        "configuration, so the age axis is effectively four populated bins "
        "plus one sparse one.")
    lines.append(
        "- Single in-sample bundle. Nothing here is out-of-sample, and "
        "burn-in episodes are included throughout.")
    lines.append(
        "- Episode duration for STILL_RESIDENT_AT_END rows is right-censored "
        "at the last observed boundary.")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

_CSV_HEADER = (
    "table", "cell", "age_bin", "level", "n", "filled_n", "unfilled_n",
    "scored_n", "fill_rate", "total_r", "mean_r", "median_r",
    "median_duration_hours", "mean_increment_miss_count",
    "mean_partial_refresh_count",
)


def csv_rows(payload: dict[str, Any]) -> list:
    rows: list = [list(_CSV_HEADER)]

    def emit(table: str, record: dict[str, Any], *, cell: str = "",
             age_bin: str = "", level: str = "") -> None:
        rows.append([
            table, cell, age_bin, level,
            record.get("n", ""), record.get("filled_n", ""),
            record.get("unfilled_n", ""), record.get("scored_n", ""),
            record.get("fill_rate", ""), record.get("total_r", ""),
            record.get("mean_r", ""), record.get("median_r", ""),
            record.get("median_duration_hours", ""),
            record.get("mean_increment_miss_count", ""),
            record.get("mean_partial_refresh_count", ""),
        ])

    for record in payload.get("age_distribution", {}).get("rows", []):
        emit("age_distribution", record, age_bin=record["age_bin"])
    for record in payload.get("marginal_by_miss", []):
        emit("marginal_by_miss", record, level=record["level"])
    for record in payload.get("marginal_by_refresh", []):
        emit("marginal_by_refresh", record, level=record["level"])
    for record in payload.get("age_by_miss", []):
        emit("age_by_miss", record, age_bin=record["age_bin"],
             level=record["level"])
    for record in payload.get("age_by_refresh", []):
        emit("age_by_refresh", record, age_bin=record["age_bin"],
             level=record["level"])
    for record in payload.get("survivor_composition", []):
        emit("survivor_composition", record, level=record["miss_level"])
    for cell, record in payload.get("decay_analysis", {}).items():
        emit("decay_analysis", record, cell=cell)
    for cell, record in payload.get("outcome_timing", {}).items():
        emit("outcome_timing", record, cell=cell)
    return rows


def _csv_escape(value: Any) -> str:
    text = str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_lifecycle_duration(payload: dict[str, Any],
                             out_dir: Path | str) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    written: dict[str, Path] = {}
    json_path = out / "lifecycle_duration.json"
    json_path.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written["lifecycle_duration.json"] = json_path

    md_path = out / "lifecycle_duration.md"
    md_path.write_text(render(body), encoding="utf-8")
    written["lifecycle_duration.md"] = md_path

    csv_path = out / "lifecycle_duration.csv"
    csv_path.write_text(
        "\n".join(",".join(_csv_escape(cell) for cell in row)
                  for row in csv_rows(payload)) + "\n", encoding="utf-8")
    written["lifecycle_duration.csv"] = csv_path
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Explicit entry point. Deliberately not a CLI verb."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="lifecycle_duration",
        description="Duration-conditioned descriptive lifecycle analysis.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    payload = analyse(run_dir)
    out_dir = (Path(args.out) if args.out
               else run_dir / "analytics" / "research" / DIRNAME)
    written = write_lifecycle_duration(payload, out_dir)
    for name, path in sorted(written.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
