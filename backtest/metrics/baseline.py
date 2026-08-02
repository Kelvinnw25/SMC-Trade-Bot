"""Baseline freeze and comparison readiness (Task 8).

REPLAY_CONTRACT.md §11 step 5 ("freeze the baseline — git tag + committed
`metrics.json` + manifest"), C-5.7.2 and C-16.6.4 (the metrics engine **MUST refuse**
to diff two runs whose `data_snapshot_id`, window, `burn_in_h1` or `universe_mode`
differ), §12.

## What a frozen baseline is

A directory holding `metrics.json`, `report.md`, `segments.csv` and one small
`baseline.json` lock file. The lock is what makes the freeze mean something: it
records the **comparison key** — every input that must match before a diff is
meaningful — plus a content hash of the numbers, so a baseline that was edited after
freezing fails verification rather than quietly becoming a different reference.

Every future refinement A1–A25 compares against this, and comparison is refused
unless the key matches. That refusal is the whole point. The commonest way a
backtest lies is by comparing two runs that differ in something nobody noticed, and
C-5.7.2 exists to make that impossible rather than unlikely.

## What it deliberately does not do

No refinement is implemented, no parameter is tuned, no hypothesis is tested. This
module stores a reference and answers one question about a candidate: *is this
comparable, and if so, what moved.* Whether a move is good is a judgement the
Experiment Log records, not a number this module emits.

## The gap you must know about

`OutcomeRecord` carries neither `burn_in_h1` nor `universe_mode`, so C-5.7.2's two
remaining preconditions **cannot be recovered from outcome records** — they have to
be declared by the caller through `ReportInputs`. `ComparisonKey` therefore treats a
missing declaration as a hard failure rather than a match: an undeclared key would
let two incomparable runs compare cleanly, which is worse than not comparing at all.
See the M10 findings.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backtest.data.repository import canonical_json
from backtest.metrics.definitions import METRIC_ENGINE_VERSION, METRICS_BY_NAME
from backtest.metrics.report import (
    BaselineReport, HEADLINE, METRICS_FILENAME, REPORT_FILENAME,
    SEGMENTS_FILENAME,
)

__all__ = [
    "BASELINE_FILENAME",
    "Baseline",
    "BaselineError",
    "ComparisonKey",
    "IncomparableBaseline",
    "MetricDelta",
    "compare",
    "freeze",
    "load_baseline",
]

BASELINE_FILENAME = "baseline.json"


class BaselineError(Exception):
    """A baseline cannot be frozen, loaded or verified."""


class IncomparableBaseline(BaselineError):
    """Two runs differ on a C-5.7.2 precondition, so no diff is meaningful."""


# ---------------------------------------------------------------------------
# The comparison key
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ComparisonKey:
    """Everything that must match before two runs may be compared.

    `strategy_version` is deliberately **absent from the strict key**: changing it
    is the entire point of an experiment. Everything else is held constant, which is
    what makes the strategy the only moving part — the definition of a controlled
    comparison.
    """

    data_snapshot_id: str
    window_start: Optional[int]
    window_end: Optional[int]
    burn_in_h1: Optional[int]
    universe_mode: Optional[str]
    contract_version: str
    outcome_config_hash: str
    ledger_schema_version: Optional[int] = None
    outcome_schema_version: Optional[int] = None
    metrics_engine_version: int = METRIC_ENGINE_VERSION
    bootstrap_seed: Optional[int] = None
    top_coins_limit: Optional[int] = None

    #: C-5.7.2 / C-16.6.4 name these four explicitly. A `None` in any of them is a
    #: *missing declaration*, not a wildcard.
    REQUIRED = ("data_snapshot_id", "window_start", "window_end", "burn_in_h1",
                "universe_mode")

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_snapshot_id": self.data_snapshot_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "burn_in_h1": self.burn_in_h1,
            "universe_mode": self.universe_mode,
            "contract_version": self.contract_version,
            "outcome_config_hash": self.outcome_config_hash,
            "ledger_schema_version": self.ledger_schema_version,
            "outcome_schema_version": self.outcome_schema_version,
            "metrics_engine_version": self.metrics_engine_version,
            "bootstrap_seed": self.bootstrap_seed,
            "top_coins_limit": self.top_coins_limit,
        }

    @property
    def undeclared(self) -> tuple[str, ...]:
        return tuple(name for name in self.REQUIRED
                     if getattr(self, name) in (None, ""))

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict()).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_report(cls, report: BaselineReport) -> "ComparisonKey":
        """Derive from a report, refusing to guess where a population is mixed.

        A population spanning two snapshot ids or two outcome-config hashes has no
        single key, and choosing one would produce a reference that silently
        describes a subset of its own data.
        """
        lineage = report.lineage
        snapshot = _single(lineage["data_snapshot_ids"], "data_snapshot_id")
        contract = _single(lineage["contract_versions"], "contract_version")
        config = _single(lineage["outcome_config_hashes"], "outcome_config_hash")
        window = lineage["window"]
        return cls(
            data_snapshot_id=snapshot,
            window_start=window["declared_start"] or window["first_boundary"],
            window_end=window["declared_end"] or window["last_boundary"],
            burn_in_h1=lineage["burn_in_h1"],
            universe_mode=lineage["universe_mode"],
            contract_version=contract,
            outcome_config_hash=config,
            ledger_schema_version=_first_int(
                lineage["ledger_schema_versions"]),
            outcome_schema_version=_first_int(
                lineage["outcome_schema_versions"]),
            metrics_engine_version=METRIC_ENGINE_VERSION,
            bootstrap_seed=report.bootstrap_seed,
            top_coins_limit=lineage.get("top_coins_limit"),
        )

    def mismatches(self, other: "ComparisonKey") -> dict[str, tuple[Any, Any]]:
        """The C-5.7.2 fields on which two keys differ."""
        differences: dict[str, tuple[Any, Any]] = {}
        for name in ("data_snapshot_id", "window_start", "window_end",
                     "burn_in_h1", "universe_mode", "contract_version",
                     "outcome_config_hash", "metrics_engine_version",
                     "bootstrap_seed"):
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine != theirs:
                differences[name] = (mine, theirs)
        return differences


def _single(values: list[str], name: str) -> str:
    if not values:
        raise BaselineError(
            f"the population declares no {name}; a baseline without it cannot be "
            f"compared safely (C-5.7.2)")
    if len(values) > 1:
        raise BaselineError(
            f"the population spans {len(values)} {name} values ({values}). A "
            f"baseline must describe one; segment first (C-16.6.3).")
    return values[0]


def _first_int(values: list[int]) -> Optional[int]:
    return int(values[0]) if values else None


# ---------------------------------------------------------------------------
# The frozen baseline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Baseline:
    """A frozen reference point."""

    baseline_id: str
    label: str
    key: ComparisonKey
    metrics_hash: str
    headline: dict[str, Optional[float]]
    counts: dict[str, int]
    #: Recorded but **not** part of the comparison key: changing the strategy is the
    #: point of an experiment, so it is the one thing allowed to differ.
    strategy_versions: tuple[str, ...] = ()
    frozen_at: Optional[str] = None
    git_commit: Optional[str] = None
    notes: tuple[str, ...] = ()
    path: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "baseline_lock",
            "baseline_id": self.baseline_id,
            "label": self.label,
            "comparison_key": self.key.to_dict(),
            "comparison_key_digest": self.key.digest,
            "strategy_versions": list(self.strategy_versions),
            "metrics_hash": self.metrics_hash,
            "headline": dict(sorted(self.headline.items())),
            "counts": dict(sorted(self.counts.items())),
            "metric_definitions": {
                name: METRICS_BY_NAME[name].formula
                for name in sorted(self.headline)
                if name in METRICS_BY_NAME
            },
            "notes": list(self.notes),
        }
        if self.frozen_at is not None:
            payload["frozen_at"] = self.frozen_at
        if self.git_commit is not None:
            payload["git_commit"] = self.git_commit
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True,
                          allow_nan=False) + "\n"

    def verify(self, report: BaselineReport) -> None:
        """Confirm a report still reproduces this baseline's numbers.

        The check that makes a freeze a freeze: if `metrics.json` were edited, or
        the engine's arithmetic drifted, the hash moves and this raises.
        """
        if report.metrics_hash != self.metrics_hash:
            raise BaselineError(
                f"baseline {self.baseline_id} was frozen with metrics_hash "
                f"{self.metrics_hash} but the report now hashes to "
                f"{report.metrics_hash}. Either the outcome records changed or a "
                f"metric definition did; neither is a comparison, both are a new "
                f"baseline.")


def freeze(
    report: BaselineReport,
    out_dir: Path | str,
    *,
    label: str,
    frozen_at: Optional[str] = None,
    git_commit: Optional[str] = None,
    notes: tuple[str, ...] = (),
    overwrite: bool = False,
    allow_undeclared: bool = False,
    allow_not_inferential: bool = False,
) -> Baseline:
    """Write a baseline bundle: the three report artifacts plus the lock file.

    Two guards, both refusable only on purpose:

    - **undeclared comparison key.** C-5.7.2 names four preconditions; two of them
      (`burn_in_h1`, `universe_mode`) cannot be recovered from outcome records. A
      freeze without them produces a reference nothing can safely be compared to,
      so it is refused unless `allow_undeclared=True`.
    - **not inferential.** §12 forbids a population below `min_segment_n` from
      justifying a change. Freezing one as *the* permanent baseline is exactly that,
      one step removed, so it is refused unless `allow_not_inferential=True` —
      which is the right flag for a pipeline smoke test and the wrong one for a real
      baseline.
    """
    key = ComparisonKey.from_report(report)
    if key.undeclared and not allow_undeclared:
        raise BaselineError(
            f"comparison key is incomplete: {list(key.undeclared)} not declared. "
            f"C-5.7.2 makes these preconditions for comparing two runs, and no "
            f"OutcomeRecord carries burn_in_h1 or universe_mode — supply them via "
            f"ReportInputs, or pass allow_undeclared=True to freeze a reference "
            f"that cannot be safely diffed.")
    if not report.global_metrics.inferential and not allow_not_inferential:
        raise BaselineError(
            f"the population has "
            f"{report.global_metrics.counts.get('scored_trades', 0)} scored "
            f"trade(s), below min_segment_n={report.min_segment_n}. §12 forbids "
            f"such a population from justifying a change; freezing it as the "
            f"permanent baseline would do so by proxy. Pass "
            f"allow_not_inferential=True for a pipeline smoke test.")

    out = Path(out_dir)
    report.write(out, overwrite=overwrite)

    headline = {name: report.global_metrics.values.get(name)
                for name in HEADLINE}
    baseline = Baseline(
        baseline_id=_baseline_id(key, report.metrics_hash),
        label=label,
        key=key,
        metrics_hash=report.metrics_hash,
        headline=headline,
        counts=dict(report.global_metrics.counts),
        strategy_versions=tuple(report.lineage["strategy_versions"]),
        frozen_at=frozen_at,
        git_commit=git_commit,
        notes=notes,
        path=out,
    )
    lock = out / BASELINE_FILENAME
    if lock.exists() and not overwrite:
        raise BaselineError(
            f"{lock} already exists. A frozen baseline is permanent; pass "
            f"overwrite=True only when you mean to replace the reference.")
    lock.write_text(baseline.to_json(), encoding="utf-8")
    return baseline


def _baseline_id(key: ComparisonKey, metrics_hash: str) -> str:
    """`sha256(comparison key + metrics hash)[:16]`.

    Both halves matter: the key alone would collide across two strategies over the
    same window, and the hash alone would collide across two windows that happened
    to produce the same numbers.
    """
    payload = canonical_json({"key": key.to_dict(), "metrics": metrics_hash})
    return "baseline-" + hashlib.sha256(
        payload.encode("utf-8")).hexdigest()[:16]


def load_baseline(path: Path | str) -> Baseline:
    """Read a lock file back."""
    path = Path(path)
    if path.is_dir():
        path = path / BASELINE_FILENAME
    if not path.exists():
        raise BaselineError(f"no baseline lock at {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "baseline_lock":
        raise BaselineError(
            f"{path} is not a baseline lock (schema="
            f"{payload.get('schema')!r})")
    return Baseline(
        baseline_id=payload["baseline_id"],
        label=payload["label"],
        key=ComparisonKey(**payload["comparison_key"]),
        metrics_hash=payload["metrics_hash"],
        headline=payload["headline"],
        counts=payload["counts"],
        strategy_versions=tuple(payload.get("strategy_versions") or ()),
        frozen_at=payload.get("frozen_at"),
        git_commit=payload.get("git_commit"),
        notes=tuple(payload.get("notes") or ()),
        path=path.parent,
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricDelta:
    """One metric, baseline vs candidate."""

    name: str
    baseline: Optional[float]
    candidate: Optional[float]

    @property
    def delta(self) -> Optional[float]:
        if self.baseline is None or self.candidate is None:
            return None
        return self.candidate - self.baseline

    @property
    def relative(self) -> Optional[float]:
        if self.baseline in (None, 0) or self.candidate is None:
            return None
        return (self.candidate - self.baseline) / abs(self.baseline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.name,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
            "relative": self.relative,
            "formula": (METRICS_BY_NAME[self.name].formula
                        if self.name in METRICS_BY_NAME else None),
        }


@dataclass(frozen=True)
class Comparison:
    """A candidate measured against a frozen baseline."""

    baseline_id: str
    baseline_label: str
    deltas: tuple[MetricDelta, ...]
    candidate_metrics_hash: str
    candidate_inferential: bool
    strategy_versions: tuple[str, str]
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unchanged(self) -> bool:
        return all(delta.delta in (None, 0) for delta in self.deltas)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "baseline_comparison",
            "baseline_id": self.baseline_id,
            "baseline_label": self.baseline_label,
            "candidate_metrics_hash": self.candidate_metrics_hash,
            "candidate_inferential": self.candidate_inferential,
            "strategy_version_baseline": self.strategy_versions[0],
            "strategy_version_candidate": self.strategy_versions[1],
            "unchanged": self.unchanged,
            "deltas": [delta.to_dict() for delta in self.deltas],
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True,
                          allow_nan=False) + "\n"


def compare(baseline: Baseline, candidate: BaselineReport) -> Comparison:
    """Diff a candidate against a frozen baseline, or refuse.

    Refusal is not a failure mode — it is the feature. C-5.7.2 and C-16.6.4 make the
    engine responsible for declining a comparison that cannot mean anything, so a
    mismatched snapshot, window, burn-in or universe policy raises here rather than
    producing a plausible-looking delta table.
    """
    candidate_key = ComparisonKey.from_report(candidate)
    mismatches = baseline.key.mismatches(candidate_key)
    if mismatches:
        detail = "; ".join(
            f"{name}: baseline={left!r} candidate={right!r}"
            for name, (left, right) in sorted(mismatches.items()))
        raise IncomparableBaseline(
            f"cannot diff against {baseline.baseline_id}: {detail}. C-5.7.2 "
            f"requires data_snapshot_id, window, burn_in_h1 and universe_mode to "
            f"match; the metrics engine MUST refuse otherwise (C-16.6.4).")

    notes: list[str] = []
    if not candidate.global_metrics.inferential:
        notes.append(
            f"the candidate has "
            f"{candidate.global_metrics.counts.get('scored_trades', 0)} scored "
            f"trades, below min_segment_n={candidate.min_segment_n}: §12 forbids "
            f"this population from justifying a change.")
    if candidate.segmentation.multiple_comparison_caution:
        notes.append(
            f"{candidate.segmentation.total_segments} segments were reported; "
            f"treat any single-segment improvement as a hypothesis, not evidence.")

    return Comparison(
        baseline_id=baseline.baseline_id,
        baseline_label=baseline.label,
        deltas=tuple(
            MetricDelta(name, baseline.headline.get(name),
                        candidate.global_metrics.values.get(name))
            for name in HEADLINE),
        candidate_metrics_hash=candidate.metrics_hash,
        candidate_inferential=candidate.global_metrics.inferential,
        strategy_versions=(
            ", ".join(baseline.strategy_versions) or "—",
            ", ".join(candidate.lineage["strategy_versions"]) or "—"),
        notes=tuple(notes),
    )
