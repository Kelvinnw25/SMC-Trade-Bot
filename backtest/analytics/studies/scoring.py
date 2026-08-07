"""Study 1 — scoring model audit. M16.

The question: **which scoring inputs carry information about the outcome?**

The ledger records, for every analysed symbol-cycle, the eight confirmation
booleans that feed `score_signal`, plus the HTF and order-block context that
shapes them. Each is tested the same way — present versus absent, Holm-corrected
across the whole family — and classified by the pre-declared procedure in
`common`. No weight is proposed and none is changed: this study produces the
evidence that a later decision would rest on.

## Why the family is tested together

Twenty factors tested independently at the 5% level produce one false positive
per run on average. Correcting across the family is what makes "this factor is
useful" mean something. It also means a factor can lose its significance when
another is added to the family — which is correct, and is why the family is
declared as a static list rather than assembled from whatever the data offers.

## What this cannot see

A factor's *marginal* contribution given the others. These are one-at-a-time
comparisons, so a factor that only matters in combination will read as neutral,
and two factors that always co-occur will each take credit for the same effect.
The co-occurrence matrix is emitted alongside the verdicts so that a reader can
see which pairs are entangled rather than discovering it later.
"""
from __future__ import annotations

from typing import Any, Optional

from backtest.analytics.studies.common import (
    LabelledTrade,
    Study,
    classify_family,
    group_by,
    summarise,
)

__all__ = ["CONFIRMATIONS", "run"]


def _flag(name: str):
    def predicate(trade: LabelledTrade) -> Optional[bool]:
        value = trade.labels.get(name)
        return None if value is None else bool(value)
    return predicate


#: The score inputs, as the ledger groups them
#: ([signal_ledger.py:118](v2/domain/signal_ledger.py#L118)) plus the context
#: fields that gate them. Static: the family size is a property of the strategy,
#: not of the run being analysed.
CONFIRMATIONS: tuple[tuple[str, str, Any], ...] = (
    ("sweep", "a liquidity sweep was detected", _flag("sweep")),
    ("fvg", "a fair-value gap was present", _flag("fvg")),
    ("displacement", "a displacement leg was validated",
     _flag("displacement")),
    ("fresh_ob", "the order block was unmitigated", _flag("fresh_ob")),
    ("volume_spike", "volume expansion at the signal candle",
     _flag("volume_spike")),
    ("target_clear", "the path to target was free of opposing structure",
     _flag("target_clear")),
    ("htf_context_clean", "no conflicting higher-timeframe context",
     _flag("htf_context_clean")),
    ("m5_choch", "M5 change-of-character confirmation", _flag("m5_choch")),
    ("momentum_weakening", "HTF momentum was weakening",
     _flag("momentum_weakening")),
    ("veto_active", "the HTF veto was raised", _flag("veto_active")),
    ("anchor_override", "the anchor override fired", _flag("anchor_override")),
    ("selected_ob_fresh", "the selected order block was fresh",
     _flag("selected_ob_fresh")),
    ("entry_ready", "the entry level was ready at signal time",
     _flag("entry_ready")),
    ("session_active", "inside the SNIPER session window",
     _flag("session_active")),
)


def run(trades: list[LabelledTrade], metrics: dict) -> Study:
    results = classify_family(trades, CONFIRMATIONS)
    by_verdict: dict[str, list[str]] = {}
    for result in results:
        by_verdict.setdefault(result.verdict, []).append(result.factor)

    payload = {
        "source": "derived",
        "study": "score_component_analysis",
        "question": "which scoring inputs carry information about the outcome?",
        "method": {
            "test": "present vs absent, two-sample means on realized_r",
            "correction": "holm-bonferroni across the declared factor family",
            "family_size": len(CONFIRMATIONS),
            "significance_rule": "adjusted p < 0.05 AND both interval methods "
                                 "agree on excluding zero",
            "material_floor_r": 0.02,
            "limitation": "one factor at a time. A factor that only matters in "
                          "combination reads as neutral here, and two factors "
                          "that co-occur each take credit for the same effect — "
                          "see `co_occurrence`.",
        },
        "population": summarise(trades),
        "verdicts": {name: sorted(by_verdict.get(name, []))
                     for name in ("USEFUL", "HARMFUL", "NEUTRAL", "INVARIANT",
                                  "SPARSE")},
        "factors": [result.to_dict() for result in results],
        "score_response": _score_response(trades),
        "confirmation_count": _confirmation_count(trades),
        "co_occurrence": _co_occurrence(trades),
    }
    return Study("score_component_analysis", "Scoring model audit", payload,
                 _markdown(payload))


