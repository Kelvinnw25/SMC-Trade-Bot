"""The labeled dataset — §11 step 4. `ledger ⋈ outcomes` on the canonical row.

REPLAY_CONTRACT.md §11 step 4, C-9.4 (the canonical-row rule), C-9.5 (one-way
derivation), C-15.8 / §12 (the data-health block).

## Why this is not part of the metrics engine

The Metrics Engine consumes **`OutcomeRecord`s only** — it never opens a ledger, a
capture file or a repository. But every strategy-context dimension §12 asks it to
segment by (`regime`, the HTF biases, `session_active`, `sweep`, `fvg`, `fresh_ob`,
`notes` tokens, `tp_rr`, the selected-OB metadata, …) lives in the **signal
ledger**, not in `OutcomeRecord`. All 26 of them — measured, not assumed.

§11 resolves this by making the join its own step: step 4 produces the *labeled
dataset*, step 5 computes statistics over it. This module is step 4. It is the only
place in the evaluation stack that opens `signal_ledger.jsonl`, and it hands the
metrics engine two plain mappings:

    records  tuple[OutcomeRecord, ...]
    labels   {canonical_key: {attribute: value}}

The metrics engine treats `labels` as opaque. It does not know a ledger exists, so
it acquires no ledger-shaped assumption, and a live-sourced labeled dataset feeds
it through the identical path (Task 9).

## The canonical key

`(run_id, symbol, h1_last_ts)` — C-9.4. Unique by construction at replay cadence:
one row per symbol per cycle, one cycle per `h1_last_ts`. For live data C-9.4 adds
`cycle_index` to disambiguate, so the key carries it as a fourth, optional
component and live joins remain exact rather than approximate.

## The signal population (M14)

`LabeledDataset` answers "what happened to the trades we took". `SignalPopulation`
answers the complementary question the metrics engine structurally cannot: **what
did the strategy decline, and why**. A `SKIP` or `WATCH` decision never
instantiates a trade, so it has no `OutcomeRecord` and is invisible to every
metric — yet on the official baseline that is 669,245 SKIP and 129,677 WATCH rows
against 118,201 instantiations. Analysing the funnel needs the ledger itself.

It lives here rather than in a new reader because of the sentence three
paragraphs up: this module is the one place in the evaluation stack that opens
`signal_ledger.jsonl`, and that invariant is worth more than the convenience of
putting a second reader next to its only caller.

It **streams**. `LabeledDataset` indexes the whole ledger by canonical key because
a join needs random access; a population summary does not, so `SignalPopulation`
holds counters bounded by the number of distinct decision/mode/score values rather
than by the 1.27 GB file it reads.

## What this module never does

It does not compute a metric, it does not write to any replay artifact, and it does
not read prices. Outcomes remain a one-way projection (C-9.5): nothing here is ever
written back into the ledger.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Optional

from backtest.outcome.record import OutcomeRecord

__all__ = [
    "CanonicalKey",
    "DatasetError",
    "HEALTH_FILENAME",
    "HealthSummary",
    "LEDGER_FILENAME",
    "LabeledDataset",
    "MANIFEST_FILENAME",
    "OUTCOMES_FILENAME",
    "SignalPopulation",
    "canonical_key",
    "label_attributes",
    "read_outcomes",
]

OUTCOMES_FILENAME = "outcomes.jsonl"
LEDGER_FILENAME = "signal_ledger.jsonl"
HEALTH_FILENAME = "run_health.jsonl"
MANIFEST_FILENAME = "manifest.json"

CanonicalKey = tuple[str, str, int, Optional[int]]

#: Every ledger field the labeled dataset carries forward, and nothing else.
#:
#: An explicit list rather than "the whole row": a segmentation dimension is a
#: deliberate analytical choice, and copying the row wholesale would quietly make
#: every future ledger field a dimension nobody decided to add. Each name here
#: appears in §12's segmentation inventory.
LABEL_FIELDS = (
    # regime and timing
    "regime", "session_active", "recorded_at",
    # HTF
    "weekly_bias", "d1_bias", "h4_bias", "location", "direction_source",
    "htf_bull", "htf_bear", "anchor_override", "veto_active", "momentum_weakening",
    # confirmations
    "sweep", "fvg", "displacement", "fresh_ob", "volume_spike", "target_clear",
    "htf_context_clean", "m5_choch",
    # entry / risk detail
    "entry_ready", "rr", "tp_rr", "atr_last", "reason", "notes",
    # order-block metadata
    "selected_ob_rank", "selected_ob_fresh", "ob_candidate_count",
    "sweep_reclaim_strength",
    # served window shape (C-16.1) — the window-truncation flag of §12
    "d1_rows", "h1_rows", "m5_rows",
    # the ledger's own view of the decision, for cross-checking the outcome's tier
    "decision", "score",
)


class DatasetError(Exception):
    """The labeled dataset cannot be assembled from what is on disk."""


def canonical_key(
    run_id: str, symbol: str, h1_last_ts: float,
    cycle_index: Optional[int] = None,
) -> CanonicalKey:
    """C-9.4's canonical row key.

    `cycle_index` is carried but defaults to `None`, because at replay cadence the
    first three components are already unique and including it would make a
    replay join depend on a field live needs and replay does not.
    """
    return (run_id, symbol, int(h1_last_ts), cycle_index)


def label_attributes(row: dict[str, Any]) -> dict[str, Any]:
    """The `LABEL_FIELDS` projection of one ledger row."""
    return {name: row.get(name) for name in LABEL_FIELDS}


# ---------------------------------------------------------------------------
# Reading outcomes
# ---------------------------------------------------------------------------

_OUTCOME_FIELDS = {f.name for f in fields(OutcomeRecord)}

#: Fields a line may omit. Every one of them is a schema addition that postdates
#: some file on disk, so demanding it would make this reader refuse records it can
#: read perfectly well — outcomes are re-derivable (C-9.5), but an old file is
#: still valid evidence for the snapshot it was derived from. Absent means `None`
#: (undeclared), and an undeclared comparison key is refused later, at
#: `metrics.freeze()`, where the consequence is understood.
_OPTIONAL_FIELDS = frozenset({
    "signal_id", "env", "burn_in_h1", "universe_mode",
})


def read_outcomes(path: Path | str) -> tuple[OutcomeRecord, ...]:
    """Parse `outcomes.jsonl` into `OutcomeRecord`s.

    The reader lives here rather than beside `OutcomeRecord.to_dict` because the
    Outcome Engine is frozen for this milestone. That is a real drift risk — a new
    record field would be silently dropped here — so unknown keys are **rejected**
    rather than ignored: a schema this reader does not understand must fail loudly,
    not quietly lose a column. See the M10 findings.
    """
    path = Path(path)
    if not path.exists():
        raise DatasetError(
            f"no {path.name} at {path}. The Outcome Engine writes it; metrics "
            f"never derive outcomes themselves (C-9.1).")
    records: list[OutcomeRecord] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            unknown = set(row) - _OUTCOME_FIELDS
            if unknown:
                raise DatasetError(
                    f"{path.name}:{number} carries unknown field(s) "
                    f"{sorted(unknown)}. The outcome schema changed without this "
                    f"reader; refusing rather than dropping columns silently.")
            missing = _OUTCOME_FIELDS - set(row) - _OPTIONAL_FIELDS
            if missing:
                raise DatasetError(
                    f"{path.name}:{number} is missing {sorted(missing)}")
            records.append(OutcomeRecord(**row))
    return tuple(records)


# ---------------------------------------------------------------------------
# Data health (§12: mandatory in every report)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HealthSummary:
    """The data-health block §12 requires in every report.

    Sourced from `run_health.jsonl` (C-15.8), which the driver owns. None of it is
    derivable from `OutcomeRecord`s, and §12 makes it mandatory — so it is read
    here, at the same stage as the ledger join, and handed to the report as plain
    data. A report without it is not conforming, which is why `cycles` being zero
    is itself reported rather than defaulted away.
    """

    cycles: int = 0
    cycles_processed: int = 0
    universe_total: int = 0
    ledger_rows: int = 0
    decisions: int = 0
    snapshots_none: int = 0
    guard_skips: int = 0
    swallowed_before_ledger: int = 0
    swallowed_after_ledger: int = 0
    universe_errors: int = 0
    m5_attached: int = 0
    m5_guard_failures: int = 0
    data_errors: int = 0
    ttl_inferred: int = 0
    ttl_log_lines: int = 0

    @property
    def coverage(self) -> Optional[float]:
        """Ledger rows per universe slot offered — §12's universe coverage."""
        if self.universe_total <= 0:
            return None
        return self.ledger_rows / self.universe_total

    @property
    def clean(self) -> bool:
        """C-15.8: both swallowed counts and every error channel at zero."""
        return (self.swallowed_before_ledger == 0
                and self.swallowed_after_ledger == 0
                and self.universe_errors == 0
                and self.data_errors == 0
                and self.ttl_inferred == self.ttl_log_lines)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "cycles": self.cycles,
            "cycles_processed": self.cycles_processed,
            "universe_total": self.universe_total,
            "ledger_rows": self.ledger_rows,
            "decisions": self.decisions,
            "snapshots_none": self.snapshots_none,
            "guard_skips": self.guard_skips,
            "swallowed_before_ledger": self.swallowed_before_ledger,
            "swallowed_after_ledger": self.swallowed_after_ledger,
            "universe_errors": self.universe_errors,
            "m5_attached": self.m5_attached,
            "m5_guard_failures": self.m5_guard_failures,
            "data_errors": self.data_errors,
            "ttl_inferred": self.ttl_inferred,
            "ttl_log_lines": self.ttl_log_lines,
        }
        payload["universe_coverage"] = self.coverage
        payload["clean"] = self.clean
        return payload

    @classmethod
    def from_health_file(cls, path: Path | str) -> "HealthSummary":
        path = Path(path)
        if not path.exists():
            return cls()
        rows = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if not rows:
            return cls()

        def total(key: str) -> int:
            return sum(int(row.get(key) or 0) for row in rows)

        return cls(
            cycles=len(rows),
            cycles_processed=sum(1 for row in rows if row.get("processed")),
            universe_total=total("universe_size"),
            ledger_rows=total("ledger_rows"),
            decisions=total("decisions"),
            snapshots_none=total("snapshots_none"),
            guard_skips=total("guard_skips"),
            swallowed_before_ledger=total("swallowed_before_ledger"),
            swallowed_after_ledger=total("swallowed_after_ledger"),
            universe_errors=sum(1 for row in rows
                                if row.get("universe_error") is not None),
            m5_attached=total("m5_attached"),
            m5_guard_failures=total("m5_guard_failures"),
            data_errors=total("data_errors"),
            ttl_inferred=total("ttl_inferred"),
            ttl_log_lines=total("ttl_log_lines"),
        )


