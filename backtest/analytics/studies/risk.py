"""Study 4 — risk and reward. M16.

The question: **why does wide RR underperform?** The baseline says trades with a
structural RR of 3 or more lose 0.12 R more than narrower ones, and that the
gap survives correction.

Four hypotheses were proposed, and each makes a different, checkable prediction:

| Hypothesis | Prediction that would confirm it |
|---|---|
| targets too ambitious | MFE rarely reaches the target; win rate falls faster than payoff rises |
| entries too late | fills land far from the planned level; MAE is large immediately |
| no follow-through | MFE clusters just short of target across all RR bands |
| stop placement | MAE exceeds the stop on trades that later would have won |

They are not mutually exclusive and the data can support several. What the study
does is measure each prediction and say which are consistent with the evidence —
it does not pick a winner where the evidence does not.

## The decisive quantity

`mfe_r` is how far a trade went in its favour, in units of its own risk. A
target at RR 3 is reachable exactly when `mfe_r >= 3`. So the **target
attainment rate** — the share of trades whose MFE reached their own structural
RR — separates "the target was never reachable" from "the target was reachable
and we did not take it". No other quantity in the bundle answers that.
"""
from __future__ import annotations

from typing import Any, Optional

from backtest.analytics.stats import describe, percentiles
from backtest.analytics.studies.common import (
    LabelledTrade,
    Study,
    group_by,
    summarise,
)

__all__ = ["RR_BANDS", "run"]

#: Declared RR bands, matching the metrics engine's `RR_EDGES` so the two
#: tables can be read together.
RR_BANDS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0)


def _band(value: Optional[float], edges=RR_BANDS) -> Optional[str]:
    if value is None:
        return None
    number = float(value)
    if number < edges[0]:
        return f"<{edges[0]:g}"
    for low, high in zip(edges, edges[1:]):
        if low <= number < high:
            return f"[{low:g},{high:g})"
    return f">={edges[-1]:g}"


def _atr(trade: LabelledTrade) -> Optional[float]:
    value = trade.labels.get("atr_last")
    return float(value) if value else None


def run(trades: list[LabelledTrade], metrics: dict) -> Study:
    by_band = _by_rr_band(trades)
    payload = {
        "source": "derived",
        "study": "risk_analysis",
        "question": "why does wide RR underperform?",
        "method": {
            "key_quantity": "mfe_r — how far a trade went in its favour, in "
                            "units of its own risk. A target at RR k was "
                            "reachable exactly when mfe_r >= k.",
            "note": "the four hypotheses are not mutually exclusive; each is "
                    "reported against its own prediction",
        },
        "by_rr_band": by_band,
        "target_attainment": _attainment(trades),
        "excursion": _excursion(trades),
        "distances": _distances(trades),
        "timeouts": _timeouts(trades),
        "hypotheses": _weigh(by_band, _attainment(trades), _excursion(trades),
                             _distances(trades)),
    }
    return Study("risk_analysis", "Risk and reward", payload,
                 _markdown(payload))


def _by_rr_band(trades: list[LabelledTrade]) -> list[dict[str, Any]]:
    rows = group_by(trades, lambda t: _band(t.record.structural_rr))
    order = {f"<{RR_BANDS[0]:g}": -1, f">={RR_BANDS[-1]:g}": 99}
    for index, (low, high) in enumerate(zip(RR_BANDS, RR_BANDS[1:])):
        order[f"[{low:g},{high:g})"] = index
    for row in rows:
        members = [t for t in trades
                   if _band(t.record.structural_rr) == row["key"]]
        mfe = [float(t.record.mfe_r) for t in members
               if t.record.mfe_r is not None]
        mae = [float(t.record.mae_r) for t in members
               if t.record.mae_r is not None]
        reached = [t for t in members
                   if t.record.mfe_r is not None
                   and t.record.structural_rr is not None
                   and float(t.record.mfe_r) >= float(t.record.structural_rr)]
        row["avg_mfe_r"] = (sum(mfe) / len(mfe)) if mfe else None
        row["avg_mae_r"] = (sum(mae) / len(mae)) if mae else None
        row["target_attainment"] = (len(reached) / len(members)
                                    if members else None)
        row["avg_structural_rr"] = (
            sum(float(t.record.structural_rr) for t in members
                if t.record.structural_rr is not None) / len(members))
    rows.sort(key=lambda row: order.get(row["key"], 50))
    return rows