def _score_response(trades: list[LabelledTrade]) -> dict[str, Any]:
    """Expectancy as a function of the raw score.

    The scoring scale claims to rank opportunities. If it does, average R rises
    with the score. This table is the direct test of that claim and does not
    depend on any band boundary.
    """
    rows = group_by(trades, lambda t: (
        None if t.record.score is None else f"{int(t.record.score):02d}"))
    rows.sort(key=lambda row: row["key"])
    ordered = [row for row in rows if row["inferential"]]
    monotone = all(a["avg_r"] <= b["avg_r"]
                   for a, b in zip(ordered, ordered[1:])) if ordered else None
    spearman = _rank_correlation(
        [int(row["key"]) for row in ordered],
        [row["avg_r"] for row in ordered]) if len(ordered) > 2 else None
    return {
        "source": "derived",
        "note": "the scale's own claim is that a higher score means a better "
                "opportunity; this is that claim tested directly",
        "by_score": rows,
        "monotonic_across_inferential_bands": monotone,
        "rank_correlation_score_vs_avg_r": spearman,
    }


def _rank_correlation(xs: list[int], ys: list[float]) -> Optional[float]:
    """Spearman's rho, computed from ranks. No ties expected in the score axis."""
    import numpy as np

    if len(xs) < 3:
        return None
    rank = lambda values: np.argsort(np.argsort(np.asarray(values, float))) + 1.0
    a, b = rank(xs), rank(ys)
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _confirmation_count(trades: list[LabelledTrade]) -> dict[str, Any]:
    """Does stacking confirmations help?

    Counts how many of the eight boolean confirmations were true and reports
    expectancy by that count. This is the aggregate version of the question the
    per-factor tests answer individually, and it is the one that would show a
    combination effect the one-at-a-time tests cannot.
    """
    names = ("sweep", "fvg", "displacement", "fresh_ob", "volume_spike",
             "target_clear", "htf_context_clean", "m5_choch")

    def count(trade: LabelledTrade) -> Optional[str]:
        present = sum(1 for name in names if trade.labels.get(name))
        return f"{present}"

    rows = group_by(trades, count)
    rows.sort(key=lambda row: int(row["key"]))
    return {"source": "derived", "confirmations": list(names), "by_count": rows}


def _co_occurrence(trades: list[LabelledTrade]) -> dict[str, Any]:
    """Pairs of confirmations that almost always fire together.

    Emitted because entangled factors make one-at-a-time verdicts misleading: if
    two flags agree on 99% of trades, the study cannot tell which of them the
    effect belongs to, and a reader who acts on one is really acting on both.
    """
    names = ("sweep", "fvg", "displacement", "fresh_ob", "volume_spike",
             "target_clear", "htf_context_clean", "m5_choch")
    pairs = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            both = agree = total = 0
            for trade in trades:
                a, b = trade.labels.get(left), trade.labels.get(right)
                if a is None or b is None:
                    continue
                total += 1
                agree += int(bool(a) == bool(b))
                both += int(bool(a) and bool(b))
            if not total:
                continue
            pairs.append({"left": left, "right": right,
                          "agreement": agree / total,
                          "both_present": both / total})
    pairs.sort(key=lambda row: -row["agreement"])
    return {
        "source": "derived",
        "note": "agreement above ~0.95 means the two flags are close to the "
                "same variable and their individual verdicts cannot be "
                "separated",
        "entangled_pairs": [p for p in pairs if p["agreement"] >= 0.90],
        "all_pairs": pairs,
    }


