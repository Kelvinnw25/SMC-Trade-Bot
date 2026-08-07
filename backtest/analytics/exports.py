"""CSV exports — M15.

## Why CSV and nothing else

A research workbench has to hand its numbers to whatever the researcher already
uses. CSV is the only format every spreadsheet, notebook and plotting tool reads
without a library, and writing it needs nothing beyond `csv` from the standard
library. No images are produced: a rendered chart bakes in a scale, a window and
a colour choice, and a reader who disagrees with any of them cannot undo it. The
series are exported so the reader can make their own.

## Determinism

`csv.writer` with `lineterminator="\\n"`, explicit column order, and rows emitted
in a sorted or path order — never a dict iteration that happens to be stable.
Two runs over one bundle produce byte-identical files.

## Provenance

Every file gets the same first two comment lines: the `run_id` and the
`metrics_hash` the numbers were read from. A CSV that escapes into a notebook and
comes back six months later can still be traced to the bundle that produced it,
which a bare table cannot.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Optional, Sequence

__all__ = ["CHARTS_DIRNAME", "EXPORTS", "write_csvs"]

CHARTS_DIRNAME = "charts"

#: Filename → the block it is drawn from. Declared so the set of artifacts is a
#: fact about this module rather than a consequence of control flow.
EXPORTS = (
    "equity.csv", "drawdown.csv", "monthly.csv", "yearly.csv", "quarterly.csv",
    "session.csv", "symbol.csv", "hypotheses.csv", "rolling.csv",
    "r_distribution.csv", "streaks.csv",
)


def write_csvs(report: Any, out_dir: Path | str) -> dict[str, Path]:
    """Emit every CSV under `out_dir`. Creates the directory; touches nothing
    outside it."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    identity = report.identity
    header = (f"run_id={identity.get('run_id')}",
              f"metrics_hash={identity.get('metrics_hash')}")

    written: dict[str, Path] = {}
    for name, (columns, rows) in _tables(report).items():
        path = out / name
        path.write_text(_render(header, columns, rows), encoding="utf-8")
        written[name] = path
    return written


def _render(header: Sequence[str], columns: Sequence[str],
            rows: Sequence[Sequence[Any]]) -> str:
    buffer = io.StringIO()
    for line in header:
        buffer.write(f"# {line}\n")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()


def _segment_rows(segments: Sequence[dict[str, Any]]) -> list[list[Any]]:
    return [[s["key"], s["n"], s["win_rate"], s["expectancy"],
             s["profit_factor"], s["payoff_ratio"], s["ci_low"], s["ci_high"],
             s["inferential"]] for s in segments]


_SEGMENT_COLUMNS = ("segment", "n", "win_rate", "expectancy", "profit_factor",
                    "payoff_ratio", "ci_low", "ci_high", "inferential")

_PERIOD_COLUMNS = ("period", "n", "net_r", "avg_r", "win_rate")