# ---------------------------------------------------------------------------
# The labeled dataset
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LabeledDataset:
    """Outcomes, their strategy-context labels, and the run's data health.

    The three inputs the metrics engine needs, assembled once. `labels` may be
    empty — a dataset with no ledger still produces every metric that depends only
    on outcomes, and the report says which dimensions were unavailable rather than
    reporting them as empty.
    """

    records: tuple[OutcomeRecord, ...]
    labels: dict[CanonicalKey, dict[str, Any]] = field(default_factory=dict)
    health: HealthSummary = field(default_factory=HealthSummary)
    run_dir: Optional[Path] = None
    ledger_rows: int = 0
    joined: int = 0
    unjoined: int = 0

    def __len__(self) -> int:
        return len(self.records)

    @property
    def join_rate(self) -> Optional[float]:
        if not self.records:
            return None
        return self.joined / len(self.records)

    def labels_for(self, record: OutcomeRecord) -> dict[str, Any]:
        """This record's labels, or `{}` when the join found no row."""
        return self.labels.get(_key_of(record), {})

    def to_dict(self) -> dict[str, Any]:
        return {
            # The run directory's **name**, never its path. The name is the
            # `run_id` and therefore identity; the path is where one person
            # happened to put it. Emitting the full path made `metrics.json`
            # differ between two byte-identical pipelines run from different
            # directories — measured in M11 Task 8, where it was the single
            # difference between two otherwise identical reports.
            "run": None if self.run_dir is None else Path(self.run_dir).name,
            "outcomes": len(self.records),
            "ledger_rows": self.ledger_rows,
            "joined": self.joined,
            "unjoined": self.unjoined,
            "join_rate": self.join_rate,
            "label_fields": list(LABEL_FIELDS),
        }

    # -- assembly ----------------------------------------------------------

    @classmethod
    def from_run(cls, run_dir: Path | str, *, require_ledger: bool = True
                 ) -> "LabeledDataset":
        """Assemble from a completed run directory.

        `require_ledger=False` builds an outcomes-only dataset, which is what a
        future live source without a co-located ledger would supply. The metrics
        engine behaves identically either way; only the available dimensions
        differ.
        """
        run_dir = Path(run_dir)
        records = read_outcomes(run_dir / OUTCOMES_FILENAME)
        ledger_path = run_dir / LEDGER_FILENAME
        if require_ledger and not ledger_path.exists():
            raise DatasetError(
                f"no {LEDGER_FILENAME} in {run_dir}. §11 step 4 joins outcomes to "
                f"the ledger on the canonical row; pass require_ledger=False for "
                f"an outcomes-only dataset (fewer segmentation dimensions).")
        labels, ledger_rows = _read_labels(ledger_path)
        joined = sum(1 for record in records if _key_of(record) in labels)
        return cls(
            records=records,
            labels=labels,
            health=HealthSummary.from_health_file(run_dir / HEALTH_FILENAME),
            run_dir=run_dir,
            ledger_rows=ledger_rows,
            joined=joined,
            unjoined=len(records) - joined,
        )

    @classmethod
    def from_records(
        cls,
        records: Iterable[OutcomeRecord],
        *,
        labels: Optional[dict[CanonicalKey, dict[str, Any]]] = None,
        health: Optional[HealthSummary] = None,
    ) -> "LabeledDataset":
        """Assemble in memory — the path tests and a future live feed use."""
        records = tuple(records)
        labels = dict(labels or {})
        joined = sum(1 for record in records if _key_of(record) in labels)
        return cls(
            records=records, labels=labels,
            health=health if health is not None else HealthSummary(),
            ledger_rows=len(labels), joined=joined,
            unjoined=len(records) - joined,
        )


