"""Study 2 — setup quality. M16.

The question: **what separates winners from losers inside each setup?**

The two execution modes are analysed separately, because they are different
trades: CONTINUATION enters on a micro-pullback inside a displacement leg,
RETRACEMENT enters at an order block. A quality dimension that matters for one
need not matter for the other, and pooling them would average the two answers
into a third that describes neither.

## The winner/loser profile

For each setup, every quality dimension is summarised twice — once over the
trades that won, once over the trades that lost — and the gap is reported. This
is deliberately *descriptive*: a profile difference is not a test, it is a place
to look. The tested version of the same question is the factor family, which is
Study 1's job and carries the correction.

Where a dimension is continuous (pullback depth, OB distance, ATR) it is
bucketed on declared edges, never on quantiles of this run's data.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backtest.analytics.studies.common import (
    LabelledTrade,
    Study,
    group_by,
    summarise,
)

__all__ = ["run"]

#: Declared bucket edges. Fixed so two runs stay comparable.
_OB_RANK_EDGES = (1, 2, 3, 5, 9)
_OB_COUNT_EDGES = (1, 2, 4, 8)
_RECLAIM_EDGES = (0.0, 0.25, 0.5, 0.75, 1.0)
_ATR_PCT_EDGES = (0.0, 0.002, 0.005, 0.01, 0.02, 0.05)
_STOP_ATR_EDGES = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)


def _bucket(value: Optional[float], edges) -> Optional[str]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < edges[0]:
        return f"<{edges[0]:g}"
    for low, high in zip(edges, edges[1:]):
        if low <= number < high:
            return f"[{low:g},{high:g})"
    return f">={edges[-1]:g}"


def _stop_atr(trade: LabelledTrade) -> Optional[float]:
    """Stop distance measured in ATR — the only scale on which a stop from one
    symbol is comparable with a stop from another."""
    atr = trade.labels.get("atr_last")
    if not atr:
        return None
    entry, stop = trade.record.entry_price, trade.record.sl
    if entry is None or stop is None:
        return None
    return abs(entry - stop) / float(atr)


def _target_atr(trade: LabelledTrade) -> Optional[float]:
    atr = trade.labels.get("atr_last")
    if not atr:
        return None
    entry, target = trade.record.entry_price, trade.record.tp
    if entry is None or target is None:
        return None
    return abs(target - entry) / float(atr)


def _atr_pct(trade: LabelledTrade) -> Optional[float]:
    """ATR as a share of price — volatility on a scale that survives a symbol
    trading at 0.00001 and one trading at 90,000."""
    atr = trade.labels.get("atr_last")
    price = trade.record.entry_price
    if not atr or not price:
        return None
    return float(atr) / float(price)


def _entry_slip_atr(trade: LabelledTrade) -> Optional[float]:
    """How far the fill landed from the planned entry, in ATR.

    A proxy for entry timing: a fill far from the planned level means price was
    already moving when the level was published.
    """
    atr = trade.labels.get("atr_last")
    fill, entry = trade.record.fill_price, trade.record.entry_price
    if not atr or fill is None or entry is None:
        return None
    return abs(fill - entry) / float(atr)


#: Quality dimensions, shared by both setups where they apply. Each entry is
#: (name, description, extractor) with the extractor returning a bucket label.
_DIMENSIONS: tuple[tuple[str, str, Callable[[LabelledTrade], Optional[str]]], ...] = (
    ("ob_rank", "rank of the selected order block from the last candle",
     lambda t: _bucket(t.labels.get("selected_ob_rank"), _OB_RANK_EDGES)),
    ("ob_candidates", "how many order blocks qualified",
     lambda t: _bucket(t.labels.get("ob_candidate_count"), _OB_COUNT_EDGES)),
    ("ob_fresh", "the selected order block was unmitigated",
     lambda t: (None if t.labels.get("selected_ob_fresh") is None
                else str(bool(t.labels.get("selected_ob_fresh"))).lower())),
    ("sweep_reclaim", "strength of the reclaim after the sweep",
     lambda t: _bucket(t.labels.get("sweep_reclaim_strength"), _RECLAIM_EDGES)),
    ("fvg_present", "a fair-value gap supported the entry",
     lambda t: (None if t.labels.get("fvg") is None
                else str(bool(t.labels.get("fvg"))).lower())),
    ("displacement", "a displacement leg was validated",
     lambda t: (None if t.labels.get("displacement") is None
                else str(bool(t.labels.get("displacement"))).lower())),
    ("htf_alignment", "D1 and H4 bias versus the trade direction",
     lambda t: _alignment(t)),
    ("location", "premium / discount / equilibrium at signal time",
     lambda t: t.labels.get("location")),
    ("regime", "the regime label analysis emitted",
     lambda t: t.labels.get("regime")),
    ("stop_atr", "stop distance in ATR",
     lambda t: _bucket(_stop_atr(t), _STOP_ATR_EDGES)),
    ("target_atr", "target distance in ATR",
     lambda t: _bucket(_target_atr(t), _STOP_ATR_EDGES)),
    ("atr_pct", "volatility as a share of price",
     lambda t: _bucket(_atr_pct(t), _ATR_PCT_EDGES)),
    ("entry_slip_atr", "distance from planned entry to fill, in ATR",
     lambda t: _bucket(_entry_slip_atr(t), (0.0, 0.1, 0.25, 0.5, 1.0))),
    ("fill_kind", "how the position was entered",
     lambda t: t.record.fill_kind),
)

#: Continuous quantities profiled directly for the winner/loser gap.
_PROFILE = (
    ("stop_atr", _stop_atr),
    ("target_atr", _target_atr),
    ("atr_pct", _atr_pct),
    ("entry_slip_atr", _entry_slip_atr),
    ("structural_rr", lambda t: t.record.structural_rr),
    ("holding_candles", lambda t: t.record.holding_candles),
    ("mae_r", lambda t: t.record.mae_r),
    ("mfe_r", lambda t: t.record.mfe_r),
    ("score", lambda t: t.record.score),
    ("ob_candidate_count", lambda t: t.labels.get("ob_candidate_count")),
    ("selected_ob_rank", lambda t: t.labels.get("selected_ob_rank")),
)


def _alignment(trade: LabelledTrade) -> Optional[str]:
    d1 = trade.labels.get("d1_bias")
    h4 = trade.labels.get("h4_bias")
    direction = trade.record.direction
    if not d1 or not h4 or not direction:
        return None
    want = "BULLISH" if direction == "LONG" else "BEARISH"
    hits = int(d1 == want) + int(h4 == want)
    return {2: "ALIGNED", 1: "PARTIAL", 0: "OPPOSED"}[hits]


def run(trades: list[LabelledTrade], metrics: dict) -> Study:
    setups = {}
    for mode in ("CONTINUATION", "RETRACEMENT"):
        members = [t for t in trades if t.record.entry_mode == mode]
        setups[mode] = {
            "population": summarise(members),
            "dimensions": {
                name: {"description": description,
                       "buckets": group_by(members, extract)}
                for name, description, extract in _DIMENSIONS
            },
            "winner_loser_profile": _profile(members),
        }

    payload = {
        "source": "derived",
        "study": "setup_quality_analysis",
        "question": "what separates winners from losers inside each setup?",
        "method": {
            "note": "descriptive. A profile gap points at a hypothesis; the "
                    "tested version of the same question is the factor family "
                    "in score_component_analysis, which carries the correction.",
            "bucket_edges": "fixed and declared, never quantiles of this run",
        },
        "setups": setups,
    }
    return Study("setup_quality_analysis", "Setup quality", payload,
                 _markdown(payload))


def _profile(members: list[LabelledTrade]) -> dict[str, Any]:
    """Mean of each continuous quantity among winners and among losers."""
    winners = [t for t in members if getattr(t.record, "state", None) == "WIN"]
    losers = [t for t in members if getattr(t.record, "state", None) == "LOSS"]

    def mean(subset, extract) -> Optional[float]:
        values = [extract(t) for t in subset]
        values = [float(v) for v in values if v is not None]
        return (sum(values) / len(values)) if values else None

    rows = []
    for name, extract in _PROFILE:
        won, lost = mean(winners, extract), mean(losers, extract)
        gap = (won - lost) if (won is not None and lost is not None) else None
        rows.append({"quantity": name, "winners": won, "losers": lost,
                     "gap": gap,
                     "relative_gap": (gap / abs(lost))
                     if (gap is not None and lost) else None})
    rows.sort(key=lambda row: (row["relative_gap"] is None,
                               -abs(row["relative_gap"] or 0.0)))
    return {"winners_n": len(winners), "losers_n": len(losers), "rows": rows}


def _markdown(payload: dict[str, Any]) -> str:
    out = ["# Setup quality\n\n", f"> {payload['question']}\n\n",
           f"> {payload['method']['note']}\n\n"]
    for mode, block in payload["setups"].items():
        population = block["population"]
        out.append(f"## {mode}\n\n")
        out.append(f"n {population['n']} · net R {population['net_r']:+.1f} · "
                   f"avg R {_num(population['avg_r'])} · win "
                   f"{_num(population['win_rate'])}\n\n")

        profile = block["winner_loser_profile"]
        out.append(f"### Winner / loser profile "
                   f"({profile['winners_n']} vs {profile['losers_n']})\n\n")
        out.append("| Quantity | winners | losers | gap | relative |\n")
        out.append("|---|---:|---:|---:|---:|\n")
        for row in profile["rows"]:
            out.append(f"| `{row['quantity']}` | {_num(row['winners'])} | "
                       f"{_num(row['losers'])} | {_num(row['gap'])} | "
                       f"{_pct(row['relative_gap'])} |\n")

        out.append("\n### Quality dimensions\n\n")
        for name, dimension in block["dimensions"].items():
            buckets = [b for b in dimension["buckets"] if b["n"]]
            if not buckets:
                continue
            out.append(f"**`{name}`** — {dimension['description']}\n\n")
            out.append("| Bucket | n | avg R | net R | win | inferential |\n")
            out.append("|---|---:|---:|---:|---:|---|\n")
            for bucket in buckets:
                out.append(f"| `{bucket['key']}` | {bucket['n']} | "
                           f"{_num(bucket['avg_r'])} | "
                           f"{_num(bucket['net_r'], 1)} | "
                           f"{_num(bucket['win_rate'])} | "
                           f"{bucket['inferential']} |\n")
            out.append("\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.{places}f}"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{100 * value:+.1f}%"
