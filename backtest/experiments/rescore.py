"""Score decomposition and counterfactual re-scoring, without running the scorer.

Phase A / A1-A3. The question these experiments ask is "what happens to scores and
decision tiers if this component is removed or reweighted", and the honest way to
ask it is arithmetic rather than a code change.

## How it works, and why it needs no `v2` import

`score_signal` is a **sum of independent terms, each of which appends exactly one
note** (v2/analysis/scoring.py:136-356). So a recorded row's `notes` list *is* the
list of terms that fired, and the recorded `score` is their total. Removing a
component means subtracting its weight; reweighting means substituting one.

That makes this module a pure reader of the ledger: no strategy import, no scoring
behavior touched, nothing to keep in sync beyond one table.

## The table is verified, not trusted

`WEIGHTS` is a reading of the source, and a misread would silently corrupt every
result here. So every row is checked: the weights of its notes must sum to its
recorded score. `Decomposition.verified` is that check, `census` counts failures,
and a non-zero count invalidates the census rather than being reported alongside
it. The check is what turns a hand-copied table into a measurement.

Two adjustments make the identity hold exactly:

- **The choppy penalty** (-2, note `choppy_penalty`) is applied after
  `score_signal` returns, and the decision tier is chosen *before* it. It is
  therefore reversed out before any tier is recomputed - a V1 quirk, preserved
  because the recorded tiers were produced under it.
- **`rr_*` notes carry their value in the text.** The band gives the weight:
  >=3.0 -> +2, >=2.0 -> +1, >=1.5 -> 0 (a note with no score, emitted for the
  audit trail).

## Three classes of "dead", and why the distinction decides everything

The Phase 3 census reported twelve components as carrying no information. They do
not behave the same way under removal:

| Class | Example | Removal effect |
|---|---|---|
| **never fires** | `discount` 0/373,723 | score identical, provably |
| **always fires** | `weekly_aligned` 373,720/373,723 | score shifts *uniformly*; tiers move unless thresholds are shifted to match |
| **measured uninformative** | `sweep`, p_adj 1.0000 | scores genuinely change |

An earlier version of this programme's spec claimed all of them were bit-neutral.
Only the first class is. A constant term is not free to delete: removing
`weekly_aligned` subtracts 2 from essentially every score and pushes rows down a
tier. `compensating_shift` finds the threshold adjustment that cancels it, which
is what makes the simplification safe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = [
    "CHOPPY_NOTE",
    "CHOPPY_PENALTY",
    "EARLY_EXIT_REASONS",
    "NEVER_FIRES",
    "ALWAYS_FIRES",
    "UNINFORMATIVE",
    "Counterfactual",
    "Decomposition",
    "RescoreCensus",
    "WEIGHTS",
    "census",
    "decide",
    "decompose",
]

CHOPPY_NOTE = "choppy_penalty"
CHOPPY_PENALTY = 2

#: `score_signal`'s own early returns. These rows never reached the tier logic, so
#: their decision is fixed by the reason and must not be recomputed from a score.
EARLY_EXIT_REASONS = frozenset({
    "missing_input", "missing_direction", "no_continuation_displacement",
    "weak_watch_only_context", "bad_rr",
})

#: note -> weight, per entry mode. Read from v2/analysis/scoring.py:136-356.
#: `rr_*` is handled separately because its weight is encoded in the note text.
_SHARED: dict[str, int] = {
    # HTF alignment (scoring.py:178-204)
    "weekly_aligned": 2, "d1_aligned": 2, "h4_aligned": 1,
    "discount": 2, "premium": 2,
    # structure quality (253-262)
    "target_clear": 1, "session_active": 1, "htf_context_clean": 1,
    # regime engine (264-273)
    "expansion": 1, "squeeze": 1, "neutral_penalty": -2,
    # momentum quality (313-316)
    "momentum_weakening": -1,
}

WEIGHTS: dict[str, dict[str, int]] = {
    "CONTINUATION": {
        **_SHARED,
        "weak_volume": -2,                 # mode gate (163-165)
        "sweep": 1, "fvg": 1, "displacement": 4, "fresh_ob": 1,   # (207-219)
        "m5_continuation_confirm": 1,      # (320-322)
    },
    "RETRACEMENT": {
        **_SHARED,
        "sweep": 2, "weak_sweep": -1, "fvg": 1, "displacement": 1,
        "fresh_ob": 2, "no_ob": -1,        # (220-238)
        "m5_choch": 2,                     # (323-325)
    },
    "WATCH_ONLY": {
        **_SHARED,
        "sweep": 1, "fvg": 1, "displacement": 1, "fresh_ob": 1,   # (239-251)
        "m5_watch_confirm": 1,             # (326-328)
    },
}

#: Zero occurrences in 373,723 scored rows - removal is provably score-neutral.
NEVER_FIRES = ("discount", "premium", "neutral_penalty", "no_ob",
               "m5_watch_confirm")
#: Fires on essentially every row - removal shifts every score and needs a
#: compensating threshold adjustment to stay tier-neutral.
ALWAYS_FIRES = ("weekly_aligned", "fresh_ob", "displacement")
#: Measured and not informative; removal genuinely changes scores.
UNINFORMATIVE = ("sweep", "target_clear", "session_active", "htf_context_clean")


def _rr_weight(note: str, rr: Optional[float]) -> Optional[int]:
    """Weight of an `rr_X.X` note (scoring.py:302-309).

    The band must come from the recorded `rr`, **not** from the note text. The note
    is formatted `f"rr_{rr:.1f}"`, which rounds: an rr of 2.98 prints as `rr_3.0`
    while the band test saw 2.98 and awarded +1, not +2. Reading the text instead
    of the value mis-weighted 378 of 16,297 rows in a 40k-row sample — caught by
    the verification identity, which is what it is for.

    `rr` is itself recorded as `round(rr, 2)`, which leaves the band edges
    ambiguous — a true 1.9996 is stored as 2.0 and would read as +1 when the engine
    awarded 0. That accounted for the last 89 of 16,297 rows, all at exactly 2.0 or
    3.0. So the value is recomputed from `entry_price`/`sl`/`tp`, which are stored
    unrounded, using `score_signal`'s own arithmetic (scoring.py:287-291); the
    rounded field is only the fallback.
    """
    if not note.startswith("rr_"):
        return None
    if rr is None:
        return None
    value = float(rr)
    if value >= 3.0:
        return 2
    if value >= 2.0:
        return 1
    return 0


def _exact_rr(row: dict) -> Optional[float]:
    """`reward / risk` from the unrounded levels, as `score_signal` computes it.

    Falls back to the rounded `rr` field when a level is missing. `and` on floats
    is deliberate: `score_signal` guards with `if entry and sl and tp`, so a level
    of 0.0 skips the RR engine entirely and must skip it here too.
    """
    entry, sl, tp = row.get("entry_price"), row.get("sl"), row.get("tp")
    if entry and sl and tp:
        risk = abs(float(entry) - float(sl))
        if risk > 0:
            return abs(float(tp) - float(entry)) / risk
    return row.get("rr")


def _weight(mode: str, note: str, rr: Optional[float]) -> Optional[int]:
    if note.startswith("rr_"):
        return _rr_weight(note, rr)
    return WEIGHTS.get(mode, {}).get(note)


# ---------------------------------------------------------------------------
# decomposition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decomposition:
    """One row's score, broken into the terms that produced it."""

    mode: str
    recorded_score: int
    raw_score: int                 # recorded, with the choppy penalty reversed
    choppy: bool
    notes: tuple[str, ...]         # raw notes, choppy removed
    contributions: dict[str, int]
    unknown_notes: tuple[str, ...]
    early_exit: bool

    @property
    def total(self) -> int:
        return sum(self.contributions.values())

    @property
    def verified(self) -> bool:
        """Do the note weights sum to the recorded score?

        An early-exit row scores 0 with no notes, so the identity holds trivially
        there and the flag stays meaningful for the rows that matter.
        """
        return not self.unknown_notes and self.total == self.raw_score