def _attainment(trades: list[LabelledTrade]) -> dict[str, Any]:
    """Did the market ever offer the target?

    `reachable` counts trades whose MFE met their own structural RR. `taken` is
    the share that actually exited at TP. The gap between the two is the part of
    the loss that is *not* the market's fault.
    """
    reachable = taken = total = 0
    shortfalls: list[float] = []
    for trade in trades:
        rr = trade.record.structural_rr
        mfe = trade.record.mfe_r
        if rr is None or mfe is None:
            continue
        total += 1
        if float(mfe) >= float(rr):
            reachable += 1
        else:
            shortfalls.append(float(rr) - float(mfe))
        if trade.record.exit_reason in ("TP", "GAP_TP"):
            taken += 1
    return {
        "source": "derived",
        "n": total,
        "reachable": reachable,
        "reachable_share": (reachable / total) if total else None,
        "exited_at_target": taken,
        "taken_share": (taken / total) if total else None,
        # Can be negative, and that is informative rather than a bug: a GAP_TP
        # exits at the target on a candle that opened through it, so the trade
        # is paid without MFE ever registering a touch at RR. A negative value
        # therefore counts gap-through wins, not missed opportunities.
        "reachable_minus_taken": reachable - taken,
        "gap_paid_without_mfe_touch": max(0, taken - reachable),
        "unrealised_reachable": max(0, reachable - taken),
        "shortfall_r": describe(shortfalls),
        "shortfall_percentiles": percentiles(shortfalls),
        "reading": "reachable_share is the share of trades the market did "
                   "offer the target to. A low value supports 'targets too "
                   "ambitious'; a high value with a much lower taken_share "
                   "would instead point at exit mechanics. When taken exceeds "
                   "reachable the difference is gap-through fills, which are "
                   "paid at the target without MFE recording a touch.",
    }


def _excursion(trades: list[LabelledTrade]) -> dict[str, Any]:
    mfe = [float(t.record.mfe_r) for t in trades if t.record.mfe_r is not None]
    mae = [float(t.record.mae_r) for t in trades if t.record.mae_r is not None]
    beyond_stop = sum(1 for value in mae if value < -1.0)
    winners_mae = [float(t.record.mae_r) for t in trades
                   if t.record.mae_r is not None
                   and getattr(t.record, "state", None) == "WIN"]
    return {
        "source": "derived",
        "mfe_r": {**describe(mfe), **percentiles(mfe)},
        "mae_r": {**describe(mae), **percentiles(mae)},
        "mae_beyond_stop": beyond_stop,
        "mae_beyond_stop_share": (beyond_stop / len(mae)) if mae else None,
        "winner_mae_r": {**describe(winners_mae), **percentiles(winners_mae)},
        "reading": "MAE past -1R is expected: the excursion is measured on the "
                   "candle that triggered the stop, and a wick overshoots. What "
                   "matters is the winners' MAE — how much heat a trade that "
                   "eventually worked had to take.",
    }


