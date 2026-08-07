"""Study 5 — the drawdown regime. M16.

The question: **what does the strategy's worst period look like from the
inside?**

The official baseline has one drawdown that dominates everything else. The
useful question is not how deep it was — the metrics engine already says — but
whether it has a *composition*: a set of symbols, a setup, a direction, a
session or a market state that the rest of the run does not share. If the
profile inside the drawdown matches the profile outside it, the drawdown is
variance and there is nothing to fix. If it differs, that difference is the
failure regime.

## How the comparison is framed

Every dimension is reported **inside versus outside**, with the share in each
and the lift between them. A category that holds 8% of trades outside and 30%
inside is over-represented by a factor of ~3.75, and that lift is the signal.

## What this is not

It is not a test. Trades inside a drawdown are selected *because* they lost, so
every loss-associated attribute will be over-represented by construction —
`direction=LONG` will look damning simply because LONG lost overall. The lift
column is a lens for finding candidates, and the honest reading is written into
the payload so nobody mistakes selection for causation. The tested versions of
these questions live in Studies 1 and 3.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backtest.analytics.studies.common import (
    LabelledTrade,
    Study,
    group_by,
    summarise,
)

__all__ = ["run"]


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _session(trade: LabelledTrade) -> Optional[str]:
    from backtest.metrics.segments import SESSIONS

    hour = _utc(trade.record.boundary).hour
    for label, start, end in SESSIONS:
        if start <= hour < end:
            return label
    return None


_DIMENSIONS: tuple[tuple[str, Callable[[LabelledTrade], Optional[str]]], ...] = (
    ("symbol", lambda t: t.record.symbol),
    ("entry_mode", lambda t: t.record.entry_mode),
    ("direction", lambda t: t.record.direction),
    ("alert_tier", lambda t: t.record.alert_tier),
    ("score", lambda t: (None if t.record.score is None
                         else f"{int(t.record.score):02d}")),
    ("session", _session),
    ("regime", lambda t: t.labels.get("regime")),
    ("location", lambda t: t.labels.get("location")),
    ("exit_reason", lambda t: t.record.exit_reason),
    ("weekly_bias", lambda t: t.labels.get("weekly_bias")),
    ("d1_bias", lambda t: t.labels.get("d1_bias")),
)


def run(trades: list[LabelledTrade], metrics: dict) -> Study:
    curve = (metrics.get("global") or {}).get("equity") or {}
    start = curve.get("max_drawdown_start_exit_T")
    end = curve.get("max_drawdown_end_exit_T")

    if start is None:
        payload = {"source": "derived", "study": "drawdown_analysis",
                   "note": "the bundle records no drawdown window"}
        return Study("drawdown_analysis", "Drawdown regime", payload,
                     "# Drawdown regime\n\nNo drawdown window recorded.\n")

    # The window is closed at the trough where the official maximum was
    # measured; `max_drawdown_end_exit_T` is that trough, not a recovery.
    inside = [t for t in trades
              if t.record.exit_T is not None
              and start <= int(t.record.exit_T) <= end]
    outside = [t for t in trades
               if t.record.exit_T is not None
               and not (start <= int(t.record.exit_T) <= end)]

    payload = {
        "source": "derived",
        "study": "drawdown_analysis",
        "question": "what does the worst period look like from the inside?",
        "window": {
            "start_exit_T": start, "end_exit_T": end,
            "start_iso": _utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_iso": _utc(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": (end - start) / 86400.0,
            "depth_r": curve.get("max_drawdown_r"),
        },
        "method": {
            "framing": "inside versus outside the window, by share and lift",
            "caution": "trades inside a drawdown are selected because they "
                       "lost, so every loss-associated attribute is "
                       "over-represented by construction. Lift finds "
                       "candidates; it does not establish cause. The tested "
                       "versions live in the scoring and direction studies.",
        },
        "inside": summarise(inside),
        "outside": summarise(outside),
        "composition": {
            name: _compare(inside, outside, key)
            for name, key in _DIMENSIONS
        },
        "worst_symbols_inside": _worst(inside),
        "monthly_inside": _monthly(inside),
    }
    return Study("drawdown_analysis", "Drawdown regime", payload,
                 _markdown(payload))


def _compare(inside: list[LabelledTrade], outside: list[LabelledTrade],
             key: Callable[[LabelledTrade], Optional[str]]
             ) -> list[dict[str, Any]]:
    def shares(members):
        counts: dict[str, int] = {}
        for trade in members:
            name = key(trade)
            if name is None:
                continue
            counts[str(name)] = counts.get(str(name), 0) + 1
        total = sum(counts.values())
        return counts, total

    inside_counts, inside_total = shares(inside)
    outside_counts, outside_total = shares(outside)
    rows = []
    for name in sorted(set(inside_counts) | set(outside_counts)):
        in_n = inside_counts.get(name, 0)
        out_n = outside_counts.get(name, 0)
        in_share = (in_n / inside_total) if inside_total else None
        out_share = (out_n / outside_total) if outside_total else None
        lift = ((in_share / out_share)
                if (in_share is not None and out_share) else None)
        members = [t for t in inside if key(t) == name]
        rows.append({
            "key": name, "inside_n": in_n, "outside_n": out_n,
            "inside_share": in_share, "outside_share": out_share,
            "lift": lift,
            "inside_net_r": summarise(members)["net_r"],
        })
    rows.sort(key=lambda row: (row["lift"] is None, -(row["lift"] or 0.0)))
    return rows


def _worst(inside: list[LabelledTrade]) -> list[dict[str, Any]]:
    rows = group_by(inside, lambda t: t.record.symbol)
    rows.sort(key=lambda row: row["net_r"])
    return rows[:15]


def _monthly(inside: list[LabelledTrade]) -> list[dict[str, Any]]:
    rows = group_by(inside, lambda t: _utc(t.record.exit_T).strftime("%Y-%m")
                    if t.record.exit_T else None)
    rows.sort(key=lambda row: row["key"])
    return rows


def _markdown(payload: dict[str, Any]) -> str:
    if "window" not in payload:
        return "# Drawdown regime\n\nNo drawdown window recorded.\n"
    window = payload["window"]
    inside, outside = payload["inside"], payload["outside"]
    out = ["# Drawdown regime\n\n", f"> {payload['question']}\n\n",
           f"> **{payload['method']['caution']}**\n\n",
           f"## Window\n\n{window['start_iso'][:10]} → "
           f"{window['end_iso'][:10]} · {window['days']:.0f} days · depth "
           f"{window['depth_r']:.1f} R\n\n",
           f"Inside: n {inside['n']}, net R {inside['net_r']:+.1f}, avg R "
           f"{_num(inside['avg_r'])}, win {_num(inside['win_rate'])}\n\n",
           f"Outside: n {outside['n']}, net R {outside['net_r']:+.1f}, avg R "
           f"{_num(outside['avg_r'])}, win {_num(outside['win_rate'])}\n\n"]

    for name, rows in payload["composition"].items():
        shown = [row for row in rows if row["inside_n"] >= 20][:12]
        if not shown:
            continue
        out.append(f"### `{name}` — over-representation inside\n\n")
        out.append("| Value | inside n | inside share | outside share | lift | "
                   "inside net R |\n")
        out.append("|---|---:|---:|---:|---:|---:|\n")
        for row in shown:
            out.append(f"| `{row['key']}` | {row['inside_n']} | "
                       f"{_pct(row['inside_share'])} | "
                       f"{_pct(row['outside_share'])} | {_num(row['lift'], 2)} |"
                       f" {row['inside_net_r']:+.1f} |\n")
        out.append("\n")

    out.append("### Worst symbols inside the window\n\n")
    out.append("| Symbol | n | net R | avg R | win |\n|---|---:|---:|---:|---:|\n")
    for row in payload["worst_symbols_inside"]:
        out.append(f"| `{row['key']}` | {row['n']} | {row['net_r']:+.1f} | "
                   f"{_num(row['avg_r'])} | {_num(row['win_rate'])} |\n")

    out.append("\n### Month by month inside the window\n\n")
    out.append("| Month | n | net R | avg R | win |\n|---|---:|---:|---:|---:|\n")
    for row in payload["monthly_inside"]:
        out.append(f"| {row['key']} | {row['n']} | {row['net_r']:+.1f} | "
                   f"{_num(row['avg_r'])} | {_num(row['win_rate'])} |\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"