def decompose(row: dict) -> Decomposition:
    """Break a ledger row's score into per-note contributions."""
    mode = str(row.get("entry_mode") or "")
    recorded = int(row.get("score") or 0)
    notes = tuple(str(n) for n in (row.get("notes") or ()))

    choppy = CHOPPY_NOTE in notes
    if choppy:
        index = len(notes) - 1 - notes[::-1].index(CHOPPY_NOTE)
        notes = notes[:index] + notes[index + 1:]
    raw = recorded + (CHOPPY_PENALTY if choppy else 0)

    rr = _exact_rr(row)
    contributions: dict[str, int] = {}
    unknown: list[str] = []
    for note in notes:
        weight = _weight(mode, note, rr)
        if weight is None:
            unknown.append(note)
        else:
            key = "rr_band" if note.startswith("rr_") else note
            contributions[key] = contributions.get(key, 0) + weight

    return Decomposition(
        mode=mode, recorded_score=recorded, raw_score=raw, choppy=choppy,
        notes=notes, contributions=contributions,
        unknown_notes=tuple(unknown),
        early_exit=str(row.get("reason") or "") in EARLY_EXIT_REASONS,
    )


# ---------------------------------------------------------------------------
# decision tiers
# ---------------------------------------------------------------------------

def decide(mode: str, score: int, *, displacement: bool, shift: int = 0) -> str:
    """The decision tier for a score (scoring.py:330-355), thresholds shiftable.

    `shift` lowers every threshold, which is how a constant term's removal is
    compensated: delete a term worth 2 that always fires, shift by 2, and every
    row that fired it keeps its tier exactly.
    """
    if mode == "CONTINUATION":
        if score >= 14 - shift:
            return "A+"
        if score >= 11 - shift:
            return "VALID"
        if score >= 8 - shift:
            return "WATCH"
        return "SKIP"
    if mode == "RETRACEMENT":
        if score >= 15 - shift:
            return "A+" if displacement else "VALID"
        if score >= 12 - shift:
            return "VALID"
        if score >= 9 - shift:
            return "WATCH"
        return "SKIP"
    if score >= 12 - shift:
        return "WATCH"
    return "SKIP"