def _distances(trades: list[LabelledTrade]) -> dict[str, Any]:
    """Stop and target distance in ATR — the scale on which symbols compare."""
    stops, targets, slips = [], [], []
    for trade in trades:
        atr = _atr(trade)
        if not atr:
            continue
        entry, stop, target = (trade.record.entry_price, trade.record.sl,
                               trade.record.tp)
        fill = trade.record.fill_price
        if entry is not None and stop is not None:
            stops.append(abs(entry - stop) / atr)
        if entry is not None and target is not None:
            targets.append(abs(target - entry) / atr)
        if entry is not None and fill is not None:
            slips.append(abs(fill - entry) / atr)
    return {
        "source": "derived",
        "unit": "ATR at signal time",
        "stop_distance": {**describe(stops), **percentiles(stops)},
        "target_distance": {**describe(targets), **percentiles(targets)},
        "entry_slippage": {**describe(slips), **percentiles(slips)},
        "reading": "a very small stop distance in ATR means the risk unit is "
                   "smaller than one candle's noise, which makes a stop-out "
                   "close to random and inflates R when it does not happen",
    }


def _timeouts(trades: list[LabelledTrade]) -> dict[str, Any]:
    total = len(trades)
    counts: dict[str, int] = {}
    for trade in trades:
        reason = trade.record.exit_reason or "NONE"
        counts[reason] = counts.get(reason, 0) + 1
    timeout = counts.get("MAX_HOLDING", 0) + counts.get("END_OF_DATA", 0)
    by_band = []
    for row in group_by(trades, lambda t: _band(t.record.structural_rr)):
        members = [t for t in trades
                   if _band(t.record.structural_rr) == row["key"]]
        hits = sum(1 for t in members
                   if t.record.exit_reason in ("MAX_HOLDING", "END_OF_DATA"))
        by_band.append({"band": row["key"], "n": row["n"], "timeouts": hits,
                        "rate": hits / row["n"] if row["n"] else None})
    return {
        "source": "derived",
        "exit_reason_counts": dict(sorted(counts.items())),
        "timeouts": timeout,
        "timeout_rate": (timeout / total) if total else None,
        "by_rr_band": by_band,
    }


def _weigh(by_band, attainment, excursion, distances) -> list[dict[str, Any]]:
    """Score each hypothesis against its own prediction.

    `supported` is a mechanical read of the numbers, not an opinion: each
    hypothesis declares a threshold in advance and the code checks it.
    """
    wide = [row for row in by_band
            if row["key"] in ("[3,4)", "[4,6)", ">=6")]
    narrow = [row for row in by_band
              if row["key"] in ("[1,1.5)", "[1.5,2)", "[2,2.5)")]

    def weighted(rows, field):
        total = sum(row["n"] for row in rows)
        if not total:
            return None
        return sum((row[field] or 0.0) * row["n"] for row in rows) / total

    wide_attain = weighted(wide, "target_attainment")
    narrow_attain = weighted(narrow, "target_attainment")
    slip = distances["entry_slippage"].get("p50")
    winner_mae = excursion["winner_mae_r"].get("mean")

    return [
        {
            "hypothesis": "targets too ambitious",
            "prediction": "wide-RR targets are reached far less often than "
                          "narrow ones",
            "measured": {"wide_attainment": wide_attain,
                         "narrow_attainment": narrow_attain},
            "supported": (wide_attain is not None and narrow_attain is not None
                          and wide_attain < narrow_attain * 0.6),
        },
        {
            "hypothesis": "entries too late",
            "prediction": "fills land far from the planned entry (median "
                          "slippage above 0.25 ATR)",
            "measured": {"median_entry_slippage_atr": slip},
            "supported": (slip is not None and slip > 0.25),
        },
        {
            "hypothesis": "market does not follow through",
            "prediction": "attainment is low in every band, not just the wide "
                          "ones",
            "measured": {"overall_attainment": attainment["reachable_share"]},
            "supported": (attainment["reachable_share"] is not None
                          and attainment["reachable_share"] < 0.5),
        },
        {
            "hypothesis": "stop placement",
            "prediction": "winners routinely take heat close to their stop "
                          "(mean winner MAE worse than -0.5R)",
            "measured": {"winner_mean_mae_r": winner_mae},
            "supported": (winner_mae is not None and winner_mae < -0.5),
        },
    ]


