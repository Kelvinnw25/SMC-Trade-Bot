"""Baseline scoring identity — re-run the score engine over the recorded ledger.

Phase 3.5A. The question: **does the installed code still produce the frozen
baseline's decisions?** Any experiment measured against a drifted control reports
drift plus effect, indistinguishably, so this has to be answerable exactly and
cheaply.

## The trick, and its exact scope

`score_signal` (v2/analysis/scoring.py) is a pure function of three dicts. Every
field it reads is recorded on every `SignalLedgerRecord` — the confirmations, the
HTF map, the levels, the regime, the mode. So the ledger *is* a golden file for
the scoring layer: 886,954 rows of (inputs → score, decision, notes, rr) captured
under `strategy_version` 2.2.0.

Re-running the function over those inputs and comparing to the recorded outputs
proves the scoring layer is unchanged, in minutes instead of the ~7.8 h a replay
costs. What it does **not** prove is stated in `SCOPE` and repeated in the report:
the upstream stages that *produced* those inputs are not re-executed here, so a
change to HTF, structure, order-block, entry or risk logic is invisible to this
check. Only a replay covers those. A test that overstates itself is worse than no
test, so the scope travels with the result.

## Reconstructing the two post-scoring adjustments

The recorded row is not `score_signal`'s raw return. Two things happen after it:

1. **The choppy penalty** (`ScoringStage.run`, scoring.py:774-776): −2 and a
   `choppy_penalty` note, applied to *any* result including the early-return
   SKIPs. `is_market_choppy` needs the H1 frame, which the ledger does not carry —
   but the note does, so the penalty is *reversed* out of the recorded values
   rather than recomputed. The decision tier is chosen inside `score_signal`
   before the penalty lands, so the recorded `decision` needs no adjustment (a V1
   quirk, preserved).
2. **M5 enrichment** (`enrich_with_m5`, scoring.py:650-709): re-scores with
   `m5_setup` and re-applies the penalty. `m5_choch` is recorded, and it is
   exactly `m5_setup["choch_confirmed"]`, so an enriched row is reproduced by
   passing that flag back in. `m5_choch is None` means enrichment did not run.

## Which rows are in scope

Four pre-scoring exits never reach `score_signal` (scoring.py:725-769), and each
is identifiable from recorded fields alone — no guessing:

| exit | ledger signature |
|---|---|
| invalid_input / invalid_htf_map | no HTF context recorded |
| no_direction | `direction is None` |
| veto | `veto_active is True` |
| not-ready | `entry_ready is False` and `entry_mode != "WATCH_ONLY"` |

Those rows are counted, not compared. Everything else must match exactly, and a
single mismatch fails the report.

## The branch census

The same pass counts how often each scoring branch fires. That is evidence about
which score inputs carry information — the EXP-0 question — and it is collected
here rather than in a separate tool because the reconstruction already has every
input in hand. Read `BranchCensus.never_fired` as *"absent from this dataset"*,
never as *"unreachable"*: the census counts occurrences, and only code inspection
can establish reachability. The distinction decides whether removing a branch is
a cleanup or a behavior change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = [
    "LEDGER_FILENAME",
    "SCOPE",
    "BranchCensus",
    "IdentityReport",
    "RowVerdict",
    "rescore_ledger",
]

LEDGER_FILENAME = "signal_ledger.jsonl"

#: What a passing report does and does not establish. Carried into the JSON
#: output so a consumer cannot read the verdict without reading the scope.
SCOPE = {
    "proves": [
        "the score engine (v2.analysis.scoring.score_signal) maps the recorded "
        "inputs to the recorded score/decision/notes/rr, row for row",
        "the decision-tier thresholds are unchanged",
        "the choppy penalty and M5 re-score compose as they did at capture time",
    ],
    "does_not_prove": [
        "that the stages producing those inputs are unchanged (htf, structure, "
        "order_block, fair_value_gap, liquidity, entry, risk, regime) — their "
        "outputs are read from the ledger, not recomputed",
        "that lifecycle admission, alerting or outcome resolution are unchanged",
        "anything about rows that took a pre-scoring exit",
    ],
    "only_a_full_replay_covers": "the stages listed under does_not_prove",
}

#: The three post-scoring notes that are not emitted by `score_signal` itself.
_CHOPPY_NOTE = "choppy_penalty"
_CHOPPY_PENALTY = 2


# ---------------------------------------------------------------------------
# per-row verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RowVerdict:
    """One disagreement between the recorded row and the re-scored result."""

    symbol: str
    h1_last_ts: float
    cycle_index: Optional[int]
    field_name: str
    recorded: Any
    recomputed: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "h1_last_ts": self.h1_last_ts,
            "cycle_index": self.cycle_index,
            "field": self.field_name,
            "recorded": self.recorded,
            "recomputed": self.recomputed,
        }


# ---------------------------------------------------------------------------
# branch census
# ---------------------------------------------------------------------------

@dataclass
class BranchCensus:
    """How often each scoring branch fired, over every row that was scored.

    `denominator` is the number of rows that reached `score_signal`, so a count of
    zero means *this branch contributed nothing to any score in this dataset*.
    It does not mean the branch cannot fire.
    """

    denominator: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    #: Rows that never reached the score engine, by which exit they took.
    exits: dict[str, int] = field(default_factory=dict)

    def hit(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def observe(self, name: str) -> None:
        """Register a branch as *watched* so a zero count is reported, not absent."""
        self.counts.setdefault(name, 0)

    @property
    def never_fired(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, c in self.counts.items() if c == 0))

    @property
    def always_fired(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, c in self.counts.items()
                            if self.denominator and c == self.denominator))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_scored": self.denominator,
            "counts": dict(sorted(self.counts.items())),
            "shares": {n: (c / self.denominator if self.denominator else None)
                       for n, c in sorted(self.counts.items())},
            "pre_scoring_exits": dict(sorted(self.exits.items())),
            "never_fired_in_this_dataset": list(self.never_fired),
            "always_fired_in_this_dataset": list(self.always_fired),
            "reading": "counts are occurrences in THIS dataset. A zero is "
                       "evidence of absence, not proof of unreachability — only "
                       "code inspection establishes that, and the difference "
                       "decides whether removing a branch is a cleanup or a "
                       "behavior change.",
        }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@dataclass
class IdentityReport:
    """The verdict, its scope, and the census collected on the way."""

    run_id: Optional[str]
    strategy_versions: set[str] = field(default_factory=set)
    rows: int = 0
    compared: int = 0
    skipped: int = 0
    matched: int = 0
    mismatches: list[RowVerdict] = field(default_factory=list)
    census: BranchCensus = field(default_factory=BranchCensus)
    #: Cap on retained mismatch detail; the *count* is never capped.
    mismatch_count: int = 0

    @property
    def passed(self) -> bool:
        return self.mismatch_count == 0 and self.compared > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "derived",
            "check": "baseline_scoring_identity",
            "passed": self.passed,
            "run_id": self.run_id,
            "strategy_versions": sorted(self.strategy_versions),
            "rows": self.rows,
            "rows_compared": self.compared,
            "rows_skipped_pre_scoring_exit": self.skipped,
            "rows_matched": self.matched,
            "mismatch_count": self.mismatch_count,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "scope": SCOPE,
            "branch_census": self.census.to_dict(),
        }


# ---------------------------------------------------------------------------
# reconstruction
# ---------------------------------------------------------------------------

def _htf_map(row: dict) -> dict[str, Any]:
    """The five HTF fields `score_signal` reads (scoring.py:376-385)."""
    return {
        "weekly_bias": row.get("weekly_bias"),
        "d1_bias": row.get("d1_bias"),
        "h4_bias": row.get("h4_bias"),
        "location": row.get("location"),
        "momentum_weakening": row.get("momentum_weakening"),
    }


def _h1_setup(row: dict) -> dict[str, Any]:
    """The `h1_setup` dict, rebuilt from the ledger's recorded fields.

    Key names are `score_signal`'s, which differ from the ledger's for the three
    confirmations it reads as `*_valid` (scoring.py:148-152).
    """
    return {
        "direction": row.get("direction"),
        "entry_mode": row.get("entry_mode"),
        "entry_source": row.get("entry_source"),
        "sweep_valid": row.get("sweep"),
        "fvg_valid": row.get("fvg"),
        "displacement_valid": row.get("displacement"),
        "fresh_ob": row.get("fresh_ob"),
        "volume_spike": row.get("volume_spike"),
        "target_clear": row.get("target_clear"),
        "session_active": row.get("session_active"),
        "htf_context_clean": row.get("htf_context_clean"),
        "entry_price": row.get("entry_price"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "tp_source": row.get("tp_source"),
        "tp_rr": row.get("tp_rr"),
    }


def _pre_scoring_exit(row: dict) -> Optional[str]:
    """Which pre-scoring exit this row took, or None if it reached scoring.

    Mirrors `ScoringStage.run` (scoring.py:725-769) in order. Every discriminator
    is a recorded field: nothing is inferred from `reason`, whose strings come
    from the entry engine and are not a closed set.
    """
    if row.get("weekly_bias") is None and row.get("d1_bias") is None:
        # No HTF context was recorded, so `htf` was None: invalid_input or
        # invalid_htf_map. Both exit before any score is computed.
        return "invalid_htf"
    if row.get("direction") is None:
        return "no_direction"
    if row.get("veto_active") is True:
        return "veto"
    if row.get("entry_ready") is not True and row.get("entry_mode") != "WATCH_ONLY":
        return "not_ready"
    return None


def _expected(row: dict) -> tuple[int, tuple[str, ...]]:
    """The raw `score_signal` score and notes, with the choppy penalty reversed.

    The penalty is additive and its note is appended last, so removing one
    occurrence of the note and adding 2 back recovers the function's own output.
    """
    notes = tuple(str(n) for n in (row.get("notes") or ()))
    score = int(row.get("score") or 0)
    if _CHOPPY_NOTE in notes:
        index = len(notes) - 1 - notes[::-1].index(_CHOPPY_NOTE)
        notes = notes[:index] + notes[index + 1:]
        score += _CHOPPY_PENALTY
    return score, notes


def _census(row: dict, result: dict, census: BranchCensus) -> None:
    """Count the branches whose firing is visible in the produced notes.

    Notes are the score engine's own audit trail — every branch that adds or
    subtracts appends one — so counting notes counts branches, without
    re-deriving any condition here. The `rr_*` notes are folded into one bucket
    per band since their text carries the value.
    """
    census.denominator += 1
    for note in (result.get("notes") or ()):
        name = str(note)
        if name.startswith("rr_"):
            name = "rr_band_note"
        census.hit(name)

    # Fields that are recorded but read by no scoring branch, plus the two
    # pre-scoring gates. Counted separately because "how often is this true" is
    # the EXP-0 question and it is not answerable from notes.
    for name, value in (("field_anchor_override_true", row.get("anchor_override")),
                        ("field_veto_active_true", row.get("veto_active")),
                        ("field_entry_ready_true", row.get("entry_ready"))):
        census.observe(name)
        if value is True:
            census.hit(name)


#: Every note `score_signal` can append, so a zero count is *reported* rather
#: than silently missing from the census. Derived by reading scoring.py:136-356.
_WATCHED_NOTES = (
    "weak_volume",
    "weekly_aligned", "d1_aligned", "h4_aligned", "discount", "premium",
    "sweep", "weak_sweep", "fvg", "displacement", "fresh_ob", "no_ob",
    "target_clear", "session_active", "htf_context_clean",
    "expansion", "squeeze", "neutral_penalty",
    "rr_band_note",
    "momentum_weakening",
    "m5_continuation_confirm", "m5_choch", "m5_watch_confirm",
)


def _rows(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def rescore_ledger(
    run_dir: Path | str, *, max_mismatches: int = 50
) -> IdentityReport:
    """Re-score every ledger row and compare against what was recorded.

    Read-only: opens exactly one file and writes nothing. `max_mismatches` caps
    retained *detail*; `mismatch_count` is exact regardless, because a report
    that under-counts its own failures is worse than a slow one.
    """
    from v2.analysis.scoring import score_signal

    path = Path(run_dir) / LEDGER_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"no {LEDGER_FILENAME} in {run_dir}; identity is checked against a "
            f"finished run's ledger, which is the golden file")

    report = IdentityReport(run_id=None)
    for note in _WATCHED_NOTES:
        report.census.observe(note)

    for row in _rows(path):
        report.rows += 1
        if report.run_id is None:
            report.run_id = row.get("run_id")
        version = row.get("strategy_version")
        if version is not None:
            report.strategy_versions.add(str(version))

        exit_taken = _pre_scoring_exit(row)
        if exit_taken is not None:
            report.skipped += 1
            report.census.exits[exit_taken] = (
                report.census.exits.get(exit_taken, 0) + 1)
            continue

        m5_choch = row.get("m5_choch")
        m5_setup = None if m5_choch is None else {"choch_confirmed": m5_choch}
        result = score_signal(
            _htf_map(row), str(row.get("regime") or ""), _h1_setup(row), m5_setup)

        report.compared += 1
        _census(row, result, report.census)

        expected_score, expected_notes = _expected(row)
        recomputed_notes = tuple(str(n) for n in (result.get("notes") or ()))
        recorded_rr = row.get("rr")
        recomputed_rr = result.get("rr")

        problems: list[tuple[str, Any, Any]] = []
        if int(result.get("score") or 0) != expected_score:
            problems.append(("score", expected_score, result.get("score")))
        if str(result.get("decision") or "") != str(row.get("decision") or ""):
            problems.append(("decision", row.get("decision"),
                             result.get("decision")))
        if recomputed_notes != expected_notes:
            problems.append(("notes", list(expected_notes),
                             list(recomputed_notes)))
        if recorded_rr != recomputed_rr:
            problems.append(("rr", recorded_rr, recomputed_rr))

        if not problems:
            report.matched += 1
            continue

        report.mismatch_count += len(problems)
        for name, recorded, recomputed in problems:
            if len(report.mismatches) < max_mismatches:
                report.mismatches.append(RowVerdict(
                    symbol=str(row.get("symbol") or ""),
                    h1_last_ts=float(row.get("h1_last_ts") or 0.0),
                    cycle_index=row.get("cycle_index"),
                    field_name=name, recorded=recorded, recomputed=recomputed))

    return report