# ---------------------------------------------------------------------------
# counterfactuals
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Counterfactual:
    """A named change to the score's terms. Nothing here mutates the strategy."""

    name: str
    #: Notes whose contribution is subtracted.
    removed: tuple[str, ...] = ()
    #: note -> new weight, replacing the table's value (sign fixes live here).
    reweighted: dict[str, int] = field(default_factory=dict)
    #: Threshold shift applied when recomputing tiers.
    shift: int = 0

    def score_of(self, decomposition: Decomposition) -> int:
        score = decomposition.raw_score
        for note in self.removed:
            key = "rr_band" if note.startswith("rr_") else note
            score -= decomposition.contributions.get(key, 0)
        for note, weight in self.reweighted.items():
            if note in decomposition.contributions:
                score += weight - decomposition.contributions[note]
        return score

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "removed": list(self.removed),
                "reweighted": dict(sorted(self.reweighted.items())),
                "threshold_shift": self.shift}


@dataclass
class RescoreCensus:
    """What a counterfactual does to scores and tiers over a whole ledger."""

    counterfactual: Counterfactual
    rows: int = 0
    scored: int = 0
    early_exit: int = 0
    unverified: int = 0
    unknown_notes: dict[str, int] = field(default_factory=dict)
    note_fired: dict[str, int] = field(default_factory=dict)
    score_deltas: dict[int, int] = field(default_factory=dict)
    tier_changes: int = 0
    transitions: dict[str, int] = field(default_factory=dict)
    #: tier changes at each candidate threshold shift, for compensation search.
    changes_by_shift: dict[int, int] = field(default_factory=dict)

    @property
    def score_neutral(self) -> bool:
        return set(self.score_deltas) <= {0}

    @property
    def tier_neutral(self) -> bool:
        return self.tier_changes == 0

    @property
    def compensating_shift(self) -> Optional[int]:
        """The shift that minimises tier changes; None when nothing was tried."""
        if not self.changes_by_shift:
            return None
        return min(self.changes_by_shift,
                   key=lambda s: (self.changes_by_shift[s], abs(s)))

    def to_dict(self) -> dict[str, Any]:
        best = self.compensating_shift
        return {
            "source": "derived",
            "analysis": "score_counterfactual_census",
            "is_replay": False,
            "counterfactual": self.counterfactual.to_dict(),
            "rows": self.rows,
            "rows_scored": self.scored,
            "rows_early_exit": self.early_exit,
            "verification": {
                "unverified_rows": self.unverified,
                "unknown_notes": dict(sorted(self.unknown_notes.items())),
                "meaning": "note weights must sum to the recorded score; a "
                           "non-zero count invalidates this census rather than "
                           "qualifying it",
            },
            "note_fired": dict(sorted(self.note_fired.items())),
            "score_delta_histogram": {str(k): v for k, v
                                      in sorted(self.score_deltas.items())},
            "score_neutral": self.score_neutral,
            "tier_changes": self.tier_changes,
            "tier_neutral": self.tier_neutral,
            "tier_transitions": dict(sorted(self.transitions.items())),
            "compensation": {
                "tier_changes_by_shift": {str(k): v for k, v
                                          in sorted(self.changes_by_shift.items())},
                "best_shift": best,
                "residual_tier_changes": (None if best is None
                                          else self.changes_by_shift[best]),
                "meaning": "a term that always fires shifts every score; lowering "
                           "the tier thresholds by the same amount cancels it. "
                           "Residual changes are the rows where the term did not "
                           "fire.",
            },
        }


