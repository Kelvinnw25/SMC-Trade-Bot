"""Lifecycle Analysis — does persistence (IncrementMiss, PartialRefresh)
relate to what happens to an episode?

Opt-in and unregistered: absent from `studies.STUDIES`, absent from the CLI.

## What this is

The first analytical layer over `episode_reconstruction`'s output. It groups
the 76,576 reconstructed episodes of the official bundle by two lifecycle
counters — `increment_miss_count` and `partial_refresh_count` — into fixed
bins, and reports, per bin: population size, fill/score rate, end-reason
composition, and realized-R accounting (fill basis only). It also reports a
small fixed miss×refresh joint table, a fixed end-reason accounting table,
the same bin tables recomputed within each of the four parent cells, and one
flat session rollup.

## What this is not

Not a search for an edge. Every grouping key is frozen in this module before
any number was computed (`specification_hash`); nothing here is chosen after
looking at the data. `IncrementMiss` and `PartialRefresh` are *lifecycle
observations* — they describe what the memory engine did to a thesis, not a
hypothesized cause of its outcome. A relationship shown here, if any, is a
lifecycle correlation: nothing in this module tests it, ranks it, or turns it
into a recommendation. Evidence class is `DESCRIPTIVE`, unconditionally.

## Population and exclusions

The population is every episode `episode_reconstruction` reconstructed —
every `InsertMutation`/`ReplaceMutation` that ever occupied a memory slot,
alerted or not. Two lifecycle outcomes never reach this population, and both
are exclusions by construction rather than by a filter this module applies:

- `STALE` is a pre-memory freshness check on the *fresh* candidate
  (reconciler.py:264-279); it never touches `resident` and produces zero
  mutations, so it never becomes an episode.
- `CAPPED` (and `KILL_COOLDOWN`) return before any mutation is appended
  (reconciler.py:507-536); they never enter memory, so they too never become
  an episode. Neither this module nor `episode_reconstruction` filters them
  out — they were never there.

`signal_id` is the episode identity throughout, inherited from
`episode_reconstruction`; `thesis_key` is coarser and is deliberately reused
across sequential episodes (see that module's docstring).

## Outcome basis

Realized-R accounting uses the **fill basis only** — an unfilled episode is
never entered as 0R (that would apply a mode-specific shrinkage; see
`performance_census`'s cross-mode prohibition, which applies here for the
identical reason). Every table that reports `mean_r`/`median_r`/`total_r`
also reports `scored_n` beside it, and `filled_n`/`fill_rate` are always
visible so an unfilled episode stays legible rather than vanishing from the
denominator.

## Cross-mode prohibition

No table in this module ever merges CONTINUATION with RETRACEMENT, or LONG
with SHORT. Every parent-cell view is one of the four frozen cells; nothing
here computes or renders a merged total across cells, screened by
`PROHIBITED_GROUPING_LABELS` and a rendered-text word screen.

## Reuse

`episode_reconstruction.json` (the written artifact, not a fresh reconstruct)
is the source of episode identity and lifecycle counters — re-parsing
`lifecycle_events.jsonl` is neither performed nor needed. `outcomes.jsonl` is
read once, directly, for `filled`/`realized_r` (the join `signal_id` is
already proven 1:1 by `episode_reconstruction`). `CELLS` comes from
`performance_census`; `SESSIONS` from `backtest.metrics.segments`. Nothing
here recomputes what any of those three already define.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional, Sequence

from backtest.analytics.studies.episode_reconstruction import (
    DIRNAME as EPISODE_RECONSTRUCTION_DIRNAME,
    Episode,
    specification_hash as parent_specification_hash,
)
from backtest.analytics.studies.performance_census import CELLS, _gross, _mean
from backtest.metrics.segments import SESSIONS, _utc

__all__ = [
    "CELLS",
    "DIRNAME",
    "END_REASON_ORDER",
    "EVIDENCE_CLASS",
    "MISS_BIN_LABELS",
    "PROHIBITED_WORDS",
    "REFRESH_BIN_LABELS",
    "SCOPE_STATEMENT",
    "SESSION_NAMES",
    "TIER_BUCKETS",
    "Book",
    "Episode",
    "LifecycleAnalysisError",
    "Row",
    "analyse",
    "csv_rows",
    "load",
    "render",
    "specification_hash",
    "write_lifecycle_analysis",
]

DIRNAME = "lifecycle_analysis"
ENGINE_VERSION = 1

#: The only evidence class this module can emit.
EVIDENCE_CLASS = "DESCRIPTIVE"

SCOPE_STATEMENT = (
    "Descriptive lifecycle accounting only: whether IncrementMiss and "
    "PartialRefresh relate to what happens to an episode. No statistical "
    "inference, ranking, comparison verdict, or strategy conclusion."
)

GOVERNANCE_NOTES: tuple = (
    "IncrementMiss and PartialRefresh counters describe observed lifecycle "
    "behaviour; they are not modelled here as a cause of episode outcome.",
    "STALE is excluded because it is a pre-memory data-freshness guard on "
    "the fresh candidate and produces no lifecycle mutation.",
    "CAPPED, and KILL_COOLDOWN alongside it, never enter memory and "
    "therefore have no episode; both are absent from this population by "
    "construction, not by a filter this module applies.",
    "signal_id is the episode identity; thesis_key is coarser and is "
    "reused across sequential episodes.",
    "Cross-mode comparison (CONTINUATION vs RETRACEMENT, LONG vs SHORT) is "
    "prohibited; only within-cell descriptive comparison is shown.",
)

#: Fixed bins for the primary IncrementMiss distribution (Section A). The
#: last label absorbs every count at or above it.
MISS_BIN_LABELS: tuple = ("0", "1", "2", "3", "4+")

#: Fixed bins for the primary PartialRefresh distribution (Section B).
REFRESH_BIN_LABELS: tuple = ("0", "1", "2", "3+")

#: Coarser bins for the miss x refresh joint table only (Section B), kept
#: deliberately small and fixed per the frozen spec.
MISS_BIN_LABELS_JOINT: tuple = ("0", "1", "2", "3+")
REFRESH_BIN_LABELS_JOINT: tuple = ("0", "1", "2+")

#: Fixed end-reason accounting order (Section C).
END_REASON_ORDER: tuple = (
    "DECAY", "REPLACED", "SUPERSEDED", "TTL", "KILL", "STILL_RESIDENT_AT_END",
)

#: The only tiers an inserted episode can start or end at (reconciler.py's
#: `_ACTIONABLE`). `OTHER` is a fixed catch-all, never a discovered category.
TIER_BUCKETS: tuple = ("WATCH", "VALID", "A+", "OTHER")

#: Clock order, taken from `SESSIONS` itself.
SESSION_NAMES: tuple = tuple(name for name, _start, _end in SESSIONS)

#: Screened against every rendered table key. A grouping key outside the
#: frozen cells/sessions/bins would be a cross-mode or ad hoc intersection.
PROHIBITED_GROUPING_LABELS = frozenset({
    "all", "total", "overall", "combined", "merged", "aggregate",
})

#: Screened against the rendered report text (not source code — see
#: `performance_census` for why source-scanning produces false positives).
PROHIBITED_WORDS: tuple = (
    "recommend", "best", "worst", "outperform", "edge", "significant",
    "p-value", "p value",
)


class LifecycleAnalysisError(Exception):
    """The lifecycle analysis cannot be assembled, or a frozen rule broke."""


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------

def _bin_of(count: int, labels: Sequence[str]) -> str:
    index = min(int(count), len(labels) - 1)
    return labels[index]


def _miss_bin(count: int) -> str:
    return _bin_of(count, MISS_BIN_LABELS)


def _refresh_bin(count: int) -> str:
    return _bin_of(count, REFRESH_BIN_LABELS)


def _miss_bin_joint(count: int) -> str:
    return _bin_of(count, MISS_BIN_LABELS_JOINT)


def _refresh_bin_joint(count: int) -> str:
    return _bin_of(count, REFRESH_BIN_LABELS_JOINT)


def _session_of(boundary: int) -> Optional[str]:
    """Mirrors `backtest.metrics.segments._session` exactly, over a raw
    boundary rather than an `OutcomeRecord` (an episode has no such record)."""
    hour = _utc(int(boundary)).hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return None


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
    """One episode, plus its fill-basis outcome facts."""

    episode: Episode
    filled: bool
    realized_r: Optional[float]

    @property
    def cell(self) -> Optional[str]:
        return _cell_of(self.episode.direction, self.episode.entry_mode)

    @property
    def session(self) -> Optional[str]:
        return _session_of(self.episode.start_boundary)

    @property
    def duration_hours(self) -> float:
        return (self.episode.end_boundary - self.episode.start_boundary) / 3600.0


@dataclass(frozen=True)
class Book:
    """The joined episode population plus the identity needed to cite it."""

    rows: tuple
    run_id: str
    source: str
    strategy_version: Optional[str]
    outcome_config_hash: Optional[str]
    data_snapshot_id: Optional[str]
    episode_reconstruction_specification_hash: str
    metrics_hash: Optional[str]
    upstream_reconciliation: dict


def _episode_artifact_path(run_dir: Path) -> Path:
    return (run_dir / "analytics" / "research" / EPISODE_RECONSTRUCTION_DIRNAME
            / "episode_reconstruction.json")


def _read_episode_artifact(run_dir: Path) -> dict:
    path = _episode_artifact_path(run_dir)
    if not path.exists():
        raise LifecycleAnalysisError(
            f"no episode reconstruction artifact at {path}. This module "
            f"reads that written artifact as the source of episode "
            f"identity; run `episode_reconstruction.main` first rather than "
            f"re-parsing lifecycle_events.jsonl here.")
    return json.loads(path.read_text(encoding="utf-8"))


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


def _outcome_lookup(run_dir: Path) -> dict:
    from backtest.dataset import OUTCOMES_FILENAME, read_outcomes

    lookup: dict = {}
    for record in read_outcomes(run_dir / OUTCOMES_FILENAME):
        if not record.signal_id:
            continue
        if record.signal_id in lookup:
            raise LifecycleAnalysisError(
                f"outcomes.jsonl carries duplicate signal_id "
                f"{record.signal_id!r}; the outcome join requires 1:1 "
                f"signal_id identity")
        lookup[record.signal_id] = (bool(record.filled), record.realized_r)
    return lookup


def load(run_dir: Path | str) -> Book:
    """Assemble the joined episode population from a finished bundle."""
    run_dir = Path(run_dir)
    artifact = _read_episode_artifact(run_dir)
    outcomes = _outcome_lookup(run_dir)

    rows = []
    for raw in artifact.get("episodes", []):
        episode = Episode(**raw)
        filled, realized_r = outcomes.get(episode.signal_id, (False, None))
        rows.append(Row(episode=episode, filled=filled, realized_r=realized_r))

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
# Section 2 — population accounting
# ---------------------------------------------------------------------------

def _population_accounting(rows: Sequence[Row], upstream_reconciliation: dict,
                           ) -> dict[str, Any]:
    total = len(rows)
    filled_n = sum(1 for row in rows if row.filled)
    scored_n = sum(1 for row in rows if row.realized_r is not None)
    joined_n = sum(1 for row in rows if row.episode.outcome_joined)
    return {
        "episodes": total,
        "filled": filled_n,
        "scored": scored_n,
        "fill_rate": (filled_n / total) if total else None,
        "outcome_joined": joined_n,
        "outcome_without_join": total - joined_n,
        "upstream_reconciliation": dict(upstream_reconciliation),
    }


# ---------------------------------------------------------------------------
# Sections 3-4 — primary bin tables (reused per-cell in Section 7)
# ---------------------------------------------------------------------------

def _bin_record(bin_label: str, subset: Sequence[Row], total: int,
                ) -> dict[str, Any]:
    n = len(subset)
    scored_values = [row.realized_r for row in subset
                     if row.realized_r is not None]
    filled_n = sum(1 for row in subset if row.filled)
    win, loss = _gross(scored_values)

    end_reason_counts = {reason: 0 for reason in END_REASON_ORDER}
    cell_counts = {name: 0 for name, _mode, _direction in CELLS}
    session_counts = {name: 0 for name in SESSION_NAMES}
    tier_start_counts = {tier: 0 for tier in TIER_BUCKETS}
    tier_end_counts = {tier: 0 for tier in TIER_BUCKETS}
    for row in subset:
        end_reason_counts[row.episode.end_reason] = (
            end_reason_counts.get(row.episode.end_reason, 0) + 1)
        cell = row.cell
        if cell is not None:
            cell_counts[cell] += 1
        session = row.session
        if session is not None:
            session_counts[session] += 1
        start_bucket = (row.episode.tier_at_start if row.episode.tier_at_start
                        in TIER_BUCKETS else "OTHER")
        tier_start_counts[start_bucket] += 1
        end_bucket = (row.episode.tier_at_end if row.episode.tier_at_end
                     in TIER_BUCKETS else "OTHER")
        tier_end_counts[end_bucket] += 1

    return {
        "bin": bin_label,
        "n": n,
        "share_of_episodes": (n / total) if total else None,
        "filled_n": filled_n,
        "scored_n": len(scored_values),
        "fill_rate": (filled_n / n) if n else None,
        "end_reason_composition": end_reason_counts,
        "total_r": float(sum(scored_values)) if scored_values else None,
        "mean_r": _mean(scored_values),
        "median_r": median(scored_values) if scored_values else None,
        "gross_positive_r": win,
        "gross_negative_r": loss,
        "mean_increment_miss_count": _mean(
            [row.episode.increment_miss_count for row in subset]),
        "mean_partial_refresh_count": _mean(
            [row.episode.partial_refresh_count for row in subset]),
        "median_duration_hours": (
            median([row.duration_hours for row in subset]) if subset else None),
        "parent_cell_composition": cell_counts,
        "session_composition": session_counts,
        "tier_at_start": tier_start_counts,
        "tier_at_end": tier_end_counts,
    }


def _miss_distribution(rows: Sequence[Row]) -> list:
    total = len(rows)
    groups: dict = {label: [] for label in MISS_BIN_LABELS}
    for row in rows:
        groups[_miss_bin(row.episode.increment_miss_count)].append(row)
    return [_bin_record(label, groups[label], total) for label in MISS_BIN_LABELS]


def _refresh_distribution(rows: Sequence[Row]) -> list:
    total = len(rows)
    groups: dict = {label: [] for label in REFRESH_BIN_LABELS}
    for row in rows:
        groups[_refresh_bin(row.episode.partial_refresh_count)].append(row)
    return [_bin_record(label, groups[label], total)
            for label in REFRESH_BIN_LABELS]


# ---------------------------------------------------------------------------
# Section 5 — miss x refresh joint table
# ---------------------------------------------------------------------------

def _joint_table(rows: Sequence[Row]) -> list:
    total = len(rows)
    groups: dict = {(miss, refresh): []
                    for miss in MISS_BIN_LABELS_JOINT
                    for refresh in REFRESH_BIN_LABELS_JOINT}
    for row in rows:
        key = (_miss_bin_joint(row.episode.increment_miss_count),
               _refresh_bin_joint(row.episode.partial_refresh_count))
        groups[key].append(row)

    out = []
    for miss_label in MISS_BIN_LABELS_JOINT:
        for refresh_label in REFRESH_BIN_LABELS_JOINT:
            subset = groups[(miss_label, refresh_label)]
            n = len(subset)
            scored_values = [row.realized_r for row in subset
                             if row.realized_r is not None]
            filled_n = sum(1 for row in subset if row.filled)
            win, loss = _gross(scored_values)
            out.append({
                "miss_bin": miss_label,
                "refresh_bin": refresh_label,
                "n": n,
                "share_of_episodes": (n / total) if total else None,
                "filled_n": filled_n,
                "scored_n": len(scored_values),
                "fill_rate": (filled_n / n) if n else None,
                "total_r": float(sum(scored_values)) if scored_values else None,
                "mean_r": _mean(scored_values),
                "gross_positive_r": win,
                "gross_negative_r": loss,
            })
    return out


# ---------------------------------------------------------------------------
# Section 6 — end-reason accounting
# ---------------------------------------------------------------------------

def _end_reason_table(rows: Sequence[Row]) -> list:
    total = len(rows)
    groups: dict = {reason: [] for reason in END_REASON_ORDER}
    for row in rows:
        groups.setdefault(row.episode.end_reason, []).append(row)

    out = []
    for reason in END_REASON_ORDER:
        subset = groups.get(reason, [])
        n = len(subset)
        filled_n = sum(1 for row in subset if row.filled)
        scored_n = sum(1 for row in subset if row.realized_r is not None)
        durations = [row.duration_hours for row in subset]
        out.append({
            "end_reason": reason,
            "n": n,
            "share_of_episodes": (n / total) if total else None,
            "mean_increment_miss_count": _mean(
                [row.episode.increment_miss_count for row in subset]),
            "mean_partial_refresh_count": _mean(
                [row.episode.partial_refresh_count for row in subset]),
            "median_duration_hours": median(durations) if durations else None,
            "filled_n": filled_n,
            "scored_n": scored_n,
            "fill_rate": (filled_n / n) if n else None,
        })
    return out


# ---------------------------------------------------------------------------
# Section 7 — parent-cell views
# ---------------------------------------------------------------------------

def _cell_views(rows: Sequence[Row]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, mode, direction in CELLS:
        subset = [row for row in rows if row.episode.entry_mode == mode
                 and row.episode.direction == direction]
        out[name] = {
            "n": len(subset),
            "miss_distribution": _miss_distribution(subset),
            "refresh_distribution": _refresh_distribution(subset),
            "end_reason_accounting": _end_reason_table(subset),
        }
    return out


# ---------------------------------------------------------------------------
# Section 8 — session view (flat, no cross-intersection)
# ---------------------------------------------------------------------------

def _session_view(rows: Sequence[Row]) -> dict[str, Any]:
    total = len(rows)
    groups: dict = {name: [] for name in SESSION_NAMES}
    unassigned = 0
    for row in rows:
        session = row.session
        if session is None:
            unassigned += 1
            continue
        groups[session].append(row)

    table = []
    for name in SESSION_NAMES:
        subset = groups[name]
        n = len(subset)
        scored_values = [row.realized_r for row in subset
                         if row.realized_r is not None]
        filled_n = sum(1 for row in subset if row.filled)
        win, loss = _gross(scored_values)
        table.append({
            "session": name,
            "n": n,
            "share_of_episodes": (n / total) if total else None,
            "filled_n": filled_n,
            "scored_n": len(scored_values),
            "fill_rate": (filled_n / n) if n else None,
            "total_r": float(sum(scored_values)) if scored_values else None,
            "mean_r": _mean(scored_values),
            "median_r": median(scored_values) if scored_values else None,
            "gross_positive_r": win,
            "gross_negative_r": loss,
            "mean_increment_miss_count": _mean(
                [row.episode.increment_miss_count for row in subset]),
            "mean_partial_refresh_count": _mean(
                [row.episode.partial_refresh_count for row in subset]),
            "end_reason_composition": {
                reason: sum(1 for row in subset
                           if row.episode.end_reason == reason)
                for reason in END_REASON_ORDER},
        })
    return {"rows": table, "unassigned": unassigned}


# ---------------------------------------------------------------------------
# Specification hash
# ---------------------------------------------------------------------------

def _specification_payload() -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "miss_bin_labels": list(MISS_BIN_LABELS),
        "refresh_bin_labels": list(REFRESH_BIN_LABELS),
        "miss_bin_labels_joint": list(MISS_BIN_LABELS_JOINT),
        "refresh_bin_labels_joint": list(REFRESH_BIN_LABELS_JOINT),
        "end_reason_order": list(END_REASON_ORDER),
        "tier_buckets": list(TIER_BUCKETS),
        "cells": [name for name, _mode, _direction in CELLS],
        "sessions": list(SESSION_NAMES),
        "prohibited_grouping_labels": sorted(PROHIBITED_GROUPING_LABELS),
        "prohibited_words": list(PROHIBITED_WORDS),
        "parent_specification_hash": parent_specification_hash(),
    }


def specification_hash() -> str:
    """A digest over every frozen constant, bound to the upstream episode
    reconstruction specification hash so a change in either is visible."""
    canonical = json.dumps(_specification_payload(), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(run_dir: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir)
    book = load(run_dir)
    rows = book.rows

    return {
        "run_id": book.run_id,
        "source": book.source,
        "metrics_hash": book.metrics_hash,
        "outcome_config_hash": book.outcome_config_hash,
        "data_snapshot_id": book.data_snapshot_id,
        "episode_reconstruction_specification_hash":
            book.episode_reconstruction_specification_hash,
        "lifecycle_analysis_specification_hash": specification_hash(),
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "governance_notes": list(GOVERNANCE_NOTES),
        "population": _population_accounting(rows, book.upstream_reconciliation),
        "increment_miss": {"distribution": _miss_distribution(rows)},
        "partial_refresh": {"distribution": _refresh_distribution(rows)},
        "joint_miss_refresh": _joint_table(rows),
        "end_reason_accounting": _end_reason_table(rows),
        "parent_cells": _cell_views(rows),
        "sessions": _session_view(rows),
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


def _bin_table_md(rows: list, key_field: str) -> list:
    lines = [
        f"| {key_field} | n | share | filled | scored | fill_rate | total_r | "
        f"mean_r | median_r | gross+ | gross- |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in rows:
        lines.append(
            f"| {record[key_field]} | {record['n']} | "
            f"{_num(record['share_of_episodes'])} | {record['filled_n']} | "
            f"{record['scored_n']} | {_num(record['fill_rate'])} | "
            f"{_num(record['total_r'])} | {_num(record['mean_r'])} | "
            f"{_num(record['median_r'])} | {_num(record['gross_positive_r'])} | "
            f"{_num(record['gross_negative_r'])} |")
    return lines


def render(payload: dict[str, Any]) -> str:
    lines: list = []

    # 1 — scope + governance
    lines.append("# Lifecycle Analysis — IncrementMiss + PartialRefresh")
    lines.append("")
    lines.append(f"- run_id: `{payload.get('run_id', '')}`")
    lines.append(f"- source: `{payload.get('source', '')}`")
    lines.append(f"- metrics_hash: `{payload.get('metrics_hash')}`")
    lines.append(f"- outcome_config_hash: `{payload.get('outcome_config_hash')}`")
    lines.append(f"- data_snapshot_id: `{payload.get('data_snapshot_id')}`")
    lines.append(
        f"- episode_reconstruction_specification_hash: "
        f"`{payload.get('episode_reconstruction_specification_hash')}`")
    lines.append(
        f"- lifecycle_analysis_specification_hash: "
        f"`{payload.get('lifecycle_analysis_specification_hash')}`")
    lines.append(f"- evidence_class: `{payload.get('evidence_class')}`")
    lines.append(f"- generated_at: `{payload.get('generated_at', '')}`")
    lines.append("")
    lines.append(f"> {payload.get('scope', '')}")
    lines.append("")
    for note in payload.get("governance_notes", []):
        lines.append(f"- {note}")
    lines.append("")

    # 2 — population accounting
    lines.append("## 2. Episode population accounting")
    lines.append("")
    population = payload.get("population", {})
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for key in ("episodes", "filled", "scored", "fill_rate", "outcome_joined",
               "outcome_without_join"):
        lines.append(f"| {key} | {_num(population.get(key))} |")
    lines.append("")

    # 3 — IncrementMiss distribution
    lines.append("## 3. IncrementMiss distribution")
    lines.append("")
    lines.extend(_bin_table_md(
        payload.get("increment_miss", {}).get("distribution", []), "bin"))
    lines.append("")

    # 4 — PartialRefresh distribution
    lines.append("## 4. PartialRefresh distribution")
    lines.append("")
    lines.extend(_bin_table_md(
        payload.get("partial_refresh", {}).get("distribution", []), "bin"))
    lines.append("")

    # 5 — joint table
    lines.append("## 5. Miss x Refresh joint table")
    lines.append("")
    lines.append(
        "| miss_bin | refresh_bin | n | share | filled | scored | fill_rate "
        "| total_r | mean_r |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for record in payload.get("joint_miss_refresh", []):
        lines.append(
            f"| {record['miss_bin']} | {record['refresh_bin']} | "
            f"{record['n']} | {_num(record['share_of_episodes'])} | "
            f"{record['filled_n']} | {record['scored_n']} | "
            f"{_num(record['fill_rate'])} | {_num(record['total_r'])} | "
            f"{_num(record['mean_r'])} |")
    lines.append("")

    # 6 — end-reason accounting
    lines.append("## 6. End-reason accounting")
    lines.append("")
    lines.append(
        "| end_reason | n | share | mean_miss | mean_refresh | "
        "median_duration_h | filled | scored | fill_rate |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for record in payload.get("end_reason_accounting", []):
        lines.append(
            f"| {record['end_reason']} | {record['n']} | "
            f"{_num(record['share_of_episodes'])} | "
            f"{_num(record['mean_increment_miss_count'])} | "
            f"{_num(record['mean_partial_refresh_count'])} | "
            f"{_num(record['median_duration_hours'])} | {record['filled_n']} | "
            f"{record['scored_n']} | {_num(record['fill_rate'])} |")
    lines.append("")

    # 7 — parent-cell views
    lines.append("## 7. Parent-cell views")
    lines.append("")
    for name, block in payload.get("parent_cells", {}).items():
        lines.append(f"### {name} (n={block['n']})")
        lines.append("")
        lines.append("Miss distribution:")
        lines.append("")
        lines.extend(_bin_table_md(block.get("miss_distribution", []), "bin"))
        lines.append("")
        lines.append("Refresh distribution:")
        lines.append("")
        lines.extend(_bin_table_md(block.get("refresh_distribution", []), "bin"))
        lines.append("")
        lines.append("End-reason accounting:")
        lines.append("")
        lines.append("| end_reason | n | share | fill_rate |")
        lines.append("|---|---|---|---|")
        for record in block.get("end_reason_accounting", []):
            lines.append(
                f"| {record['end_reason']} | {record['n']} | "
                f"{_num(record['share_of_episodes'])} | "
                f"{_num(record['fill_rate'])} |")
        lines.append("")

    # 8 — session view
    lines.append("## 8. Session view")
    lines.append("")
    session_block = payload.get("sessions", {})
    lines.append(
        "| session | n | share | filled | scored | fill_rate | total_r | "
        "mean_r |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for record in session_block.get("rows", []):
        lines.append(
            f"| {record['session']} | {record['n']} | "
            f"{_num(record['share_of_episodes'])} | {record['filled_n']} | "
            f"{record['scored_n']} | {_num(record['fill_rate'])} | "
            f"{_num(record['total_r'])} | {_num(record['mean_r'])} |")
    lines.append(f"unassigned: {session_block.get('unassigned')}")
    lines.append("")

    # 9 — key descriptive observations (pure restatement, no new inference)
    lines.append("## 9. Key descriptive observations")
    lines.append("")
    miss_dist = payload.get("increment_miss", {}).get("distribution", [])
    refresh_dist = payload.get("partial_refresh", {}).get("distribution", [])
    end_reasons = payload.get("end_reason_accounting", [])
    total_episodes = population.get("episodes") or 0
    zero_miss = next((r for r in miss_dist if r["bin"] == "0"), None)
    zero_refresh = next((r for r in refresh_dist if r["bin"] == "0"), None)
    if zero_miss is not None and total_episodes:
        lines.append(
            f"- {zero_miss['n']} of {total_episodes} episodes "
            f"({_num(zero_miss['share_of_episodes'])}) never accumulated an "
            f"IncrementMiss.")
    if zero_refresh is not None and total_episodes:
        lines.append(
            f"- {zero_refresh['n']} of {total_episodes} episodes "
            f"({_num(zero_refresh['share_of_episodes'])}) never received a "
            f"PartialRefresh.")
    for record in end_reasons:
        if total_episodes:
            lines.append(
                f"- {record['end_reason']} accounts for "
                f"{_num(record['share_of_episodes'])} of episodes "
                f"(n={record['n']}).")
    lines.append("")

    # 10 — limitations
    lines.append("## 10. Limitations")
    lines.append("")
    lines.append(
        "- Single in-sample bundle; nothing here is out-of-sample or "
        "holdout-tested.")
    lines.append(
        "- Burn-in episodes are included in every count; this report does "
        "not exclude them the way `performance_census`'s evaluated "
        "population does.")
    lines.append(
        "- A relationship shown between a lifecycle counter and realized R "
        "is a correlation observed in one bundle, not a causal effect and "
        "not a tested one.")
    lines.append(
        "- Fill-basis R accounting excludes unfilled episodes from "
        "mean/median/total; `fill_rate` and `filled_n`/`scored_n` are always "
        "shown beside them so the exclusion stays visible.")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_HEADER = (
    "table", "key", "n", "share_of_episodes", "filled_n", "scored_n",
    "fill_rate", "total_r", "mean_r", "median_r", "gross_positive_r",
    "gross_negative_r", "mean_increment_miss_count",
    "mean_partial_refresh_count", "median_duration_hours",
)


def csv_rows(payload: dict[str, Any]) -> list:
    rows: list = [list(_CSV_HEADER)]

    def emit(table: str, key: str, record: dict[str, Any]) -> None:
        rows.append([
            table, key,
            record.get("n", ""),
            record.get("share_of_episodes", ""),
            record.get("filled_n", ""),
            record.get("scored_n", ""),
            record.get("fill_rate", ""),
            record.get("total_r", ""),
            record.get("mean_r", ""),
            record.get("median_r", ""),
            record.get("gross_positive_r", ""),
            record.get("gross_negative_r", ""),
            record.get("mean_increment_miss_count", ""),
            record.get("mean_partial_refresh_count", ""),
            record.get("median_duration_hours", ""),
        ])

    for record in payload.get("increment_miss", {}).get("distribution", []):
        emit("increment_miss", record["bin"], record)
    for record in payload.get("partial_refresh", {}).get("distribution", []):
        emit("partial_refresh", record["bin"], record)
    for record in payload.get("joint_miss_refresh", []):
        emit("joint_miss_refresh",
             f"{record['miss_bin']}__{record['refresh_bin']}", record)
    for record in payload.get("end_reason_accounting", []):
        emit("end_reason", record["end_reason"], record)
    for record in payload.get("sessions", {}).get("rows", []):
        emit("session", record["session"], record)

    return rows


def _csv_escape(value: Any) -> str:
    text = str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_lifecycle_analysis(payload: dict[str, Any],
                             out_dir: Path | str) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    written: dict[str, Path] = {}
    json_path = out / "lifecycle_analysis.json"
    json_path.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written["lifecycle_analysis.json"] = json_path

    md_path = out / "lifecycle_analysis.md"
    md_path.write_text(render(body), encoding="utf-8")
    written["lifecycle_analysis.md"] = md_path

    csv_path = out / "lifecycle_analysis.csv"
    csv_path.write_text(
        "\n".join(",".join(_csv_escape(cell) for cell in row)
                  for row in csv_rows(payload)) + "\n", encoding="utf-8")
    written["lifecycle_analysis.csv"] = csv_path
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Explicit entry point. Deliberately not a CLI verb."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="lifecycle_analysis",
        description="Descriptive IncrementMiss/PartialRefresh lifecycle "
                    "analysis over a reconstructed episode ledger.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    payload = analyse(run_dir)
    out_dir = (Path(args.out) if args.out
               else run_dir / "analytics" / "research" / DIRNAME)
    written = write_lifecycle_analysis(payload, out_dir)
    for name, path in sorted(written.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