# ---------------------------------------------------------------------------
# The signal population (M14)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalPopulation:
    """What the strategy decided, across every analysed symbol-cycle.

    The counterpart to `LabeledDataset`: that one describes trades, this one
    describes **decisions**, including the overwhelming majority that never
    became a trade. Counts only — no metric, no R, no expectancy. A decision that
    produced no trade has no outcome to average, so any attempt to score this
    population would be inventing a number.

    Every mapping is keyed by the ledger's own vocabulary and is left exactly as
    the ledger wrote it: `decision` is `SKIP`/`WATCH`/`VALID`/`A+`, `entry_mode`
    is `NONE`/`CONTINUATION`/`RETRACEMENT` — and, if the strategy ever emits it,
    `WATCH_ONLY`. Absent keys are absent, never zero-filled: "the strategy never
    produced this" and "this bucket happens to be empty" are different facts.
    """

    ledger_rows: int = 0
    by_decision: dict[str, int] = field(default_factory=dict)
    by_entry_mode: dict[str, int] = field(default_factory=dict)
    by_mode_decision: dict[str, int] = field(default_factory=dict)
    by_direction: dict[str, int] = field(default_factory=dict)
    by_score: dict[str, int] = field(default_factory=dict)
    entry_ready: dict[str, int] = field(default_factory=dict)
    #: The ledger's `reason` — why analysis declined to produce an entry. Absent
    #: on rows that did produce one, so the counts here cover rejections only.
    by_reason: dict[str, int] = field(default_factory=dict)
    by_mode_reason: dict[str, int] = field(default_factory=dict)

    #: Decisions that can carry an alert, in ascending tier order. `SKIP` and
    #: `WATCH` are the two that never instantiate a trade under C-2.1.
    ACTIONABLE = ("VALID", "A+")

    @property
    def actionable(self) -> int:
        return sum(self.by_decision.get(name, 0) for name in self.ACTIONABLE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_rows": self.ledger_rows,
            "by_decision": dict(sorted(self.by_decision.items())),
            "by_entry_mode": dict(sorted(self.by_entry_mode.items())),
            "by_mode_decision": dict(sorted(self.by_mode_decision.items())),
            "by_direction": dict(sorted(self.by_direction.items())),
            "by_score": dict(sorted(self.by_score.items(),
                                    key=lambda item: _score_order(item[0]))),
            "entry_ready": dict(sorted(self.entry_ready.items())),
            "by_reason": dict(sorted(self.by_reason.items())),
            "by_mode_reason": dict(sorted(self.by_mode_reason.items())),
            "actionable": self.actionable,
        }

    @classmethod
    def from_run(cls, run_dir: Path | str) -> "SignalPopulation":
        """Stream `signal_ledger.jsonl` and tally. Memory is bounded by the number
        of distinct keys, not by the size of the file."""
        path = Path(run_dir) / LEDGER_FILENAME
        if not path.exists():
            raise DatasetError(
                f"no {LEDGER_FILENAME} in {run_dir}. The signal population is the "
                f"ledger; without it the decisions the strategy declined cannot be "
                f"counted at all.")
        rows = 0
        decision: dict[str, int] = {}
        mode: dict[str, int] = {}
        mode_decision: dict[str, int] = {}
        direction: dict[str, int] = {}
        score: dict[str, int] = {}
        ready: dict[str, int] = {}
        reason: dict[str, int] = {}
        mode_reason: dict[str, int] = {}

        def tally(target: dict[str, int], key: Any) -> None:
            if key is None or key == "":
                return
            name = str(key)
            target[name] = target.get(name, 0) + 1

        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                rows += 1
                tally(decision, row.get("decision"))
                tally(mode, row.get("entry_mode"))
                tally(direction, row.get("direction"))
                tally(score, row.get("score"))
                tally(ready, row.get("entry_ready"))
                tally(reason, row.get("reason"))
                if row.get("entry_mode") and row.get("decision"):
                    tally(mode_decision,
                          f"{row['entry_mode']}|{row['decision']}")
                if row.get("entry_mode") and row.get("reason"):
                    tally(mode_reason, f"{row['entry_mode']}|{row['reason']}")
        return cls(
            ledger_rows=rows, by_decision=decision, by_entry_mode=mode,
            by_mode_decision=mode_decision, by_direction=direction,
            by_score=score, entry_ready=ready, by_reason=reason,
            by_mode_reason=mode_reason)


