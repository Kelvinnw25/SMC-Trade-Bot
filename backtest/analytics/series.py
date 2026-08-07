"""Path and distribution analysis — M15.

Everything here describes the **shape** of a result rather than its level. The
metrics engine reports that the deepest drawdown was 655.8 R; this module reports
how many drawdowns there were, how long they lasted, whether they recovered, and
what the trade-by-trade path looked like on the way. Neither question is a
per-segment metric, which is why neither has a home in the registry.

## Ordering

Every sequence here walks the C-9.8 order — `(exit_T, cycle_index,
universe_rank, symbol)` — the same order the official equity curve uses. A
rolling statistic computed in a different order would describe a run that never
happened.

## Rolling windows

Fixed, declared window sizes. A window chosen to make a chart look good is a
free parameter, and a free parameter chosen after seeing the data is how a
research artifact starts lying.

## Sharpe

Reported as **mean(R) / stdev(R) over the window** and nothing else. It is not
annualised and it is not a portfolio Sharpe: there is no position sizing (A25),
trades overlap in time, and the unit of the series is one trade rather than one
period. Calling it a Sharpe ratio without that sentence attached would invite a
comparison with published figures that does not hold.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from backtest.analytics.stats import describe, percentiles

__all__ = [
    "DURATION_EDGES",
    "R_EDGES",
    "ROLLING_WINDOWS",
    "contributors",
    "drawdown_episodes",
    "duration_distribution",
    "holding_statistics",
    "leaderboard",
    "r_distribution",
    "rolling_statistics",
    "streaks",
]

H1_SECONDS = 3600

#: Fixed R bands. Declared edges, never quantiles — quantile bands move with the
#: data and make two baselines incomparable.
R_EDGES = (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)

#: Holding-time bands in H1 candles, matching the metrics engine's
#: `HOLDING_EDGES` so the two tables can be read side by side.
DURATION_EDGES = (1.0, 2.0, 4.0, 8.0, 24.0, 48.0, 96.0, 168.0)

#: Rolling window sizes in trades. Three scales because a single one always
#: flatters some conclusion: 100 shows regime texture, 1000 shows whether an
#: edge persists, 5000 shows the trend the whole run is made of.
ROLLING_WINDOWS = (100, 1_000, 5_000)

#: A symbol needs this many scored trades before its expectancy is reported as
#: anything but description. Mirrors the metrics engine's `MIN_SEGMENT_N`.
LEADERBOARD_MIN_N = 30


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def drawdown_episodes(
    series: Sequence[Sequence[float]],
) -> tuple[list[dict[str, Any]], Optional[float]]:
    """Every peak-to-peak drawdown, and the share of trades spent inside one.

    The running peak starts at the *first* point rather than at zero, which is
    what makes `max(depth_r)` reproduce the engine's `max_drawdown_r`: a curve
    whose first trade loses has not drawn down from an imaginary flat start.

    An episode still open at the last trade is `recovered: false` with no
    recovery time — reporting a recovery that has not happened would be the one
    genuinely misleading thing this table could do.
    """
    points = [(int(stamp), float(value)) for stamp, value in series]
    if not points:
        return [], None

    episodes: list[dict[str, Any]] = []
    peak_value = points[0][1]
    peak_index = 0
    trough_value = points[0][1]
    trough_index = 0
    open_episode = False
    below = 0

    for index, (_stamp, value) in enumerate(points):
        if value >= peak_value:
            if open_episode:
                episodes.append(_episode(points, peak_index, trough_index,
                                         index, peak_value, trough_value))
                open_episode = False
            peak_value = value
            peak_index = index
            continue
        below += 1
        if not open_episode:
            open_episode = True
            trough_value = value
            trough_index = index
        elif value < trough_value:
            trough_value = value
            trough_index = index

    if open_episode:
        episodes.append(_episode(points, peak_index, trough_index, None,
                                 peak_value, trough_value))
    return episodes, below / len(points)


def _episode(points: list[tuple[int, float]], peak_index: int,
             trough_index: int, end_index: Optional[int],
             peak_value: float, trough_value: float) -> dict[str, Any]:
    from backtest.analytics.core import _iso

    start_T = points[peak_index][0]
    trough_T = points[trough_index][0]
    end_T = points[end_index][0] if end_index is not None else None
    last_T = points[-1][0]
    return {
        "start_exit_T": start_T,
        "start_iso": _iso(start_T),
        "trough_exit_T": trough_T,
        "trough_iso": _iso(trough_T),
        "end_exit_T": end_T,
        "end_iso": _iso(end_T) if end_T is not None else None,
        "peak_r": round(peak_value, 6),
        "trough_r": round(trough_value, 6),
        "depth_r": round(peak_value - trough_value, 6),
        "recovered": end_index is not None,
        "duration_seconds": (end_T if end_T is not None else last_T) - start_T,
        "recovery_seconds": (end_T - trough_T) if end_T is not None else None,
        "trades": (end_index if end_index is not None
                   else len(points) - 1) - peak_index,
    }


def drawdown_summary(episodes: Sequence[dict[str, Any]],
                     in_drawdown: Optional[float]) -> dict[str, Any]:
    """Depth and duration statistics over the episode table."""
    depths = [episode["depth_r"] for episode in episodes]
    durations = [episode["duration_seconds"] for episode in episodes]
    recoveries = [episode["recovery_seconds"] for episode in episodes
                  if episode["recovery_seconds"] is not None]
    unrecovered = [episode for episode in episodes if not episode["recovered"]]
    longest = max(episodes, key=lambda e: e["duration_seconds"],
                  default=None) if episodes else None
    return {
        "source": "derived",
        "episodes_total": len(episodes),
        "unrecovered": len(unrecovered),
        "time_in_drawdown_share": in_drawdown,
        "depth_r": describe(depths),
        "duration_hours": describe([d / H1_SECONDS for d in durations]),
        "recovery_hours": describe([r / H1_SECONDS for r in recoveries]),
        "average_depth_r": (sum(depths) / len(depths)) if depths else None,
        "longest_episode": longest,
    }


# ---------------------------------------------------------------------------
# Rolling statistics
# ---------------------------------------------------------------------------

def rolling_statistics(records: Sequence[Any], *,
                       windows: Sequence[int] = ROLLING_WINDOWS,
                       stride: int = 0) -> dict[str, Any]:
    """Rolling expectancy, win rate, profit factor and per-trade Sharpe.

    `stride` thins the emitted points so a 31,209-trade run does not put three
    full-length series into a JSON artifact; it never changes a computed value,
    only how many of them are kept. Every window's final point is always kept,
    so the series ends where the run ends.
    """
    values = np.asarray([float(record.realized_r) for record in records],
                        dtype=float)
    wins = np.asarray([1.0 if getattr(record, "state", None) == "WIN" else 0.0
                       for record in records], dtype=float)
    stamps = [int(getattr(record, "exit_T", 0) or 0) for record in records]

    out: dict[str, Any] = {
        "source": "derived",
        "order_key": "(exit_T, cycle_index, universe_rank, symbol)",
        "sharpe_definition": "mean(R) / stdev(R) within the window. NOT "
                             "annualised and not a portfolio Sharpe: there is "
                             "no position sizing, trades overlap in time, and "
                             "the unit of the series is one trade.",
        "windows": {},
    }
    for window in windows:
        if values.size < window:
            out["windows"][str(window)] = {
                "window": window, "points": 0,
                "note": f"fewer than {window} scored trades"}
            continue
        step = stride or max(1, values.size // 400)
        series = []
        for end in range(window, values.size + 1, step):
            block = values[end - window:end]
            won = wins[end - window:end]
            series.append({
                "index": end,
                "exit_T": stamps[end - 1],
                "expectancy": float(block.mean()),
                "win_rate": float(won.mean()),
                "profit_factor": _profit_factor(block),
                "sharpe": _sharpe(block),
            })
        if series and series[-1]["index"] != values.size:
            block = values[values.size - window:]
            won = wins[values.size - window:]
            series.append({
                "index": int(values.size),
                "exit_T": stamps[-1],
                "expectancy": float(block.mean()),
                "win_rate": float(won.mean()),
                "profit_factor": _profit_factor(block),
                "sharpe": _sharpe(block),
            })
        out["windows"][str(window)] = {
            "window": window, "stride": step, "points": len(series),
            "series": series,
            "expectancy_range": [min(p["expectancy"] for p in series),
                                 max(p["expectancy"] for p in series)],
            "share_of_windows_positive": (
                sum(1 for p in series if p["expectancy"] > 0) / len(series)),
        }
    return out


def _profit_factor(block: np.ndarray) -> Optional[float]:
    gains = block[block > 0].sum()
    losses = -block[block < 0].sum()
    if losses <= 0:
        return None            # undefined, never infinity
    return float(gains / losses)


def _sharpe(block: np.ndarray) -> Optional[float]:
    if block.size < 2:
        return None
    spread = block.std(ddof=1)
    if spread <= 0:
        return None
    return float(block.mean() / spread)


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------

def _histogram(values: Sequence[float],
               edges: Sequence[float]) -> list[dict[str, Any]]:
    bands: list[dict[str, Any]] = []
    ordered = list(edges)
    bounds = [(-math.inf, ordered[0])]
    bounds += list(zip(ordered, ordered[1:]))
    bounds.append((ordered[-1], math.inf))
    for low, high in bounds:
        inside = [v for v in values if low <= v < high]
        label = (f"<{ordered[0]:g}" if low == -math.inf else
                 f">={ordered[-1]:g}" if high == math.inf else
                 f"[{low:g},{high:g})")
        bands.append({
            "band": label, "n": len(inside),
            "share": (len(inside) / len(values)) if values else None,
            "sum_r": sum(inside) if inside else 0.0,
        })
    return bands


def r_distribution(records: Sequence[Any]) -> dict[str, Any]:
    """The R-multiple distribution, on fixed declared bands."""
    values = [float(record.realized_r) for record in records
              if getattr(record, "realized_r", None) is not None]
    return {
        "source": "derived",
        "edges": list(R_EDGES),
        "summary": describe(values),
        "percentiles": percentiles(values),
        "bands": _histogram(values, R_EDGES),
    }


def duration_distribution(records: Sequence[Any]) -> dict[str, Any]:
    """Trade duration in H1 candles, which are hours."""
    values = [float(record.holding_candles) for record in records
              if getattr(record, "holding_candles", None) is not None]
    return {
        "source": "derived",
        "unit": "H1 candles (= hours)",
        "edges": list(DURATION_EDGES),
        "summary": describe(values),
        "percentiles": percentiles(values),
        "bands": _histogram(values, DURATION_EDGES),
    }


def holding_statistics(records: Sequence[Any]) -> dict[str, Any]:
    """Holding time, split by how the trade ended.

    Winners and losers are separated because a single average over both answers
    no question: a strategy that cuts losses fast and lets winners run has the
    same mean as one that does the reverse.
    """
    def hold(subset: Iterable[Any]) -> list[float]:
        return [float(r.holding_candles) for r in subset
                if getattr(r, "holding_candles", None) is not None]

    everything = list(records)
    wins = [r for r in everything if getattr(r, "state", None) == "WIN"]
    losses = [r for r in everything if getattr(r, "state", None) == "LOSS"]
    return {
        "source": "derived",
        "unit": "H1 candles (= hours)",
        "all": {**describe(hold(everything)), **percentiles(hold(everything))},
        "wins": {**describe(hold(wins)), **percentiles(hold(wins))},
        "losses": {**describe(hold(losses)), **percentiles(hold(losses))},
    }


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

def streaks(records: Sequence[Any]) -> dict[str, Any]:
    """Consecutive win and loss runs along the C-9.8 order.

    The engine reports the two maxima; this reports the whole distribution,
    because "the worst run was 51" and "runs of 20+ happened eleven times" are
    different facts and only the second tells you what to expect.
    """
    runs_win: list[int] = []
    runs_loss: list[int] = []
    current = 0
    current_win: Optional[bool] = None
    for record in records:
        value = getattr(record, "realized_r", None)
        if value is None:
            continue
        won = value > 0
        if current_win is None or won == current_win:
            current += 1
        else:
            (runs_win if current_win else runs_loss).append(current)
            current = 1
        current_win = won
    if current_win is not None and current:
        (runs_win if current_win else runs_loss).append(current)

    def tally(runs: list[int]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for length in runs:
            key = ("1" if length == 1 else "2-3" if length <= 3 else
                   "4-6" if length <= 6 else "7-10" if length <= 10 else
                   "11-20" if length <= 20 else "21+")
            counts[key] = counts.get(key, 0) + 1
        return {"runs": len(runs), "longest": max(runs, default=0),
                "mean_length": (sum(runs) / len(runs)) if runs else None,
                "by_length": dict(sorted(counts.items()))}

    return {
        "source": "derived",
        "note": "a run is decided by the sign of realized_r, so a 0R gap-exit "
                "counts as a loss-side element; the engine's "
                "max_consecutive_* use the same convention",
        "wins": tally(runs_win),
        "losses": tally(runs_loss),
    }


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

def leaderboard(segments: Sequence[dict[str, Any]], *,
                minimum_n: int = LEADERBOARD_MIN_N,
                top: int = 10) -> dict[str, Any]:
    """Best and worst symbols, ranked two ways.

    By **total R** because that is what moved the equity curve, and by
    **expectancy among symbols that clear the sample floor** because that is
    what might generalise. Ranking by expectancy alone across all symbols is the
    classic error: on the official baseline the top ten by expectancy all hold
    twelve trades or fewer and are pure noise.
    """
    rows = []
    for segment in segments:
        n = segment.get("n") or 0
        expectancy = segment.get("expectancy")
        if not n or expectancy is None:
            continue
        rows.append({
            "symbol": segment["key"], "n": n,
            "expectancy": expectancy,
            "win_rate": segment.get("win_rate"),
            "profit_factor": segment.get("profit_factor"),
            "total_r": expectancy * n,
            "inferential": bool(segment.get("inferential")),
        })
    by_total = sorted(rows, key=lambda row: row["total_r"])
    qualified = sorted((row for row in rows if row["n"] >= minimum_n),
                       key=lambda row: row["expectancy"])
    return {
        "source": "derived",
        "basis": "official `symbol` segments; total_r = expectancy * n",
        "minimum_n": minimum_n,
        "symbols_total": len(rows),
        "symbols_qualified": len(qualified),
        "all": sorted(rows, key=lambda row: row["symbol"]),
        "best_by_total_r": list(reversed(by_total[-top:])),
        "worst_by_total_r": by_total[:top],
        "best_by_expectancy_qualified": list(reversed(qualified[-top:])),
        "worst_by_expectancy_qualified": qualified[:top],
        "caution": "expectancy ranking below the sample floor is noise and is "
                   "deliberately not reported as a ranking",
    }


def contributors(segments: Sequence[dict[str, Any]], *,
                 top: int = 10) -> dict[str, Any]:
    """Where the result actually came from, and how concentrated it is."""
    rows = [{"symbol": s["key"], "n": s["n"],
             "total_r": (s.get("expectancy") or 0.0) * (s.get("n") or 0)}
            for s in segments if s.get("n")]
    positive = sorted((r for r in rows if r["total_r"] > 0),
                      key=lambda r: -r["total_r"])
    negative = sorted((r for r in rows if r["total_r"] < 0),
                      key=lambda r: r["total_r"])
    net = sum(r["total_r"] for r in rows)
    return {
        "source": "derived",
        "net_r": net,
        "positive_symbols": len(positive),
        "negative_symbols": len(negative),
        "gross_positive_r": sum(r["total_r"] for r in positive),
        "gross_negative_r": sum(r["total_r"] for r in negative),
        "top_positive": positive[:top],
        "top_negative": negative[:top],
        "concentration": {
            f"top_{k}": {
                "positive_r": sum(r["total_r"] for r in positive[:k]),
                "negative_r": sum(r["total_r"] for r in negative[:k]),
            } for k in (1, 5, 10, 25)
        },
    }
