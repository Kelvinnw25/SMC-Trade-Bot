"""Study 3 — direction bias. M16.

The question the baseline forces: SHORT beat LONG by +0.124 R with a Holm-
adjusted p of 0.0000. **Is that a property of the strategy, or of 2023-2026?**

The distinction matters more than the effect. A structural asymmetry — the entry
logic reads bearish structure better than bullish — would be actionable. A
period effect — the window contained more downside follow-through than upside —
would not be, and acting on it would be fitting the strategy to a market that
has already happened.

## How the two are told apart

A structural effect is **stable across partitions**. If SHORT wins in every
year, in every session, in both setups and across score tiers, the asymmetry
does not depend on when you looked. If it concentrates in one year, it is that
year.

So the same comparison is run inside every partition, and what is reported is
the *consistency* of the sign, not just its average. `sign_consistency` is the
share of inferential partitions in which SHORT led — 1.0 means every partition
agreed, 0.5 means the effect is a coin flip once you condition.

No correction is applied across partitions here: these are not independent
questions, they are one question asked repeatedly, and the headline test in
`research` already carries the corrected answer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backtest.analytics.stats import compare_means
from backtest.analytics.studies.common import (
    LabelledTrade,
    Study,
    group_by,
    summarise,
)
from backtest.metrics import MIN_SEGMENT_N

__all__ = ["PARTITIONS", "run"]


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _session(trade: LabelledTrade) -> Optional[str]:
    from backtest.metrics.segments import SESSIONS

    hour = _utc(trade.record.boundary).hour
    for label, start, end in SESSIONS:
        if start <= hour < end:
            return label
    return None


#: The partitions the asymmetry is tested inside. Declared, not discovered.
PARTITIONS: tuple[tuple[str, Callable[[LabelledTrade], Optional[str]]], ...] = (
    ("year", lambda t: _utc(t.record.boundary).strftime("%Y")),
    ("quarter", lambda t: f"{_utc(t.record.boundary).year}-Q"
                          f"{(_utc(t.record.boundary).month - 1) // 3 + 1}"),
    ("session", _session),
    ("entry_mode", lambda t: t.record.entry_mode),
    ("score_tier", lambda t: (None if t.record.score is None else
                              ">=14" if t.record.score >= 14 else "<14")),
    ("regime", lambda t: t.labels.get("regime")),
    ("regime_class", lambda t: _regime_class(t)),
    ("alert_tier", lambda t: t.record.alert_tier),
    ("location", lambda t: t.labels.get("location")),
)

_REGIME_CLASS = {
    "expansion_up": "TRENDING", "expansion_down": "TRENDING",
    "accumulation": "RANGING", "distribution": "RANGING",
    "rotational": "RANGING", "compression": "CHOPPY",
    "neutral": "CHOPPY", "unknown": "CHOPPY",
}


def _regime_class(trade: LabelledTrade) -> Optional[str]:
    return _REGIME_CLASS.get(str(trade.labels.get("regime") or "")) or None


def run(trades: list[LabelledTrade], metrics: dict) -> Study:
    partitions = {}
    for name, key in PARTITIONS:
        cells = {}
        for trade in trades:
            bucket = key(trade)
            if bucket is None or trade.record.direction not in ("LONG", "SHORT"):
                continue
            cells.setdefault(str(bucket), {"LONG": [], "SHORT": []})[
                trade.record.direction].append(float(trade.record.realized_r))

        rows = []
        for bucket in sorted(cells):
            longs, shorts = cells[bucket]["LONG"], cells[bucket]["SHORT"]
            comparison = compare_means(
                f"{name}:{bucket}", "SHORT vs LONG", "SHORT", "LONG",
                shorts, longs, metric="expectancy")
            rows.append({
                "bucket": bucket,
                "long_n": len(longs), "short_n": len(shorts),
                "long_avg_r": (sum(longs) / len(longs)) if longs else None,
                "short_avg_r": (sum(shorts) / len(shorts)) if shorts else None,
                "effect_r": comparison.effect,
                "welch_ci": (list(comparison.welch_ci)
                             if comparison.welch_ci else None),
                "p_value": comparison.p_value,
                "inferential": comparison.inferential,
                "short_leads": (None if comparison.effect is None
                                else comparison.effect > 0),
            })
        inferential = [row for row in rows if row["inferential"]]
        leading = [row for row in inferential if row["short_leads"]]
        partitions[name] = {
            "cells": rows,
            "inferential_cells": len(inferential),
            "cells_where_short_leads": len(leading),
            "sign_consistency": (len(leading) / len(inferential))
            if inferential else None,
        }

    consistencies = [block["sign_consistency"] for block in partitions.values()
                     if block["sign_consistency"] is not None]
    payload = {
        "source": "derived",
        "study": "direction_analysis",
        "question": "is SHORT genuinely better, or only period/regime "
                    "dependent?",
        "method": {
            "logic": "a structural asymmetry is stable across partitions; a "
                     "period effect concentrates in one. `sign_consistency` is "
                     "the share of inferential cells in which SHORT led.",
            "no_correction": "these partitions are one question asked "
                             "repeatedly, not independent hypotheses; the "
                             "corrected headline lives in the hypothesis "
                             "registry",
            "min_cell_n": MIN_SEGMENT_N,
        },
        "headline": _headline(trades),
        "partitions": partitions,
        "overall_sign_consistency": (sum(consistencies) / len(consistencies))
        if consistencies else None,
        "symbols": _symbols(trades),
    }
    return Study("direction_analysis", "Direction bias", payload,
                 _markdown(payload))


def _headline(trades: list[LabelledTrade]) -> dict[str, Any]:
    longs = [float(t.record.realized_r) for t in trades
             if t.record.direction == "LONG"]
    shorts = [float(t.record.realized_r) for t in trades
              if t.record.direction == "SHORT"]
    comparison = compare_means("short_vs_long", "SHORT vs LONG", "SHORT",
                               "LONG", shorts, longs)
    # A study's own output has to be self-describing: this comparison is not
    # routed through the hypothesis registry, so nothing else would fill the
    # reading in and a consumer would receive an empty string.
    import dataclasses

    comparison = dataclasses.replace(
        comparison, interpretation=_headline_reading(comparison))
    return {
        "long": summarise([t for t in trades if t.record.direction == "LONG"]),
        "short": summarise([t for t in trades
                            if t.record.direction == "SHORT"]),
        "comparison": comparison.to_dict(),
    }


def _headline_reading(comparison) -> str:
    """The uncorrected reading of the raw SHORT-vs-LONG gap.

    Says "uncorrected" out loud: the corrected verdict for this same question
    lives in the hypothesis registry, and two numbers that differ because one
    was adjusted must never be quoted as though they were the same test.
    """
    if comparison.left_n == 0 or comparison.right_n == 0:
        return "one direction is absent; the comparison cannot be made"
    effect = comparison.effect or 0.0
    if not comparison.inferential:
        return (f"{effect:+.4f} R, but the smaller side holds "
                f"{min(comparison.left_n, comparison.right_n)} trades — "
                f"described, not tested")
    excluded = (comparison.welch_ci is not None
                and (comparison.welch_ci[0] > 0 or comparison.welch_ci[1] < 0))
    verdict = ("the interval excludes zero" if excluded
               else "the interval contains zero")
    return (f"SHORT is {effect:+.4f} R against LONG and {verdict} "
            f"(uncorrected; the Holm-corrected verdict for this question is in "
            f"the hypothesis registry)")


def _symbols(trades: list[LabelledTrade]) -> dict[str, Any]:
    """Per-symbol direction split, to see whether the asymmetry is broad.

    An effect carried by a handful of symbols is a symbol effect wearing a
    direction costume.
    """
    rows = []
    by_symbol: dict[str, dict[str, list[float]]] = {}
    for trade in trades:
        if trade.record.direction not in ("LONG", "SHORT"):
            continue
        by_symbol.setdefault(trade.record.symbol,
                             {"LONG": [], "SHORT": []})[
            trade.record.direction].append(float(trade.record.realized_r))
    for symbol, sides in by_symbol.items():
        longs, shorts = sides["LONG"], sides["SHORT"]
        if len(longs) < MIN_SEGMENT_N or len(shorts) < MIN_SEGMENT_N:
            continue
        rows.append({
            "symbol": symbol, "long_n": len(longs), "short_n": len(shorts),
            "long_avg_r": sum(longs) / len(longs),
            "short_avg_r": sum(shorts) / len(shorts),
            "effect_r": sum(shorts) / len(shorts) - sum(longs) / len(longs),
        })
    leading = [row for row in rows if row["effect_r"] > 0]
    rows.sort(key=lambda row: -row["effect_r"])
    return {
        "qualified_symbols": len(rows),
        "symbols_where_short_leads": len(leading),
        "share": (len(leading) / len(rows)) if rows else None,
        "top": rows[:10],
        "bottom": rows[-10:],
    }


def _markdown(payload: dict[str, Any]) -> str:
    headline = payload["headline"]
    comparison = headline["comparison"]
    out = ["# Direction bias\n\n", f"> {payload['question']}\n\n",
           f"> {payload['method']['logic']}\n\n",
           f"## Headline\n\nSHORT n {headline['short']['n']} avg R "
           f"{_num(headline['short']['avg_r'])} · LONG n "
           f"{headline['long']['n']} avg R {_num(headline['long']['avg_r'])} · "
           f"effect {_num(comparison['effect'])} R · Welch CI "
           f"{_ci(comparison['welch_ci'])} · bootstrap "
           f"{_ci(comparison['bootstrap_ci'])}\n\n",
           f"**Overall sign consistency across partitions: "
           f"{_num(payload['overall_sign_consistency'])}** — 1.0 means SHORT "
           f"led in every inferential cell of every partition; 0.5 means the "
           f"sign is a coin flip once you condition.\n\n"]

    out.append("## Consistency by partition\n\n")
    out.append("| Partition | inferential cells | SHORT leads | consistency |\n")
    out.append("|---|---:|---:|---:|\n")
    for name, block in payload["partitions"].items():
        out.append(f"| `{name}` | {block['inferential_cells']} | "
                   f"{block['cells_where_short_leads']} | "
                   f"{_num(block['sign_consistency'])} |\n")

    for name, block in payload["partitions"].items():
        out.append(f"\n### `{name}`\n\n")
        out.append("| Bucket | LONG n | SHORT n | LONG avg R | SHORT avg R | "
                   "effect | 95% CI | inferential |\n")
        out.append("|---|---:|---:|---:|---:|---:|---|---|\n")
        for row in block["cells"]:
            out.append(f"| `{row['bucket']}` | {row['long_n']} | "
                       f"{row['short_n']} | {_num(row['long_avg_r'])} | "
                       f"{_num(row['short_avg_r'])} | {_num(row['effect_r'])} |"
                       f" {_ci(row['welch_ci'])} | {row['inferential']} |\n")

    symbols = payload["symbols"]
    out.append(f"\n## Breadth across symbols\n\n"
               f"{symbols['symbols_where_short_leads']} of "
               f"{symbols['qualified_symbols']} symbols with at least 30 trades "
               f"on each side favour SHORT ({_num(symbols['share'])}).\n\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _ci(bounds: Optional[list]) -> str:
    if not bounds or bounds[0] is None:
        return "—"
    mark = " ✓" if (bounds[0] > 0 or bounds[1] < 0) else ""
    return f"[{bounds[0]:+.4f}, {bounds[1]:+.4f}]{mark}"