def _markdown(payload: dict[str, Any]) -> str:
    out = ["# Scoring model audit\n\n",
           f"> {payload['question']}\n\n",
           f"> Method: {payload['method']['test']}; "
           f"{payload['method']['correction']} across "
           f"{payload['method']['family_size']} factors. "
           f"{payload['method']['significance_rule']}.\n\n",
           f"> **Limitation.** {payload['method']['limitation']}\n\n"]

    verdicts = payload["verdicts"]
    out.append("## Verdicts\n\n| Class | Factors |\n|---|---|\n")
    for name in ("USEFUL", "HARMFUL", "NEUTRAL", "INVARIANT", "SPARSE"):
        members = verdicts.get(name) or []
        out.append(f"| **{name}** | "
                   + (", ".join(f"`{m}`" for m in members) or "—") + " |\n")

    out.append("\n## Factor detail\n\n")
    out.append("| Factor | freq | n present | n absent | E present | E absent |"
               " effect R | 95% CI | p adj | r | verdict |\n")
    out.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|\n")
    for factor in sorted(payload["factors"],
                         key=lambda f: (f["effect_r"] is None,
                                        f["effect_r"] or 0.0)):
        ci = factor["welch_ci"]
        ci_text = "—" if not ci else f"[{ci[0]:+.4f}, {ci[1]:+.4f}]"
        out.append(
            f"| `{factor['factor']}` | {_pct(factor['frequency'])} | "
            f"{factor['present']['n']} | {factor['absent']['n']} | "
            f"{_num(factor['present']['expectancy'])} | "
            f"{_num(factor['absent']['expectancy'])} | "
            f"{_num(factor['effect_r'])} | {ci_text} | "
            f"{_num(factor['p_adjusted'])} | {_num(factor['point_biserial_r'])} "
            f"| {factor['verdict']}"
            f"{'' if factor['material'] else ' (immaterial)'} |\n")

    response = payload["score_response"]
    out.append("\n## Score response\n\n")
    out.append(f"> {response['note']}\n\n")
    out.append(f"Monotonic across inferential bands: "
               f"**{response['monotonic_across_inferential_bands']}** · "
               f"rank correlation score vs avg R: "
               f"{_num(response['rank_correlation_score_vs_avg_r'])}\n\n")
    out.append("| Score | n | avg R | net R | win | inferential |\n")
    out.append("|---|---:|---:|---:|---:|---|\n")
    for row in response["by_score"]:
        out.append(f"| {row['key']} | {row['n']} | {_num(row['avg_r'])} | "
                   f"{_num(row['net_r'], 1)} | {_num(row['win_rate'])} | "
                   f"{row['inferential']} |\n")

    counts = payload["confirmation_count"]
    out.append("\n## Confirmations stacked\n\n")
    out.append("| Confirmations true | n | avg R | net R | win |\n")
    out.append("|---:|---:|---:|---:|---:|\n")
    for row in counts["by_count"]:
        out.append(f"| {row['key']} | {row['n']} | {_num(row['avg_r'])} | "
                   f"{_num(row['net_r'], 1)} | {_num(row['win_rate'])} |\n")

    entangled = payload["co_occurrence"]["entangled_pairs"]
    out.append("\n## Entangled confirmations\n\n")
    out.append(f"> {payload['co_occurrence']['note']}\n\n")
    if not entangled:
        out.append("No pair agrees on 90% or more of trades.\n")
    else:
        out.append("| Left | Right | agreement | both present |\n")
        out.append("|---|---|---:|---:|\n")
        for pair in entangled:
            out.append(f"| `{pair['left']}` | `{pair['right']}` | "
                       f"{_pct(pair['agreement'])} | "
                       f"{_pct(pair['both_present'])} |\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.{places}f}"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"
