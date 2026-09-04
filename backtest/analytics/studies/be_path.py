"""BE@1R path measurement — the one missing path primitive.

Opt-in and unregistered: absent from `studies.STUDIES`, absent from the CLI.

## The single question

For trades that reach +1R and eventually win, what **R-weighted** fraction `q`
returns to the fill price after +1R but before TP?

`q` is compared to a threshold `q*` frozen in `Q_STAR` **before this module
existed**, derived from the exit audit's recoverable-pool arithmetic on the same
frozen bundle. `q*` is never recomputed here, and nothing in this module
searches, optimises, or ranks.

    BE@1R is EV-positive  iff  q < q*        (per cell)

## Why this does not touch the walker

`backtest.outcome.walker` is a locked surface. This module **inherits** every
decision it made — `fill_T`, `fill_price`, `fill_kind`, `exit_T`, `exit_reason`,
`realized_r`, `mfe_r` all come from the frozen `outcomes.jsonl` — and re-reads
only the candles *between* fill and exit to locate intermediate level touches
the outcome schema never recorded. Nothing is re-decided, so the frozen outcomes
cannot drift and no contract surface changes. No replay, no admission change, no
`OutcomeConfig` change.

## Segment model, and why ordering is honest

The walk is reproduced at the walker's own granularity:

- each H1 candle from `fill_T` to `exit_T` inclusive is expanded into its 12 M5
  candles when the M5 set is **complete** (`M5_PER_H1`, C-4.3's definition),
  otherwise it stays one coarse H1 segment and the trade is flagged `degraded`;
- on a limit-fill candle the scan starts at the first M5 candle in which the
  limit could have filled, mirroring `_Walk._fill_index`. Without this a dip
  *before* the fill would be miscounted as a return to entry;
- level tests are the walker's own directional predicates (`_Walk._hits`):
  favourable levels need `high >= level` for a long, `low <= level` for a short.

Ordering is asserted only **between** segments. Two events inside one segment are
not ordered — H1 (or 5m) data cannot establish intrabar sequence, and this module
does not invent one.

## The bracket

Every ambiguity is reported both ways, and the interval is the answer:

    q_lower   an ambiguous return is treated as NOT observed
    q_upper   an ambiguous return is treated as observed wherever it is
              logically possible

A return is **unambiguous** only when its segment lies strictly after the +1R
segment and strictly before the TP segment. Sharing a segment with either
endpoint is ambiguous and lands in the bracket, never in a point estimate.

## Estimand

R-weighted, matching how `q*` was constructed: the numerator is the sum of
`realized_r` over returning winners, the denominator the sum over all
+1R-reaching winners. An unweighted count would answer a different question and
would not be comparable to `q*`. Both are emitted; only the weighted one is
compared.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional, Sequence

from backtest.analytics.studies import fill_null as fn
from backtest.analytics.studies.exit_census import CELLS, SL_CLASS, TP_CLASS

__all__ = [
    "BE_TRIGGER_R",
    "CELLS",
    "DIRNAME",
    "EVIDENCE_CLASS",
    "LADDER_R",
    "M5_PER_H1",
    "PathFacts",
    "Q_STAR",
    "SCOPE_STATEMENT",
    "BePathError",
    "analyse",
    "csv_rows",
    "measure",
    "path_facts",
    "render",
    "specification_hash",
    "write_be_path",
]

DIRNAME = "be_path"
ENGINE_VERSION = 1
EVIDENCE_CLASS = "DESCRIPTIVE"

SCOPE_STATEMENT = (
    "Path measurement only. Reports the R-weighted return-to-entry fraction "
    "q against a pre-frozen threshold. No exit rule is implemented or "
    "simulated, no threshold is searched, and no profitability claim is made."
)

H1_SECONDS = 3600

#: C-4.3's definition of a complete M5 set, verbatim from the walker.
M5_PER_H1 = 12

#: The level ladder, in R from the fill price. Fixed; not a search grid.
LADDER_R: tuple = (0.5, 1.0, 1.5, 2.0)

#: The only BE trigger this module measures. One level, by instruction.
BE_TRIGGER_R = 1.0

#: Pre-frozen break-even thresholds from the exit audit, computed on this same
#: bundle BEFORE any path data existed: q* = |pool R| / winner R, where the pool
#: is "reached +1R, finished <= 0" and winners are "reached +1R, finished > 0".
#: These are INPUTS. Nothing here recomputes or tunes them.
Q_STAR: dict = {
    "CONTINUATION×SHORT": 0.2077,
    "CONTINUATION×LONG": 0.2555,
    "RETRACEMENT×SHORT": 0.3472,
    "RETRACEMENT×LONG": 0.3679,
}

#: Pre-registered kill rule, stated before measurement.
KILL_RULE = (
    "Trade-management BE path is killed if the measured q is at or above q* "
    "in at least 3 of the 4 cells."
)
KILL_CELL_THRESHOLD = 3

VERDICTS: tuple = ("BELOW_Q_STAR", "ABOVE_Q_STAR", "CROSSES_Q_STAR",
                   "INSUFFICIENT")


class BePathError(Exception):
    """The path measurement cannot be assembled, or an invariant broke."""


# ---------------------------------------------------------------------------
# Path facts for one trade
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PathFacts:
    """First-passage facts for one trade, at the walker's own granularity.

    `t_*` fields are H1 candle indices in the walker's convention
    (`(open_time - fill_T) // 3600`); `seg_*` are the finer segment indices the
    ordering is actually decided on. `None` means never touched.
    """

    signal_id: Optional[str]
    cell: str
    realized_r: float
    mfe_r: float
    is_winner: bool

    t_touch_0_5r: Optional[int] = None
    t_touch_1r: Optional[int] = None
    t_touch_1_5r: Optional[int] = None
    t_touch_2r: Optional[int] = None
    t_return_entry_after_1r: Optional[int] = None
    t_touch_sl: Optional[int] = None
    t_touch_tp: Optional[int] = None

    seg_touch_1r: Optional[int] = None
    seg_return_entry_after_1r: Optional[int] = None
    seg_touch_tp: Optional[int] = None

    returned_before_tp: bool = False
    return_ambiguous: bool = False
    ambiguous_1r_entry_same_segment: bool = False
    ambiguous_entry_tp_same_segment: bool = False
    ambiguous_1r_tp_same_segment: bool = False
    degraded_no_m5: bool = False
    segments_scanned: int = 0
    mfe_disagreement: bool = False

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


# ---------------------------------------------------------------------------
# Segment construction
# ---------------------------------------------------------------------------

def _limit_fill_reached(is_long: bool, entry: float, open_: float, low: float,
                        high: float) -> bool:
    """`_Walk._limit_fill` reduced to a boolean, for locating the fill segment."""
    if is_long:
        return open_ < entry or low <= entry
    return open_ > entry or high >= entry


def _fill_segment_index(rows, is_long: bool, entry: float) -> int:
    for offset in range(len(rows)):
        row = rows[offset]
        if _limit_fill_reached(is_long, entry, float(row["open"]),
                               float(row["low"]), float(row["high"])):
            return offset
    return 0


def _segments(record, repository) -> tuple:
    """The ordered (h1_index, high, low) segments from fill to exit inclusive.

    Returns `(segments, degraded)`. Mirrors the walker's granularity exactly:
    M5 where the set is complete, the H1 candle otherwise.
    """
    native = record.symbol.replace("/", "")
    fill_open = int(record.fill_T)
    exit_open = int(record.exit_T)
    if exit_open < fill_open:
        raise BePathError(
            f"{record.signal_id}: exit_T {exit_open} precedes fill_T "
            f"{fill_open}")

    h1 = repository.range(native, "1h", start_ms=fill_open * 1000,
                          end_ms=exit_open * 1000 + 1)
    segments: list = []
    degraded = False
    is_long = record.direction == "LONG"
    limit_fill = record.fill_kind in ("LIMIT_TOUCH", "LIMIT_GAP")

    for position in range(len(h1)):
        row = h1[position]
        open_time = int(row["timestamp"]) // 1000
        h1_index = (open_time - fill_open) // H1_SECONDS
        m5 = repository.range(native, "5m", start_ms=open_time * 1000,
                              end_ms=open_time * 1000 + H1_SECONDS * 1000)
        if len(m5) == M5_PER_H1:
            start = 0
            if position == 0 and limit_fill:
                start = _fill_segment_index(m5, is_long,
                                            float(record.fill_price))
            for offset in range(start, len(m5)):
                sub = m5[offset]
                segments.append((int(h1_index), float(sub["high"]),
                                 float(sub["low"])))
        else:
            degraded = True
            segments.append((int(h1_index), float(row["high"]),
                             float(row["low"])))
    return segments, degraded


# ---------------------------------------------------------------------------
# First passage
# ---------------------------------------------------------------------------

def _first_favourable(segments: Sequence, is_long: bool, level: float,
                      start: int = 0) -> Optional[int]:
    """First segment index at/after `start` reaching a favourable level."""
    for index in range(start, len(segments)):
        _h1, high, low = segments[index]
        if (high >= level) if is_long else (low <= level):
            return index
    return None


def _first_adverse(segments: Sequence, is_long: bool, level: float,
                   start: int = 0) -> Optional[int]:
    """First segment index at/after `start` reaching an adverse level."""
    for index in range(start, len(segments)):
        _h1, high, low = segments[index]
        if (low <= level) if is_long else (high >= level):
            return index
    return None


def path_facts(record, repository) -> PathFacts:
    """Measure one trade's first-passage facts. Decides nothing about exits."""
    is_long = record.direction == "LONG"
    fill_price = float(record.fill_price)
    r_unit = abs(fill_price - float(record.sl))
    if r_unit == 0.0:
        raise BePathError(f"{record.signal_id}: zero risk unit")
    sign = 1.0 if is_long else -1.0

    segments, degraded = _segments(record, repository)
    cell = f"{record.entry_mode}×{record.direction}"
    realized_r = float(record.realized_r)
    is_winner = realized_r > 0.0

    def h1_of(index: Optional[int]) -> Optional[int]:
        return None if index is None else segments[index][0]

    ladder_seg = {
        level: _first_favourable(segments, is_long,
                                 fill_price + sign * level * r_unit)
        for level in LADDER_R
    }
    seg_1r = ladder_seg[BE_TRIGGER_R]
    seg_tp = _first_favourable(segments, is_long, float(record.tp))
    seg_sl = _first_adverse(segments, is_long, float(record.sl))

    seg_return = None
    if seg_1r is not None:
        seg_return = _first_adverse(segments, is_long, fill_price,
                                    start=seg_1r)

    returned = False
    ambiguous = False
    amb_1r_entry = amb_entry_tp = amb_1r_tp = False
    if seg_1r is not None and seg_return is not None:
        amb_1r_entry = seg_return == seg_1r
        amb_entry_tp = seg_tp is not None and seg_return == seg_tp
        amb_1r_tp = seg_tp is not None and seg_1r == seg_tp
        before_tp = seg_tp is None or seg_return <= seg_tp
        if before_tp:
            if amb_1r_entry or amb_entry_tp:
                # Shares a segment with +1R or with TP: intrabar order is not
                # establishable, so it enters the bracket, never the point.
                ambiguous = True
            else:
                returned = True

    return PathFacts(
        signal_id=record.signal_id,
        cell=cell,
        realized_r=realized_r,
        mfe_r=float(record.mfe_r) if record.mfe_r is not None else 0.0,
        is_winner=is_winner,
        t_touch_0_5r=h1_of(ladder_seg[0.5]),
        t_touch_1r=h1_of(ladder_seg[1.0]),
        t_touch_1_5r=h1_of(ladder_seg[1.5]),
        t_touch_2r=h1_of(ladder_seg[2.0]),
        t_return_entry_after_1r=h1_of(seg_return),
        t_touch_sl=h1_of(seg_sl),
        t_touch_tp=h1_of(seg_tp),
        seg_touch_1r=seg_1r,
        seg_return_entry_after_1r=seg_return,
        seg_touch_tp=seg_tp,
        returned_before_tp=returned,
        return_ambiguous=ambiguous,
        ambiguous_1r_entry_same_segment=amb_1r_entry,
        ambiguous_entry_tp_same_segment=amb_entry_tp,
        ambiguous_1r_tp_same_segment=amb_1r_tp,
        degraded_no_m5=degraded,
        segments_scanned=len(segments),
        mfe_disagreement=bool(
            (float(record.mfe_r or 0.0) >= BE_TRIGGER_R) != (seg_1r is not None)),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _cell_measure(facts: Sequence[PathFacts], cell: str) -> dict[str, Any]:
    """q_lower / q_upper for one cell, R-weighted, plus the descriptive block."""
    reached = [f for f in facts if f.seg_touch_1r is not None
               or f.mfe_r >= BE_TRIGGER_R]
    winners = [f for f in reached if f.is_winner]
    pool = [f for f in reached if not f.is_winner]

    winner_r = sum(f.realized_r for f in winners)
    returned = [f for f in winners if f.returned_before_tp]
    ambiguous = [f for f in winners if f.return_ambiguous]
    returned_r = sum(f.realized_r for f in returned)
    ambiguous_r = sum(f.realized_r for f in ambiguous)

    q_lower = (returned_r / winner_r) if winner_r > 0 else None
    q_upper = ((returned_r + ambiguous_r) / winner_r) if winner_r > 0 else None
    q_star = Q_STAR[cell]

    if q_lower is None or q_upper is None or not winners:
        verdict = "INSUFFICIENT"
    elif q_upper < q_star:
        verdict = "BELOW_Q_STAR"
    elif q_lower >= q_star:
        verdict = "ABOVE_Q_STAR"
    else:
        verdict = "CROSSES_Q_STAR"

    all_returned = [f for f in reached if f.returned_before_tp]
    all_ambiguous = [f for f in reached if f.return_ambiguous]

    def _times(rows, attr_a, attr_b):
        out = []
        for row in rows:
            a, b = getattr(row, attr_a), getattr(row, attr_b)
            if a is not None and b is not None:
                out.append(b - a)
        return out

    to_return = _times(returned, "t_touch_1r", "t_return_entry_after_1r")
    to_tp = _times(winners, "t_touch_1r", "t_touch_tp")
    winner_rr = [f.realized_r for f in winners]

    return {
        "cell": cell,
        "evidence_class": EVIDENCE_CLASS,
        "q_star": q_star,
        "q_lower": q_lower,
        "q_upper": q_upper,
        "verdict": verdict,
        "n_reached_1r": len(reached),
        "n_winners": len(winners),
        "n_pool": len(pool),
        "winner_r_weight": winner_r,
        "returned_r_weight": returned_r,
        "ambiguous_r_weight": ambiguous_r,
        "n_returned_unambiguous": len(returned),
        "n_returned_ambiguous": len(ambiguous),
        "ambiguity_share_of_winners": (
            len(ambiguous) / len(winners)) if winners else None,
        "q_unweighted_lower": (
            len(returned) / len(winners)) if winners else None,
        "q_unweighted_upper": (
            (len(returned) + len(ambiguous)) / len(winners)) if winners else None,
        "descriptive": {
            "p_return_given_1r_and_winner_lower": (
                len(returned) / len(winners)) if winners else None,
            "p_return_given_1r_lower": (
                len(all_returned) / len(reached)) if reached else None,
            "p_return_given_1r_upper": (
                (len(all_returned) + len(all_ambiguous)) / len(reached)
            ) if reached else None,
            "median_h1_from_1r_to_return": median(to_return) if to_return else None,
            "median_h1_from_1r_to_tp": median(to_tp) if to_tp else None,
            "median_winner_rr": median(winner_rr) if winner_rr else None,
            "mean_winner_rr": (
                sum(winner_rr) / len(winner_rr)) if winner_rr else None,
            "same_candle_ambiguity_rate": (
                len(all_ambiguous) / len(reached)) if reached else None,
            "degraded_no_m5_n": sum(1 for f in reached if f.degraded_no_m5),
            "mfe_disagreement_n": sum(1 for f in reached if f.mfe_disagreement),
        },
    }


def measure(facts_by_cell: dict) -> dict[str, Any]:
    cells = {cell: _cell_measure(facts_by_cell.get(cell, ()), cell)
             for cell, _mode, _direction in CELLS}
    at_or_above = sum(
        1 for block in cells.values()
        if block["verdict"] == "ABOVE_Q_STAR"
        or (block["verdict"] == "CROSSES_Q_STAR"
            and block["q_upper"] is not None
            and block["q_upper"] >= block["q_star"]
            and block["q_lower"] is not None
            and block["q_lower"] >= block["q_star"]))
    strictly_above = sum(1 for block in cells.values()
                         if block["verdict"] == "ABOVE_Q_STAR")
    return {
        "cells": cells,
        "kill_rule": KILL_RULE,
        "kill_cell_threshold": KILL_CELL_THRESHOLD,
        "cells_at_or_above_q_star": strictly_above,
        "killed": strictly_above >= KILL_CELL_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# Specification hash
# ---------------------------------------------------------------------------

def _specification_payload() -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
        "ladder_r": list(LADDER_R),
        "be_trigger_r": BE_TRIGGER_R,
        "q_star": dict(sorted(Q_STAR.items())),
        "kill_rule": KILL_RULE,
        "kill_cell_threshold": KILL_CELL_THRESHOLD,
        "verdicts": list(VERDICTS),
        "cells": [name for name, _m, _d in CELLS],
        "m5_per_h1": M5_PER_H1,
        "sl_class": sorted(SL_CLASS),
        "tp_class": sorted(TP_CLASS),
    }


def specification_hash() -> str:
    canonical = json.dumps(_specification_payload(), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _identity(run_dir: Path, records: Sequence) -> dict[str, Any]:
    metrics_hash = None
    path = run_dir / "metrics.json"
    if path.exists():
        try:
            metrics_hash = json.loads(
                path.read_text(encoding="utf-8")).get("metrics_hash")
        except (OSError, json.JSONDecodeError):
            metrics_hash = None
    first = records[0] if records else None
    return {
        "run_id": first.run_id if first is not None else "",
        "source": first.source if first is not None else "",
        "strategy_version": first.strategy_version if first is not None else None,
        "metrics_hash": metrics_hash,
        "outcome_config_hash": (
            first.outcome_config_hash if first is not None else None),
        "data_snapshot_id": first.data_snapshot_id if first is not None else None,
        "be_path_specification_hash": specification_hash(),
        "evidence_class": EVIDENCE_CLASS,
        "scope": SCOPE_STATEMENT,
    }


def analyse(run_dir: Path | str, *, repository=None,
            limit: Optional[int] = None) -> dict[str, Any]:
    """Measure `q` on the frozen §B3 `barrier_resolved` population."""
    from backtest.analytics.studies.common import labelled_trades
    from backtest.data.repository import KlineRepository

    run_dir = Path(run_dir)
    trades = list(labelled_trades(run_dir))
    records = [t.record for t in trades]
    pops = fn.partition(records)
    pops.check()
    resolved = list(pops.barrier_resolved)

    if repository is None:
        manifest = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8"))
        root = manifest.get("repository_root") or "data/stage1/canonical"
        repository = KlineRepository(root)

    eligible = [r for r in resolved
                if r.mfe_r is not None and float(r.mfe_r) >= BE_TRIGGER_R
                and r.fill_T is not None and r.exit_T is not None
                and r.exit_reason in (SL_CLASS | TP_CLASS)]
    if limit is not None:
        eligible = eligible[:limit]

    facts_by_cell: dict = {name: [] for name, _m, _d in CELLS}
    skipped = 0
    for record in eligible:
        cell = f"{record.entry_mode}×{record.direction}"
        if cell not in facts_by_cell:
            skipped += 1
            continue
        facts_by_cell[cell].append(path_facts(record, repository))

    payload = _identity(run_dir, resolved)
    payload.update(measure(facts_by_cell))
    payload["population"] = {
        "barrier_resolved": len(resolved),
        "eligible_reached_1r": len(eligible),
        "skipped_unknown_cell": skipped,
        "population_definition": "frozen §B3 barrier_resolved, mfe_r >= 1.0",
    }
    return payload


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


def render(payload: dict[str, Any]) -> str:
    lines: list = ["# BE@1R Path Measurement", ""]
    for key in ("run_id", "source", "strategy_version", "metrics_hash",
                "outcome_config_hash", "data_snapshot_id",
                "be_path_specification_hash", "evidence_class", "generated_at"):
        lines.append(f"- {key}: `{payload.get(key)}`")
    lines.append("")
    lines.append(f"> {payload.get('scope', '')}")
    lines.append("")
    population = payload.get("population", {})
    lines.append(
        f"Population: {population.get('barrier_resolved')} barrier_resolved, "
        f"{population.get('eligible_reached_1r')} reached +1R "
        f"({population.get('population_definition')}).")
    lines.append("")

    lines.append("## q vs pre-frozen q*")
    lines.append("")
    lines.append(
        "| cell | q* | q_lower | q_upper | verdict | winners | winner R | "
        "returned R | ambiguous R |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for cell, block in payload.get("cells", {}).items():
        lines.append(
            f"| {cell} | {_num(block['q_star'])} | {_num(block['q_lower'])} | "
            f"{_num(block['q_upper'])} | {block['verdict']} | "
            f"{block['n_winners']} | {_num(block['winner_r_weight'], 1)} | "
            f"{_num(block['returned_r_weight'], 1)} | "
            f"{_num(block['ambiguous_r_weight'], 1)} |")
    lines.append("")

    lines.append("## Ambiguity")
    lines.append("")
    lines.append(
        "| cell | unambiguous returns | ambiguous returns | ambiguity share | "
        "degraded (no M5) | MFE disagreements |")
    lines.append("|---|---|---|---|---|---|")
    for cell, block in payload.get("cells", {}).items():
        descriptive = block["descriptive"]
        lines.append(
            f"| {cell} | {block['n_returned_unambiguous']} | "
            f"{block['n_returned_ambiguous']} | "
            f"{_num(block['ambiguity_share_of_winners'])} | "
            f"{descriptive['degraded_no_m5_n']} | "
            f"{descriptive['mfe_disagreement_n']} |")
    lines.append("")

    lines.append("## Descriptive")
    lines.append("")
    lines.append(
        "| cell | P(return \\| 1R, winner) lower | P(return \\| 1R) lower | "
        "P(return \\| 1R) upper | median h1 1R→return | median h1 1R→TP | "
        "median winner RR | mean winner RR |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cell, block in payload.get("cells", {}).items():
        d = block["descriptive"]
        lines.append(
            f"| {cell} | {_num(d['p_return_given_1r_and_winner_lower'])} | "
            f"{_num(d['p_return_given_1r_lower'])} | "
            f"{_num(d['p_return_given_1r_upper'])} | "
            f"{_num(d['median_h1_from_1r_to_return'], 1)} | "
            f"{_num(d['median_h1_from_1r_to_tp'], 1)} | "
            f"{_num(d['median_winner_rr'])} | {_num(d['mean_winner_rr'])} |")
    lines.append("")

    lines.append("## Pre-registered kill rule")
    lines.append("")
    lines.append(f"> {payload.get('kill_rule', '')}")
    lines.append("")
    lines.append(
        f"- cells strictly at or above q*: "
        f"**{payload.get('cells_at_or_above_q_star')}** of {len(CELLS)}")
    lines.append(f"- kill triggered: **{payload.get('killed')}**")
    lines.append("")
    lines.append(
        "No exit rule is implemented or simulated here, and no profitability "
        "claim follows from any figure above.")
    lines.append("")
    return "\n".join(lines) + "\n"


_CSV_HEADER = (
    "cell", "q_star", "q_lower", "q_upper", "verdict", "n_reached_1r",
    "n_winners", "n_pool", "winner_r_weight", "returned_r_weight",
    "ambiguous_r_weight", "n_returned_unambiguous", "n_returned_ambiguous",
    "ambiguity_share_of_winners", "median_h1_from_1r_to_return",
    "median_h1_from_1r_to_tp", "median_winner_rr", "degraded_no_m5_n",
)


def csv_rows(payload: dict[str, Any]) -> list:
    rows: list = [list(_CSV_HEADER)]
    for cell, block in payload.get("cells", {}).items():
        d = block["descriptive"]
        rows.append([
            cell, block["q_star"], block["q_lower"], block["q_upper"],
            block["verdict"], block["n_reached_1r"], block["n_winners"],
            block["n_pool"], block["winner_r_weight"],
            block["returned_r_weight"], block["ambiguous_r_weight"],
            block["n_returned_unambiguous"], block["n_returned_ambiguous"],
            block["ambiguity_share_of_winners"],
            d["median_h1_from_1r_to_return"], d["median_h1_from_1r_to_tp"],
            d["median_winner_rr"], d["degraded_no_m5_n"],
        ])
    return rows


def _csv_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_be_path(payload: dict[str, Any], out_dir: Path | str
                  ) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["generated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    written: dict[str, Path] = {}
    json_path = out / "be_path.json"
    json_path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    written["be_path.json"] = json_path
    md_path = out / "be_path.md"
    md_path.write_text(render(body), encoding="utf-8")
    written["be_path.md"] = md_path
    csv_path = out / "be_path.csv"
    csv_path.write_text(
        "\n".join(",".join(_csv_escape(c) for c in row)
                  for row in csv_rows(payload)) + "\n", encoding="utf-8")
    written["be_path.csv"] = csv_path
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="be_path",
        description="Measure the R-weighted return-to-entry fraction q.")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run)
    payload = analyse(run_dir, limit=args.limit)
    out_dir = (Path(args.out) if args.out
               else run_dir / "analytics" / "research" / DIRNAME)
    for name, path in sorted(write_be_path(payload, out_dir).items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":               # pragma: no cover
    raise SystemExit(main())