def _score_order(key: str) -> tuple[int, Any]:
    """Numeric scores sort numerically; anything else sorts after, by text."""
    try:
        return (0, int(float(key)))
    except (TypeError, ValueError):
        return (1, key)


def _key_of(record: OutcomeRecord) -> CanonicalKey:
    return canonical_key(record.run_id, record.symbol, record.h1_last_ts)


def _read_labels(path: Path) -> tuple[dict[CanonicalKey, dict[str, Any]], int]:
    """Index the ledger by canonical row.

    A duplicate key is a contract violation, not a merge to resolve: C-9.4 says
    the key is unique by construction at replay cadence, so two rows sharing it
    means the file holds more than one run or more than one cycle per
    `h1_last_ts`. Refusing is the only honest response — silently keeping the last
    row would attribute one trade's outcome to another scan's context.
    """
    labels: dict[CanonicalKey, dict[str, Any]] = {}
    count = 0
    if not path.exists():
        return labels, count
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            count += 1
            key = canonical_key(
                str(row.get("run_id") or ""), str(row.get("symbol") or ""),
                float(row.get("h1_last_ts") or 0.0))
            if key in labels:
                raise DatasetError(
                    f"{path.name}:{number} repeats the canonical row {key}. "
                    f"C-9.4 makes `(run_id, symbol, h1_last_ts)` unique at replay "
                    f"cadence; a repeat means two runs or two cycles share one "
                    f"reference candle, and the join would attribute the wrong "
                    f"context to a trade.")
            labels[key] = label_attributes(row)
    return labels, count