def _rows(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def census(
    run_dir: Path | str,
    counterfactual: Counterfactual,
    *,
    ledger_filename: str = "signal_ledger.jsonl",
    shifts: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7),
) -> RescoreCensus:
    """Stream a run's ledger and measure a counterfactual. Reads only."""
    path = Path(run_dir) / ledger_filename
    if not path.exists():
        raise FileNotFoundError(f"no {ledger_filename} in {run_dir}")

    report = RescoreCensus(counterfactual=counterfactual)
    watched = set(counterfactual.removed) | set(counterfactual.reweighted)
    for note in watched:
        report.note_fired.setdefault(note, 0)

    for row in _rows(path):
        report.rows += 1
        decomposition = decompose(row)
        if not decomposition.notes and decomposition.recorded_score == 0:
            # No HTF context / no direction / veto / not-ready: the row never
            # reached the score engine, so there is nothing to re-score.
            continue

        report.scored += 1
        if not decomposition.verified:
            report.unverified += 1
            for note in decomposition.unknown_notes:
                report.unknown_notes[note] = (
                    report.unknown_notes.get(note, 0) + 1)
            continue
        if decomposition.early_exit:
            report.early_exit += 1
            continue

        for note in watched:
            key = "rr_band" if note.startswith("rr_") else note
            if key in decomposition.contributions:
                report.note_fired[note] += 1

        new_score = counterfactual.score_of(decomposition)
        delta = new_score - decomposition.raw_score
        report.score_deltas[delta] = report.score_deltas.get(delta, 0) + 1

        displacement = bool(row.get("displacement"))
        before = decide(decomposition.mode, decomposition.raw_score,
                        displacement=displacement)
        after = decide(decomposition.mode, new_score,
                       displacement=displacement,
                       shift=counterfactual.shift)
        if before != after:
            report.tier_changes += 1
            key = f"{before}->{after}"
            report.transitions[key] = report.transitions.get(key, 0) + 1

        for shift in shifts:
            shifted = decide(decomposition.mode, new_score,
                             displacement=displacement, shift=shift)
            if shifted != before:
                report.changes_by_shift[shift] = (
                    report.changes_by_shift.get(shift, 0) + 1)
            else:
                report.changes_by_shift.setdefault(shift, 0)

    return report