def _markdown(payload: dict[str, Any]) -> str:
    out = ["# Risk and reward\n\n", f"> {payload['question']}\n\n",
           f"> {payload['method']['key_quantity']}\n\n"]

    out.append("## Hypotheses weighed\n\n")
    out.append("| Hypothesis | prediction | measured | supported |\n")
    out.append("|---|---|---|---|\n")
    for row in payload["hypotheses"]:
        measured = ", ".join(f"{k} {_num(v)}"
                             for k, v in row["measured"].items())
        out.append(f"| **{row['hypothesis']}** | {row['prediction']} | "
                   f"{measured} | {'**yes**' if row['supported'] else 'no'} |\n")

    attainment = payload["target_attainment"]
    out.append(f"\n## Target attainment\n\n"
               f"{attainment['reachable']} of {attainment['n']} trades "
               f"({_pct(attainment['reachable_share'])}) saw their MFE reach "
               f"their own structural RR. {attainment['exited_at_target']} "
               f"({_pct(attainment['taken_share'])}) actually exited at target: "
               f"{attainment['unrealised_reachable']} were reachable and not "
               f"taken, and {attainment['gap_paid_without_mfe_touch']} were "
               f"paid by a gap through the target without MFE recording a "
               f"touch.\n\n"
               f"> {attainment['reading']}\n\n")

    out.append("## By structural RR band\n\n")
    out.append("| Band | n | avg R | net R | win | avg MFE | avg MAE | "
               "attainment |\n")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for row in payload["by_rr_band"]:
        out.append(f"| `{row['key']}` | {row['n']} | {_num(row['avg_r'])} | "
                   f"{_num(row['net_r'], 1)} | {_num(row['win_rate'])} | "
                   f"{_num(row['avg_mfe_r'])} | {_num(row['avg_mae_r'])} | "
                   f"{_pct(row['target_attainment'])} |\n")

    excursion = payload["excursion"]
    out.append(f"\n## Excursion\n\n> {excursion['reading']}\n\n")
    out.append("| Series | mean | p05 | p50 | p95 | max |\n")
    out.append("|---|---:|---:|---:|---:|---:|\n")
    for name in ("mfe_r", "mae_r", "winner_mae_r"):
        block = excursion[name]
        out.append(f"| `{name}` | {_num(block.get('mean'))} | "
                   f"{_num(block.get('p5'))} | {_num(block.get('p50'))} | "
                   f"{_num(block.get('p95'))} | {_num(block.get('max'))} |\n")
    out.append(f"\nMAE beyond the stop: {excursion['mae_beyond_stop']} "
               f"({_pct(excursion['mae_beyond_stop_share'])})\n")

    distances = payload["distances"]
    out.append(f"\n## Distances ({distances['unit']})\n\n"
               f"> {distances['reading']}\n\n")
    out.append("| Series | mean | p05 | p25 | p50 | p75 | p95 |\n")
    out.append("|---|---:|---:|---:|---:|---:|---:|\n")
    for name in ("stop_distance", "target_distance", "entry_slippage"):
        block = distances[name]
        out.append(f"| `{name}` | {_num(block.get('mean'))} | "
                   f"{_num(block.get('p5'))} | {_num(block.get('p25'))} | "
                   f"{_num(block.get('p50'))} | {_num(block.get('p75'))} | "
                   f"{_num(block.get('p95'))} |\n")

    timeouts = payload["timeouts"]
    out.append(f"\n## Timeouts\n\n{timeouts['timeouts']} trades "
               f"({_pct(timeouts['timeout_rate'])}) hit the holding cap or ran "
               f"out of data.\n\n")
    out.append("| Band | n | timeouts | rate |\n|---|---:|---:|---:|\n")
    for row in timeouts["by_rr_band"]:
        out.append(f"| `{row['band']}` | {row['n']} | {row['timeouts']} | "
                   f"{_pct(row['rate'])} |\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.{places}f}"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"