def _tables(report: Any) -> dict[str, tuple[Sequence[str],
                                            Sequence[Sequence[Any]]]]:
    equity = report.equity
    calendar = report.calendar
    sessions = report.sessions
    research = report.research

    series = (equity.get("curve") or {}).get("series") or []
    equity_rows = [[index, stamp, value]
                   for index, (stamp, value) in enumerate(series, 1)]

    episodes = (equity.get("drawdown_episodes") or {}).get("all") or []
    drawdown_rows = [
        [index, e["depth_r"], e["start_iso"], e["trough_iso"],
         e["end_iso"], e["duration_seconds"] / 3600.0,
         (e["recovery_seconds"] / 3600.0
          if e["recovery_seconds"] is not None else None),
         e["recovered"], e["trades"]]
        for index, e in enumerate(episodes, 1)]

    monthly = (calendar.get("equity_calendar") or {}).get("monthly") or []
    yearly = (calendar.get("equity_calendar") or {}).get("yearly") or []
    period_rows = lambda rows: [[r["key"], r["n"], r["net_r"], r["avg_r"],
                                 r["win_rate"]] for r in rows]

    symbols = (report.symbols.get("leaderboard") or {})
    symbol_rows = sorted(
        ([row["symbol"], row["n"], row["win_rate"], row["expectancy"],
          row["profit_factor"], row["total_r"], row["inferential"]]
         for row in _all_symbols(symbols)),
        key=lambda row: (-(row[5] or 0.0), row[0]))

    hypothesis_rows = []
    for result in research.get("results") or []:
        for metric in ("expectancy", "win_rate"):
            block = result[metric]
            hypothesis_rows.append([
                result["name"], metric, block["left"]["label"],
                block["right"]["label"], block["left"]["n"],
                block["right"]["n"], block["left"]["value"],
                block["right"]["value"], block["effect"], block["cohens_d"],
                (block["welch_ci"] or [None, None])[0],
                (block["welch_ci"] or [None, None])[1],
                (block["bootstrap_ci"] or [None, None])[0],
                (block["bootstrap_ci"] or [None, None])[1],
                block["p_value"], block["p_adjusted"], block["significant"],
                block["inferential"]])

    rolling_rows = []
    for window, block in sorted((report.rolling.get("windows") or {}).items(),
                                key=lambda item: int(item[0])):
        for point in block.get("series") or []:
            rolling_rows.append([window, point["index"], point["exit_T"],
                                 point["expectancy"], point["win_rate"],
                                 point["profit_factor"], point["sharpe"]])

    bands = (report.distributions.get("r_multiple") or {}).get("bands") or []
    band_rows = [[b["band"], b["n"], b["share"], b["sum_r"]] for b in bands]

    streak_rows = []
    for side in ("wins", "losses"):
        block = (report.distributions.get("streaks") or {}).get(side) or {}
        for length, count in (block.get("by_length") or {}).items():
            streak_rows.append([side, length, count])

    return {
        "equity.csv": (("trade_index", "exit_T", "cumulative_r"), equity_rows),
        "drawdown.csv": (("episode", "depth_r", "start", "trough", "end",
                          "duration_hours", "recovery_hours", "recovered",
                          "trades"), drawdown_rows),
        "monthly.csv": (_PERIOD_COLUMNS, period_rows(monthly)),
        "yearly.csv": (_PERIOD_COLUMNS, period_rows(yearly)),
        "quarterly.csv": (_SEGMENT_COLUMNS, _segment_rows(
            (calendar.get("quarterly") or {}).get("segments") or [])),
        "session.csv": (_SEGMENT_COLUMNS, _segment_rows(
            (sessions.get("by_session") or {}).get("segments") or [])),
        "symbol.csv": (("symbol", "n", "win_rate", "expectancy",
                        "profit_factor", "total_r", "inferential"),
                       symbol_rows),
        "hypotheses.csv": (("hypothesis", "metric", "left", "right", "left_n",
                            "right_n", "left_value", "right_value", "effect",
                            "cohens_d", "welch_ci_low", "welch_ci_high",
                            "bootstrap_ci_low", "bootstrap_ci_high", "p_value",
                            "p_adjusted", "significant", "inferential"),
                           hypothesis_rows),
        "rolling.csv": (("window", "trade_index", "exit_T", "expectancy",
                         "win_rate", "profit_factor", "sharpe"), rolling_rows),
        "r_distribution.csv": (("band", "n", "share", "sum_r"), band_rows),
        "streaks.csv": (("side", "run_length", "count"), streak_rows),
    }


def _all_symbols(leaderboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every symbol the leaderboard saw, de-duplicated across its four rankings.

    The leaderboard reports slices; the CSV reports the population, because a
    spreadsheet's whole purpose is to let the reader re-sort it themselves.
    """
    seen: dict[str, dict[str, Any]] = {}
    for key in ("best_by_total_r", "worst_by_total_r",
                "best_by_expectancy_qualified",
                "worst_by_expectancy_qualified", "all"):
        for row in leaderboard.get(key) or []:
            seen.setdefault(row["symbol"], row)
    return list(seen.values())
