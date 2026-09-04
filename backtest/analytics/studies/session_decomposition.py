"""Session decomposition — realized R by mode x direction x session.

Opt-in and unregistered: absent from `studies.STUDIES`, absent from the CLI.

## What this answers

    Within each fixed entry_mode x direction cell, how did realized R
    distribute across the five UTC session bands?

Session comparison **inside one fixed cell** is the whole purpose of this
module and is descriptive. Comparison *across* cells is not available here for
the same reason it is not available in `performance_census`: the two entry
modes are selected into the scored population at very different rates (2.5%
against 21.5% attrition), so a cross-mode difference is unidentified under
every convention. `_guard_row` refuses any row not scoped to exactly one
(cell, session) pair, and the rendered output is screened against a
comparative word list.

## What this does not answer

Which session is preferable. No ranking, no best/worst panel, no candidate
list, no significance claim, no p-value, no pass/fail, no recommendation.
Sessions are emitted in clock order — a property of the day, not of any
metric — and every row is `DESCRIPTIVE`.

## Session definition

Reused verbatim from `backtest.metrics.segments.SESSIONS`, which is the
repository's only definition:

    ASIA               [00, 07)
    LONDON             [07, 12)
    LONDON_NY_OVERLAP  [12, 16)
    NEWYORK            [16, 21)
    OFF                [21, 24)

Half-open UTC hour bands on `record.boundary` (instantiation), matching
`performance_census.TIME_BASIS`. The bands are exhaustive over 0-23 and
mutually exclusive, so every evaluated record lands in exactly one — asserted
at run time by `_session_accounting` rather than assumed.

`session_active` is a different thing entirely: it is the strategy's own
SNIPER gate (Mon-Fri 07:00-22:00 UTC). It is neither a substitute for these
bands nor reconciled with them, and it is not used here. `session` is closed
inside `performance_census`'s *contrast family* because it is nested in that
gate, which made it redundant **as a ninth contrast**; it remains a
stratification axis exactly like `symbol` and `month`, which is the role it
plays here.

## Shares are scoped to the parent cell

`share_of_parent_gross_positive` divides a session's win contribution by its
**own cell's** gross positive, not by the whole book. Within a cell the five
sessions therefore sum to exactly 1.0, and the figure answers "where inside
this cell did the wins land" rather than mixing four differently-selected
populations into one denominator. The trade-level convention that makes this
invariant to partition refinement is inherited unchanged from
`performance_census._gross`.

## Persistence is quarterly

Slicing a cell's ~40 months by five sessions leaves monthly buckets below the
frozen `MIN_SIDE_N` floor in 17 of 20 combinations; quarterly buckets fail in
4 of 20. Persistence is therefore reported on quarters, at the **existing**
`performance_census.MIN_SIDE_N` — no second floor is introduced. Unusable
quarters are named and counted in the output, never dropped in silence.
Monthly figures are still emitted, labelled raw accounting rather than
persistence, because a bucket that fails the floor is a count and not a trend.

## Reuse

Every metric comes from `performance_census`, which in turn takes its
definitions from `backtest.metrics`. Nothing is recomputed here: this module
adds one grouping axis and the arithmetic that scopes shares to a parent cell.

## Purity

Reader-only. No RNG, no import of `backtest.replay`, no `v2` or `src`
reference, and no write outside the caller's output directory.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional, Sequence

from backtest.analytics.studies.performance_census import (
    CELLS,
    EVIDENCE_CLASS,
    FEE_BAND_BP,
    LEG_CONVENTIONS,
    MIN_SIDE_N,
    PROHIBITED_GROUPING_KEYS,
    PROHIBITED_WORDS,
    TIME_BASIS,
    Book,
    Row,
    _concentration_side,
    _fill_selection,
    _friction,
    _gross,
    _gross_record,
    _herfindahl,
    _mean,
    _month_of,
    guard_grouping_key,
    load,
)
from backtest.analytics.studies.performance_census import (
    specification_hash as parent_specification_hash,
)
from backtest.metrics.segments import SESSIONS, _utc

__all__ = [
    "CELLS",
    "PERSISTENCE_BUCKET",
    "SESSIONS",
    "SessionDecompositionError",
    "analyse",
    "csv_rows",
    "render",
    "specification_hash",
    "write_decomposition",
]

DECOMPOSITION_DIRNAME = "session_decomposition"
DECOMPOSITION_ENGINE_VERSION = 1

#: Quarters, not months. The choice is recorded in `specification_hash` because
#: it changes which buckets clear the floor, and therefore what the persistence
#: block means.
PERSISTENCE_BUCKET = "quarter"

#: Clock order, taken from `SESSIONS` itself. Deliberately not a metric order:
#: sorting sessions by any measured quantity would be a ranking.
SESSION_NAMES: tuple[str, ...] = tuple(name for name, _s, _e in SESSIONS)

#: Symbol concentration depths, matching the parent census.
_SYMBOL_TOP_K = (1, 5, 10, 25)

#: Governance prose, as constants rather than literals inside `analyse`.
#:
#: Extracted so a fixture-based test exercises the same strings the real run
#: emits. The first version of this module put them inline, and the word-list
#: screen — which is strict on rendered output by design — only tripped on the
#: slow official-bundle test. Note the phrasing avoids naming the prohibited
#: words themselves: `PROHIBITED_WORDS` is checked against rendered output, so
#: prose that says "no recommendation is emitted" would fail its own screen.
ANSWERS_TEXT = (
    "within each fixed entry_mode x direction cell, how realized R "
    "distributed across the five UTC session bands")

DOES_NOT_ANSWER_TEXT = (
    "which session, mode, or direction is preferable. No ranking, candidate "
    "list, significance claim, p-value, or pass/fail is emitted, and no course "
    "of action is proposed")

SCOPE_TEXT = (
    "one in-sample bundle covering roughly 27% of the strategy's decisions. "
    "No causal claim is available: session is confounded with everything else "
    "that varies by hour")

LEGAL_COMPARISON_TEXT = "sessions within one fixed cell, descriptively"

SESSION_ORDERING_TEXT = "clock order, never a metric order"

PROHIBITED_STATEMENTS = (
    "any ordering or ranking of the five sessions, within a cell or across cells",
    "any claim that one session, mode, or direction is preferable to another",
    "any comparison of two different mode x direction cells, in either basis",
    "any use of WIN/LOSS as a grouping key rather than as the accounting split",
    "any forward expectation, parameter value, filter, or strategy change",
)

LIMITATIONS = (
    "One in-sample bundle. Session bands are a reporting convention imposed on "
    "a market that trades continuously; nothing in the strategy reads them.",
    "Coverage is roughly 27% of decisions: 82,954 shadow records sit outside "
    "every population here.",
    "The counterfactual R of an unfilled decision is unobservable, so fill "
    "rates are disclosed rather than corrected for.",
    "Costs are imputed. The account's fee tier is not in the bundle, so "
    "friction is arithmetic across a declared band.",
    "Session is confounded with everything else that varies by hour of day — "
    "liquidity, spread, which symbols are active, and the SNIPER gate itself. "
    "A session difference is not evidence about the session.",
    "Slicing a cell five ways divides its effective sample five ways. Measured "
    "variance inflation on this bundle is about 3.97x, so no inference at this "
    "granularity is available and none is offered.",
    "Interaction reversal is undetectable here: no intersection beyond "
    "cell x session is computed.",
    "No causal claim is available. Session assignment is observational and "
    "jointly determined with every other property of a decision.",
)


class SessionDecompositionError(Exception):
    """The decomposition cannot be assembled, or a frozen rule was violated."""


def specification_hash() -> str:
    """A digest over this module's frozen constants and its parent's.

    The parent hash is folded in so that a change to the census specification —
    the cells, the floor, the time basis — moves this hash too. The two
    artifacts describe the same book and must not drift apart silently.
    """
    payload = {
        "engine_version": DECOMPOSITION_ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "sessions": [list(entry) for entry in SESSIONS],
        "cells": [list(cell) for cell in CELLS],
        "min_side_n": MIN_SIDE_N,
        "persistence_bucket": PERSISTENCE_BUCKET,
        "time_basis": TIME_BASIS,
        "fee_band_bp": list(FEE_BAND_BP),
        "leg_conventions": [list(leg) for leg in LEG_CONVENTIONS],
        "symbol_top_k": list(_SYMBOL_TOP_K),
        "prohibited_words": list(PROHIBITED_WORDS),
        "prohibited_grouping_keys": sorted(PROHIBITED_GROUPING_KEYS),
        "parent_specification_hash": parent_specification_hash(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

def _session_of(row: Row) -> Optional[str]:
    """The session band holding this decision's instantiation hour.

    Applies `SESSIONS` directly rather than restating its edges. `None` would
    mean the bands stopped covering the clock, which `_session_accounting`
    turns into a hard failure instead of a silent bucket.
    """
    if row.record.boundary is None:
        return None
    hour = _utc(int(row.record.boundary)).hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return None


def _quarter_of(row: Row) -> str:
    moment = datetime.fromtimestamp(int(row.record.boundary), tz=timezone.utc)
    return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"


def _cell_of(row: Row) -> Optional[str]:
    return row.cell


def _guard_row(row: dict[str, Any]) -> dict[str, Any]:
    """Refuse any row not scoped to exactly one (cell, session).

    This is the mechanism behind the cross-mode prohibition. A row spanning two
    modes would need a cell outside the frozen four; a row spanning two
    sessions would need a session outside the frozen five. Neither is
    constructible.
    """
    cells = {name for name, _mode, _direction in CELLS}
    if row.get("cell") not in cells:
        raise SessionDecompositionError(
            f"row cell {row.get('cell')!r} is not one of the four frozen "
            f"single-mode cells; a row spanning two modes cannot be emitted")
    if row.get("session") not in SESSION_NAMES:
        raise SessionDecompositionError(
            f"row session {row.get('session')!r} is not one of the five frozen "
            f"session bands")
    return row


# ---------------------------------------------------------------------------
# Block 0 — session accounting (exhaustiveness, asserted not assumed)
# ---------------------------------------------------------------------------

def _session_accounting(book: Book) -> dict[str, Any]:
    """Every evaluated decision lands in exactly one session, or this fails."""
    hours_covered: set[int] = set()
    for _name, start, end in SESSIONS:
        span = set(range(start, end))
        if hours_covered & span:
            raise SessionDecompositionError(
                f"session bands overlap at hours {sorted(hours_covered & span)}")
        hours_covered |= span
    if hours_covered != set(range(24)):
        raise SessionDecompositionError(
            f"session bands do not cover the clock; missing "
            f"{sorted(set(range(24)) - hours_covered)}")

    per_session: dict[str, int] = {name: 0 for name in SESSION_NAMES}
    unassigned = 0
    for entry in book.resolved:
        session = _session_of(entry)
        if session is None:
            unassigned += 1
        else:
            per_session[session] += 1
    if unassigned:
        raise SessionDecompositionError(
            f"{unassigned} resolved decisions could not be assigned to a "
            f"session band")
    total = sum(per_session.values())
    if total != len(book.resolved):
        raise SessionDecompositionError(
            f"session counts sum to {total}, not {len(book.resolved)}")

    return {
        "denominators": book.denominators,
        "bands": [{"name": name, "start_hour": start, "end_hour": end,
                   "convention": "half-open [start, end) UTC"}
                  for name, start, end in SESSIONS],
        "time_basis": TIME_BASIS,
        "resolved_decisions": len(book.resolved),
        "per_session_resolved": per_session,
        "unassigned": unassigned,
        "identity": {
            "bands_cover_the_clock": True,
            "bands_are_disjoint": True,
            "counts_sum_to_resolved": total == len(book.resolved),
        },
        "session_active_note": (
            "these bands are a reporting grouping of the UTC clock. The "
            "strategy's own gate is the SNIPER window (Mon-Fri 07:00-22:00 "
            "UTC), reported separately as session_active and deliberately not "
            "reconciled with these edges"),
    }


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    """One (cell, session) group, in both bases."""

    cell: str
    session: str
    resolved: tuple[Row, ...]

    @property
    def scored(self) -> tuple[Row, ...]:
        return tuple(entry for entry in self.resolved if entry.filled)

    @property
    def unfilled(self) -> tuple[Row, ...]:
        return tuple(entry for entry in self.resolved if not entry.filled)

    @property
    def values(self) -> list[float]:
        return [float(entry.record.realized_r) for entry in self.scored]


def _grid(book: Book) -> dict[str, dict[str, Segment]]:
    """The fixed 4 x 5 grid. Every combination exists, empty or not."""
    buckets: dict[tuple[str, str], list[Row]] = {}
    for entry in book.resolved:
        cell, session = _cell_of(entry), _session_of(entry)
        if cell is None or session is None:
            continue
        buckets.setdefault((cell, session), []).append(entry)

    grid: dict[str, dict[str, Segment]] = {}
    for cell_name, _mode, _direction in CELLS:
        grid[cell_name] = {}
        for session in SESSION_NAMES:
            grid[cell_name][session] = Segment(
                cell=cell_name, session=session,
                resolved=tuple(buckets.get((cell_name, session), [])))
    return grid


# ---------------------------------------------------------------------------
# Block 3 — WIN / LOSS accounting
# ---------------------------------------------------------------------------

def _win_loss(segment: Segment, denominators: dict[str, int]) -> dict[str, Any]:
    """Outcome counts. An accounting partition, never a grouping key.

    `state` is the metrics engine's own WIN/LOSS verdict; the sign split beside
    it is what the gross ledger uses. They are reported together so the small
    residue — a trade that resolved at exactly 0R — is visible rather than
    making the two views disagree for no stated reason.
    """
    scored = segment.scored
    wins = sum(1 for entry in scored
               if getattr(entry.record, "state", None) == "WIN")
    losses = sum(1 for entry in scored
                 if getattr(entry.record, "state", None) == "LOSS")
    values = segment.values
    return {
        "denominators": dict(denominators),
        "population": "fill basis",
        "scored_n": len(scored),
        "win_count": wins,
        "lose_count": losses,
        "win_rate": (wins / len(scored)) if scored else None,
        "positive_r_n": sum(1 for value in values if value > 0),
        "negative_r_n": sum(1 for value in values if value < 0),
        "zero_r_n": sum(1 for value in values if value == 0),
        "note": ("WIN/LOSS is an outcome partition reported as accounting. It "
                 "is closed as a conditioning key"),
    }


# ---------------------------------------------------------------------------
# Block 4 — concentration, within cell x session
# ---------------------------------------------------------------------------

def _concentration(segment: Segment, denominators: dict[str, int],
                   ) -> dict[str, Any]:
    """Symbol concentration computed inside this segment, never pooled."""
    wins: dict[str, float] = {}
    losses: dict[str, float] = {}
    for entry in segment.scored:
        value = float(entry.record.realized_r)
        symbol = entry.record.symbol
        if value > 0:
            wins[symbol] = wins.get(symbol, 0.0) + value
        elif value < 0:
            losses[symbol] = losses.get(symbol, 0.0) - value

    symbols = {entry.record.symbol for entry in segment.scored}
    return {
        "denominators": dict(denominators),
        "scope": "within this cell x session; never pooled across cells",
        "symbols_present": len(symbols),
        "symbols_at_min_side_n": sum(
            1 for symbol in symbols
            if sum(1 for entry in segment.scored
                   if entry.record.symbol == symbol) >= MIN_SIDE_N),
        "positive": {"by_symbol": _concentration_side(wins, _SYMBOL_TOP_K)},
        "negative": {"by_symbol": _concentration_side(losses, _SYMBOL_TOP_K)},
    }


# ---------------------------------------------------------------------------
# Block 5 — quarterly persistence
# ---------------------------------------------------------------------------

def _persistence(segment: Segment, denominators: dict[str, int],
                 ) -> dict[str, Any]:
    """Quarterly sign counts at the frozen floor, with failures named.

    Monthly buckets fall below `MIN_SIDE_N` in most cell x session
    combinations, so quarters carry the persistence reading. The monthly view
    is still emitted below it as raw accounting — counts and net R — and is
    explicitly not a persistence statement.
    """
    by_quarter: dict[str, list[float]] = {}
    for entry in segment.scored:
        by_quarter.setdefault(_quarter_of(entry), []).append(
            float(entry.record.realized_r))

    ordered = sorted(by_quarter.items())
    usable = [(key, values) for key, values in ordered
              if len(values) >= MIN_SIDE_N]
    unusable = [{"quarter": key, "n": len(values), "net": float(sum(values)),
                 "marker": "BELOW_MIN_SIDE_N"}
                for key, values in ordered if len(values) < MIN_SIDE_N]

    positives = sum(1 for _key, values in usable if sum(values) > 0)
    longest = current = 0
    for _key, values in usable:
        if sum(values) <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0

    by_month: dict[str, list[float]] = {}
    for entry in segment.scored:
        by_month.setdefault(_month_of(entry), []).append(
            float(entry.record.realized_r))

    return {
        "denominators": dict(denominators),
        "bucket": PERSISTENCE_BUCKET,
        "floor": MIN_SIDE_N,
        "floor_note": (
            "the frozen performance_census floor, reused rather than replaced. "
            "Monthly buckets fail it in most cell x session combinations, "
            "which is why quarters carry the persistence reading"),
        "quarters_present": len(ordered),
        "quarters_usable": len(usable),
        "quarters_unusable": unusable,
        "quarters_positive": positives,
        "positive_quarter_share": (positives / len(usable)) if usable else None,
        "longest_negative_run_quarters": longest,
        "usable_detail": [{"quarter": key, "n": len(values),
                           "net": float(sum(values)),
                           "mean_r": _mean(values)}
                          for key, values in usable],
        "monthly_raw_accounting": {
            "note": ("raw counts and net R only. Months below the floor are "
                     "not a persistence reading and are not presented as one"),
            "months_present": len(by_month),
            "months_at_or_above_floor": sum(
                1 for values in by_month.values() if len(values) >= MIN_SIDE_N),
            "months": [{"month": key, "n": len(values),
                        "net": float(sum(values)),
                        "below_floor": len(values) < MIN_SIDE_N}
                       for key, values in sorted(by_month.items())],
        },
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _segment_record(segment: Segment, cell_win: float, cell_loss: float,
                    denominators: dict[str, int]) -> dict[str, Any]:
    """One (cell, session) row, in full."""
    values = segment.values
    gross = _gross_record(values, cell_win, cell_loss, denominators)
    # `_gross_record` names its shares against whatever denominator it is
    # given. Here that denominator is the parent cell, so the keys are renamed
    # to say so and the book-scoped names are not carried forward.
    gross["share_of_parent_gross_positive"] = gross.pop(
        "share_of_gross_positive")
    gross["share_of_parent_gross_negative"] = gross.pop(
        "share_of_gross_negative")
    gross["share_scope"] = (
        "denominator is this session's own parent cell, so the five sessions "
        "within a cell sum to 1.0. It is not a share of the whole book")

    fill = _fill_selection(list(segment.resolved), denominators)
    row = {
        "cell": segment.cell,
        "session": segment.session,
        "evidence_class": EVIDENCE_CLASS,
        "denominators": dict(denominators),
        "evaluated_n": len(segment.resolved),
        "filled_n": len(segment.scored),
        "unfilled_n": len(segment.unfilled),
        "fill_rate": fill["fill_rate"],
        "fill_selection": fill,
        "mean_r_fill_basis": _mean(values),
        "median_r": (float(median(values)) if values else None),
        "total_r": float(sum(values)),
        "gross": gross,
        "win_loss": _win_loss(segment, denominators),
        "concentration": _concentration(segment, denominators),
        "persistence": _persistence(segment, denominators),
        "friction": _friction(list(segment.resolved), denominators),
        "below_min_side_n": len(segment.scored) < MIN_SIDE_N,
        "usability_markers": (
            ["BELOW_MIN_SIDE_N"] if len(segment.scored) < MIN_SIDE_N else []),
    }
    return _guard_row(row)


def analyse(run_dir: Path | str) -> dict[str, Any]:
    """The whole decomposition as a plain dictionary. Pure: no RNG, no writes."""
    book = load(run_dir)
    denominators = book.denominators
    accounting = _session_accounting(book)
    grid = _grid(book)

    cells_out: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for cell_name, _mode, _direction in CELLS:
        cell_rows = [entry for entry in book.scored if entry.cell == cell_name]
        cell_values = [float(entry.record.realized_r) for entry in cell_rows]
        cell_win, cell_loss = _gross(cell_values)

        session_rows = []
        for session in SESSION_NAMES:
            record = _segment_record(
                grid[cell_name][session], cell_win, cell_loss, denominators)
            session_rows.append(record)
            rows.append(record)

        share_positive = sum(
            entry["gross"]["share_of_parent_gross_positive"] or 0.0
            for entry in session_rows)
        share_negative = sum(
            entry["gross"]["share_of_parent_gross_negative"] or 0.0
            for entry in session_rows)
        cells_out[cell_name] = {
            "denominators": denominators,
            "cell_scored_n": len(cell_rows),
            "cell_gross_positive": cell_win,
            "cell_gross_negative": cell_loss,
            "cell_net": cell_win + cell_loss,
            "sessions": session_rows,
            "closure": {
                "session_shares_sum_to_one_positive":
                    abs(share_positive - 1.0) < 1e-9 if cell_win else None,
                "session_shares_sum_to_one_negative":
                    abs(share_negative - 1.0) < 1e-9 if cell_loss else None,
                "session_n_sums_to_cell":
                    sum(entry["filled_n"] for entry in session_rows)
                    == len(cell_rows),
            },
        }

    return {
        "study": "session_decomposition",
        "engine_version": DECOMPOSITION_ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "specification_hash": specification_hash(),
        "parent_specification_hash": parent_specification_hash(),
        "governance": {
            "evidence_class": EVIDENCE_CLASS,
            "answers": ANSWERS_TEXT,
            "does_not_answer": DOES_NOT_ANSWER_TEXT,
            "scope": SCOPE_TEXT,
            "legal_comparison": LEGAL_COMPARISON_TEXT,
            "prohibited_statements": list(PROHIBITED_STATEMENTS),
            "prohibited_words_in_output": list(PROHIBITED_WORDS),
            "prohibited_grouping_keys": sorted(PROHIBITED_GROUPING_KEYS),
            "min_side_n": MIN_SIDE_N,
            "persistence_bucket": PERSISTENCE_BUCKET,
            "time_basis": TIME_BASIS,
            "fee_band_bp": list(FEE_BAND_BP),
            "leg_conventions": [label for label, _legs in LEG_CONVENTIONS],
            "session_ordering": SESSION_ORDERING_TEXT,
            "registered_in_studies": False,
        },
        "session_accounting": accounting,
        "overview": _overview(cells_out, denominators),
        "cells": cells_out,
        "rows": rows,
        "limitations": list(LIMITATIONS),
    }


def _overview(cells_out: dict[str, Any],
              denominators: dict[str, int]) -> dict[str, Any]:
    """The 4 x 5 matrix: n, fill rate, win rate, mean/median R, total R."""
    matrix = []
    for cell_name, block in cells_out.items():
        for entry in block["sessions"]:
            matrix.append({
                "cell": entry["cell"],
                "session": entry["session"],
                "evaluated_n": entry["evaluated_n"],
                "filled_n": entry["filled_n"],
                "fill_rate": entry["fill_rate"],
                "win_rate": entry["win_loss"]["win_rate"],
                "mean_r": entry["mean_r_fill_basis"],
                "median_r": entry["median_r"],
                "total_r": entry["total_r"],
                "evidence_class": EVIDENCE_CLASS,
                "usability_markers": entry["usability_markers"],
            })
    return {
        "denominators": denominators,
        "ordering": ("cells in frozen order, sessions in clock order; never "
                     "by any metric"),
        "rows": matrix,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.{places}f}"


def _denominator_line(denominators: dict[str, int]) -> str:
    return (f"> Denominators: "
            f"{denominators['shadow_records_excluded']:,} shadow records "
            f"excluded · "
            f"{denominators['unfilled_decisions_excluded_from_fill_basis']:,} "
            f"unfilled decisions excluded from the fill basis.\n\n")


def render(payload: dict[str, Any]) -> str:
    """Markdown. Screened against the prohibited word list by test."""
    governance = payload["governance"]
    out: list[str] = ["# Session decomposition\n\n"]
    out.append(f"> **Evidence class: {payload['evidence_class']}.** "
               f"{governance['scope']}\n\n")
    out.append(f"> Answers: {governance['answers']}\n\n")
    out.append(f"> Does not answer: {governance['does_not_answer']}\n\n")
    out.append(f"> Legal comparison: {governance['legal_comparison']}. "
               f"Session ordering: {governance['session_ordering']}.\n\n")
    out.append(f"> Specification hash `{payload['specification_hash']}` "
               f"(parent `{payload['parent_specification_hash']}`) · "
               f"MIN_SIDE_N {governance['min_side_n']} · persistence bucket "
               f"`{governance['persistence_bucket']}` · time basis "
               f"`{governance['time_basis']}`\n\n")

    # -- 1 · overview --------------------------------------------------------
    accounting = payload["session_accounting"]
    out.append("## 1 · Overview — 4 × 5 matrix\n\n")
    out.append(_denominator_line(payload["overview"]["denominators"]))
    out.append("| Session | UTC band |\n|---|---|\n")
    for band in accounting["bands"]:
        out.append(f"| `{band['name']}` | "
                   f"[{band['start_hour']:02d}, {band['end_hour']:02d}) |\n")
    out.append(f"\n> {accounting['session_active_note']}.\n\n")
    out.append("| Cell | Session | n eval | filled | fill rate | win rate | "
               "mean R | median R | total R |\n"
               "|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for entry in payload["overview"]["rows"]:
        marker = (" ⚠" if entry["usability_markers"] else "")
        out.append(f"| `{entry['cell']}` | `{entry['session']}`{marker} | "
                   f"{entry['evaluated_n']:,} | {entry['filled_n']:,} | "
                   f"{_num(entry['fill_rate'])} | {_num(entry['win_rate'])} | "
                   f"{_num(entry['mean_r'], 5)} | {_num(entry['median_r'])} | "
                   f"{_num(entry['total_r'], 1)} |\n")
    out.append("\n")

    # -- 2 · per-cell session tables ----------------------------------------
    out.append("## 2 · Per-cell session tables\n\n")
    for cell_name, block in payload["cells"].items():
        out.append(f"### `{cell_name}`\n\n")
        out.append(_denominator_line(block["denominators"]))
        out.append(f"Cell scored n {block['cell_scored_n']:,} · gross+ "
                   f"{_num(block['cell_gross_positive'], 1)} · gross− "
                   f"{_num(block['cell_gross_negative'], 1)} · net "
                   f"{_num(block['cell_net'], 1)}\n\n")
        out.append("| Session | n eval | filled | unfilled | fill rate | "
                   "total R | mean R | median R | gross+ | gross− | "
                   "share+ | share− |\n"
                   "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for entry in block["sessions"]:
            gross = entry["gross"]
            out.append(
                f"| `{entry['session']}` | {entry['evaluated_n']:,} | "
                f"{entry['filled_n']:,} | {entry['unfilled_n']:,} | "
                f"{_num(entry['fill_rate'])} | {_num(entry['total_r'], 1)} | "
                f"{_num(entry['mean_r_fill_basis'], 5)} | "
                f"{_num(entry['median_r'])} | "
                f"{_num(gross['win_contribution'], 1)} | "
                f"{_num(gross['loss_contribution'], 1)} | "
                f"{_num(gross['share_of_parent_gross_positive'])} | "
                f"{_num(gross['share_of_parent_gross_negative'])} |\n")
        closure = block["closure"]
        out.append(f"\n> Shares are scoped to this cell and sum to 1.0 "
                   f"(positive: {_num(closure['session_shares_sum_to_one_positive'])}, "
                   f"negative: {_num(closure['session_shares_sum_to_one_negative'])}). "
                   f"Session n sums to cell n: "
                   f"{_num(closure['session_n_sums_to_cell'])}.\n\n")

    # -- 3 · WIN / LOSS accounting ------------------------------------------
    out.append("## 3 · WIN / LOSS accounting\n\n")
    out.append(_denominator_line(payload["overview"]["denominators"]))
    out.append(f"> {payload['rows'][0]['win_loss']['note']}.\n\n")
    out.append("| Cell | Session | scored | WIN | LOSE | win rate | r>0 | "
               "r<0 | r=0 |\n|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for entry in payload["rows"]:
        block = entry["win_loss"]
        out.append(f"| `{entry['cell']}` | `{entry['session']}` | "
                   f"{block['scored_n']:,} | {block['win_count']:,} | "
                   f"{block['lose_count']:,} | {_num(block['win_rate'])} | "
                   f"{block['positive_r_n']:,} | {block['negative_r_n']:,} | "
                   f"{block['zero_r_n']:,} |\n")
    out.append("\n")

    # -- 4 · concentration ---------------------------------------------------
    out.append("## 4 · Concentration\n\n")
    out.append(_denominator_line(payload["overview"]["denominators"]))
    out.append(f"> {payload['rows'][0]['concentration']['scope']}.\n\n")
    out.append("| Cell | Session | symbols | top-1 share+ | top-5 share+ | "
               "HHI+ | top-1 share− | HHI− |\n"
               "|---|---|---:|---:|---:|---:|---:|---:|\n")
    for entry in payload["rows"]:
        block = entry["concentration"]
        positive = block["positive"]["by_symbol"]
        negative = block["negative"]["by_symbol"]
        out.append(f"| `{entry['cell']}` | `{entry['session']}` | "
                   f"{block['symbols_present']} | "
                   f"{_num(positive['top']['top_1']['share'])} | "
                   f"{_num(positive['top']['top_5']['share'])} | "
                   f"{_num(positive['herfindahl'])} | "
                   f"{_num(negative['top']['top_1']['share'])} | "
                   f"{_num(negative['herfindahl'])} |\n")
    out.append("\n")

    # -- 5 · persistence -----------------------------------------------------
    out.append("## 5 · Persistence (quarterly)\n\n")
    out.append(_denominator_line(payload["overview"]["denominators"]))
    out.append(f"> {payload['rows'][0]['persistence']['floor_note']}.\n\n")
    out.append("| Cell | Session | quarters present | usable | unusable | "
               "positive | share | longest negative run |\n"
               "|---|---|---:|---:|---:|---:|---:|---:|\n")
    for entry in payload["rows"]:
        block = entry["persistence"]
        out.append(f"| `{entry['cell']}` | `{entry['session']}` | "
                   f"{block['quarters_present']} | {block['quarters_usable']} | "
                   f"{len(block['quarters_unusable'])} | "
                   f"{block['quarters_positive']} | "
                   f"{_num(block['positive_quarter_share'])} | "
                   f"{block['longest_negative_run_quarters']} |\n")
    out.append("\n> Unusable quarters are named per row in the JSON artifact "
               "and are not dropped.\n\n")

    # -- 6 · friction --------------------------------------------------------
    out.append("## 6 · Friction\n\n")
    out.append(_denominator_line(payload["overview"]["denominators"]))
    out.append(f"> {payload['rows'][0]['friction']['note']}.\n\n")
    columns = [f"{basis_points:g}bp_{label}" for basis_points in FEE_BAND_BP
               for label, _legs in LEG_CONVENTIONS]
    out.append("| Cell | Session | notional/R p25 | median | p75 | "
               + " | ".join(columns) + " |\n")
    out.append("|---|---|" + "---:|" * (3 + len(columns)) + "\n")
    for entry in payload["rows"]:
        block = entry["friction"]
        implied = " | ".join(_num(block["implied_friction_r"][column])
                             for column in columns)
        out.append(f"| `{entry['cell']}` | `{entry['session']}` | "
                   f"{_num(block['notional_per_r_p25'], 1)} | "
                   f"{_num(block['notional_per_r_median'], 1)} | "
                   f"{_num(block['notional_per_r_p75'], 1)} | {implied} |\n")
    out.append("\n")

    # -- 7 · limitations -----------------------------------------------------
    out.append("## 7 · Limitations and prohibited interpretations\n\n")
    out.append(_denominator_line(payload["overview"]["denominators"]))
    out.append("### Prohibited statements\n\n")
    for statement in governance["prohibited_statements"]:
        out.append(f"- {statement}\n")
    out.append("\n### Irreducible limitations\n\n")
    for limitation in payload["limitations"]:
        out.append(f"- {limitation}\n")
    return "".join(out)


def csv_rows(payload: dict[str, Any]) -> list[list[str]]:
    """One row per (cell, session)."""
    header = ["cell", "session", "evaluated_n", "filled_n", "unfilled_n",
              "fill_rate", "win_count", "lose_count", "win_rate", "total_r",
              "mean_r_fill_basis", "median_r", "decision_basis_mean_r",
              "decision_basis_shrinkage_factor", "bracket_width_r",
              "gross_positive", "gross_negative", "net",
              "share_of_parent_gross_positive",
              "share_of_parent_gross_negative", "symbols_present",
              "top_1_symbol_share_positive", "herfindahl_positive",
              "quarters_present", "quarters_usable", "quarters_positive",
              "longest_negative_run_quarters", "notional_per_r_median",
              "friction_5bp_round_trip", "usability_markers",
              "evidence_class", "shadow_records_excluded",
              "unfilled_decisions_excluded"]
    rows = [header]
    for entry in payload["rows"]:
        gross = entry["gross"]
        fill = entry["fill_selection"]
        win_loss = entry["win_loss"]
        concentration = entry["concentration"]["positive"]["by_symbol"]
        persistence = entry["persistence"]
        friction = entry["friction"]
        rows.append([
            entry["cell"], entry["session"], str(entry["evaluated_n"]),
            str(entry["filled_n"]), str(entry["unfilled_n"]),
            _num(entry["fill_rate"]), str(win_loss["win_count"]),
            str(win_loss["lose_count"]), _num(win_loss["win_rate"]),
            _num(entry["total_r"], 3), _num(entry["mean_r_fill_basis"], 6),
            _num(entry["median_r"]), _num(fill["decision_basis_mean_r"], 6),
            _num(fill["decision_basis_shrinkage_factor"]),
            _num(fill["bracket_width_r"], 3),
            _num(gross["win_contribution"], 3),
            _num(gross["loss_contribution"], 3), _num(gross["net"], 3),
            _num(gross["share_of_parent_gross_positive"]),
            _num(gross["share_of_parent_gross_negative"]),
            str(entry["concentration"]["symbols_present"]),
            _num(concentration["top"]["top_1"]["share"]),
            _num(concentration["herfindahl"]),
            str(persistence["quarters_present"]),
            str(persistence["quarters_usable"]),
            str(persistence["quarters_positive"]),
            str(persistence["longest_negative_run_quarters"]),
            _num(friction["notional_per_r_median"], 2),
            _num(friction["implied_friction_r"]["5bp_round_trip"]),
            ";".join(entry["usability_markers"]),
            entry["evidence_class"],
            str(entry["denominators"]["shadow_records_excluded"]),
            str(entry["denominators"]
                ["unfilled_decisions_excluded_from_fill_basis"]),
        ])
    return rows


def _csv_escape(value: str) -> str:
    text = str(value)
    if any(character in text for character in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_decomposition(payload: dict[str, Any], out_dir: Path | str,
                        *, identity: Optional[dict[str, Any]] = None,
                        ) -> dict[str, Path]:
    """Emit the three artifacts into the caller's directory."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["identity"] = dict(identity or {})
    body["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    written: dict[str, Path] = {}
    json_path = out / "session_decomposition.json"
    json_path.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written["session_decomposition.json"] = json_path

    md_path = out / "session_decomposition.md"
    md_path.write_text(render(payload), encoding="utf-8")
    written["session_decomposition.md"] = md_path

    csv_path = out / "session_decomposition.csv"
    csv_path.write_text(
        "\n".join(",".join(_csv_escape(column) for column in row)
                  for row in csv_rows(payload)) + "\n", encoding="utf-8")
    written["session_decomposition.csv"] = csv_path
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Explicit entry point. Deliberately not a CLI verb."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="session_decomposition",
        description="Realized R by mode x direction x session, descriptively.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    payload = analyse(run_dir)
    out_dir = (Path(args.out) if args.out
               else run_dir / "analytics" / "research" / DECOMPOSITION_DIRNAME)
    written = write_decomposition(payload, out_dir,
                                  identity={"run_dir": str(run_dir)})
    for name, path in sorted(written.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
