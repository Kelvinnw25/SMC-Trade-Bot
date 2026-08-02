"""The equity curve, drawdown and losing streaks — C-9.8.

Max drawdown is **path-dependent**, so the order in which trades contribute is not
a presentation choice: C-9.8 fixes it as `(exit_T, cycle_index, universe_rank,
symbol)` — the first three from the trade's *exit* event, the last as a final
tie-break — with each trade adding its `realized_r` to a running sum from 0.
Unfilled and `OPEN` trades contribute nothing.

Two consequences worth stating, because both are easy to get wrong:

- the order is by **exit**, not by instantiation. A trade opened early and held for
  a week lands in the curve after trades opened later and closed sooner, which is
  what an operator's account would have experienced;
- the equity convention is **1R per trade** (C-4.12/§13.8). No position sizing
  exists (A25), so the curve is a sum of R, not of currency, and its drawdown is
  in R. It is therefore *not* a compounded return series and must never be
  presented as one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from backtest.outcome.record import OutcomeRecord

__all__ = ["EquityCurve", "build_equity_curve", "c98_order_key"]


def c98_order_key(record: OutcomeRecord) -> tuple[int, int, int, str]:
    """C-9.8's ordering key.

    `exit_T` is `None` only for records that contribute nothing, which are filtered
    before sorting; `cycle_index` and `universe_rank` are optional in the schema, so
    a missing value sorts first via `-1` rather than raising. Deterministic for any
    input, which is the property the curve depends on.
    """
    return (
        int(record.exit_T or 0),
        -1 if record.cycle_index is None else int(record.cycle_index),
        -1 if record.universe_rank is None else int(record.universe_rank),
        record.symbol,
    )


@dataclass(frozen=True)
class EquityCurve:
    """The 1R equity path and everything derived from it."""

    #: Cumulative R after each contributing trade, in C-9.8 order.
    points: tuple[float, ...]
    #: `(exit_T, cumulative_r)` pairs — the curve as a reader plots it.
    series: tuple[tuple[int, float], ...]
    max_drawdown_r: Optional[float]
    max_drawdown_start: Optional[int]
    max_drawdown_end: Optional[int]
    max_runup_r: Optional[float]
    max_consecutive_losses: int
    max_consecutive_wins: int
    final_r: Optional[float]
    trades: int

    def to_dict(self, *, include_series: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "trades": self.trades,
            "final_r": self.final_r,
            "max_drawdown_r": self.max_drawdown_r,
            "max_drawdown_start_exit_T": self.max_drawdown_start,
            "max_drawdown_end_exit_T": self.max_drawdown_end,
            "max_runup_r": self.max_runup_r,
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_consecutive_wins": self.max_consecutive_wins,
        }
        if include_series:
            payload["series"] = [[t, r] for t, r in self.series]
        return payload


def build_equity_curve(records: Iterable[OutcomeRecord]) -> EquityCurve:
    """Order by C-9.8, accumulate `realized_r`, and measure the path.

    Trades with no `realized_r` — every `NOT_FILLED`, and every `OPEN` trade — are
    excluded rather than treated as 0R. Treating them as 0R would leave the sum
    unchanged but insert flat steps into the path, which changes nothing about the
    drawdown *depth* while making the curve's length misrepresent how many
    positions were actually carried.
    """
    contributing = [record for record in records
                    if record.realized_r is not None and not record.is_open]
    if not contributing:
        return EquityCurve(
            points=(), series=(), max_drawdown_r=None, max_drawdown_start=None,
            max_drawdown_end=None, max_runup_r=None, max_consecutive_losses=0,
            max_consecutive_wins=0, final_r=None, trades=0,
        )

    contributing.sort(key=c98_order_key)

    points: list[float] = []
    series: list[tuple[int, float]] = []
    total = 0.0
    for record in contributing:
        total += float(record.realized_r)      # type: ignore[arg-type]
        points.append(total)
        series.append((int(record.exit_T or 0), total))

    drawdown, dd_start, dd_end = _max_drawdown(points, contributing)
    return EquityCurve(
        points=tuple(points),
        series=tuple(series),
        max_drawdown_r=drawdown,
        max_drawdown_start=dd_start,
        max_drawdown_end=dd_end,
        max_runup_r=_max_runup(points),
        max_consecutive_losses=_longest_run(contributing, negative=True),
        max_consecutive_wins=_longest_run(contributing, negative=False),
        final_r=points[-1],
        trades=len(points),
    )


def _max_drawdown(
    points: Sequence[float], records: Sequence[OutcomeRecord]
) -> tuple[float, Optional[int], Optional[int]]:
    """The largest peak-to-trough decline, plus the exits that bracket it.

    The running peak starts at **0.0**, not at the first point: C-9.8 says the sum
    starts at 0, so a curve that is negative from its first trade is already in
    drawdown. Anchoring the peak at `points[0]` instead would hide the first
    trade's loss from the drawdown entirely.
    """
    peak = 0.0
    peak_at: Optional[int] = None
    worst = 0.0
    start: Optional[int] = None
    end: Optional[int] = None
    for index, value in enumerate(points):
        if value > peak:
            peak = value
            peak_at = int(records[index].exit_T or 0)
        decline = peak - value
        if decline > worst:
            worst = decline
            start = peak_at
            end = int(records[index].exit_T or 0)
    return worst, start, end


def _max_runup(points: Sequence[float]) -> float:
    trough = 0.0
    best = 0.0
    for value in points:
        if value < trough:
            trough = value
        best = max(best, value - trough)
    return best


def _longest_run(records: Sequence[OutcomeRecord], *, negative: bool) -> int:
    """Longest streak of losses (or wins) by sign of `realized_r`.

    Sign, not state: a `EXPIRED` trade that closed below its fill is part of a
    losing streak as an operator experienced it, whatever its label says. Trades at
    exactly 0R break both streaks.
    """
    best = 0
    current = 0
    for record in records:
        value = float(record.realized_r)       # type: ignore[arg-type]
        hit = value < 0 if negative else value > 0
        current = current + 1 if hit else 0
        best = max(best, current)
    return best
