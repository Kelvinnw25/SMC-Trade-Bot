"""Hypothesis-oriented reports — M15.

## Why the hypotheses are a fixed list

The segmentation carries 1,078 segments. At the conventional 5% level roughly 54
of them will look significant on noise alone, which is why §12 says a segment may
motivate a hypothesis and never justify a change. A report that scanned those
segments and printed the extremes would be a machine for manufacturing false
findings.

So the questions are **declared here, in code, before any data is read**. The
list does not depend on the bundle, does not grow when a run looks unusual, and
is the same for every run analysed. That is what makes the Holm correction
meaningful: the family size is fixed in advance rather than chosen after seeing
which comparisons were interesting.

Adding a hypothesis is a deliberate edit to this file, and it widens the
correction for every other hypothesis at the same time. That cost is the point.

## What each question may use

`OutcomeRecord` fields only. Every split below is computable from a trade's own
record, so no label join is required and the hypotheses hold for any source that
produces outcomes — including a future live feed.

## What a result does not do

It does not recommend anything. `interpretation` states what the numbers say in
plain language, including — especially — when they say nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from backtest.analytics.stats import (
    Comparison,
    compare_means,
    compare_proportions,
    holm_bonferroni,
)

__all__ = ["HYPOTHESES", "Hypothesis", "evaluate_hypotheses"]

H1_SECONDS = 3600

Predicate = Callable[[Any], Optional[bool]]


@dataclass(frozen=True)
class Hypothesis:
    """One pre-declared question.

    `split` returns True for the left group, False for the right, and `None` for
    a record that belongs to neither — an explicit third answer, so that "not
    applicable" never silently joins the control group.
    """

    name: str
    question: str
    left_label: str
    right_label: str
    split: Predicate
    rationale: str = ""


def _session(record: Any) -> Optional[str]:
    """Reproduces the registry's session grouping from the record alone.

    Duplicating the *bucketing* is acceptable where duplicating a *metric* is
    not: this is a predicate over a timestamp, it is pinned against
    `metrics.segments.SESSIONS` by test, and importing the extractor would drag
    the whole segmentation module in to answer one question about an hour.
    """
    from backtest.metrics.segments import SESSIONS

    hour = datetime.fromtimestamp(int(record.boundary), tz=timezone.utc).hour
    for label, start, end in SESSIONS:
        if start <= hour < end:
            return label
    return None


def _mode(record: Any, wanted: str) -> Optional[bool]:
    value = getattr(record, "entry_mode", None)
    if value not in ("CONTINUATION", "RETRACEMENT"):
        return None
    return value == wanted


def _tier(record: Any) -> Optional[bool]:
    value = getattr(record, "alert_tier", None)
    if value not in ("A+", "VALID"):
        return None
    return value == "A+"


def _direction(record: Any) -> Optional[bool]:
    value = getattr(record, "direction", None)
    if value not in ("LONG", "SHORT"):
        return None
    return value == "SHORT"


def _score_high(record: Any) -> Optional[bool]:
    score = getattr(record, "score", None)
    return None if score is None else score >= 14


def _london(record: Any) -> Optional[bool]:
    session = _session(record)
    return None if session is None else session == "LONDON"


def _newyork_vs_asia(record: Any) -> Optional[bool]:
    session = _session(record)
    if session not in ("NEWYORK", "ASIA"):
        return None
    return session == "NEWYORK"


def _wide_rr(record: Any) -> Optional[bool]:
    rr = getattr(record, "structural_rr", None)
    return None if rr is None else rr >= 3.0


def _top_rank(record: Any) -> Optional[bool]:
    rank = getattr(record, "universe_rank", None)
    if rank is None:
        return None
    if rank <= 10:
        return True
    if rank >= 21:
        return False
    return None


def _fast_fill(record: Any) -> Optional[bool]:
    kind = getattr(record, "fill_kind", None)
    if kind is None:
        return None
    return kind == "IMMEDIATE"


#: The complete, pre-declared family. Order is stable so the Holm correction and
#: the CSV export are reproducible.
HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis(
        "continuation_vs_retracement",
        "Does CONTINUATION outperform RETRACEMENT?",
        "CONTINUATION", "RETRACEMENT",
        lambda record: _mode(record, "CONTINUATION"),
        rationale="the two execution modes are the strategy's primary fork"),
    Hypothesis(
        "aplus_vs_valid",
        "Does the A+ tier actually outperform VALID?",
        "A+", "VALID", _tier,
        rationale="the tier exists to mark higher conviction; if it does not "
                  "separate outcomes it is not carrying information"),
    Hypothesis(
        "short_vs_long",
        "Do SHORT trades outperform LONG?",
        "SHORT", "LONG", _direction,
        rationale="a directional asymmetry would point at the HTF bias logic "
                  "rather than at the entry rules"),
    Hypothesis(
        "high_score_vs_low_score",
        "Does a higher score increase expectancy?",
        "score >= 14", "score < 14", _score_high,
        rationale="scoring is meant to rank opportunities; a flat response "
                  "means the scale is not ordering by expected value"),
    Hypothesis(
        "london_vs_rest",
        "Does the LONDON session underperform?",
        "LONDON", "other sessions", _london,
        rationale="session effects are commonly asserted and rarely measured"),
    Hypothesis(
        "newyork_vs_asia",
        "Does NEW YORK outperform ASIA?",
        "NEWYORK", "ASIA", _newyork_vs_asia,
        rationale="the two highest-volume non-overlapping sessions"),
    Hypothesis(
        "wide_rr_vs_narrow",
        "Do wide structural targets (RR >= 3) outperform narrow ones?",
        "RR >= 3", "RR < 3", _wide_rr,
        rationale="a wider target trades win rate for payoff; the question is "
                  "whether the trade is priced correctly"),
    Hypothesis(
        "top_rank_vs_bottom_rank",
        "Do the most liquid universe slots outperform the least liquid?",
        "rank 1-10", "rank 21-30", _top_rank,
        rationale="liquidity tier is a proxy for spread and slippage, neither "
                  "of which the outcome model charges for"),
    Hypothesis(
        "immediate_vs_resting_fill",
        "Do IMMEDIATE fills underperform resting fills?",
        "IMMEDIATE", "other fills", _fast_fill,
        rationale="an immediate fill means price was already at the level, "
                  "which is a different trade from the one that was planned"),
)


@dataclass(frozen=True)
class HypothesisResult:
    """A question, answered twice — on expectancy and on win rate."""

    hypothesis: Hypothesis
    expectancy: Comparison
    win_rate: Comparison

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.hypothesis.name,
            "question": self.hypothesis.question,
            "rationale": self.hypothesis.rationale,
            "expectancy": self.expectancy.to_dict(),
            "win_rate": self.win_rate.to_dict(),
        }


def evaluate_hypotheses(scored: Sequence[Any]) -> dict[str, Any]:
    """Evaluate every declared hypothesis over the scored population."""
    results: list[HypothesisResult] = []
    for hypothesis in HYPOTHESES:
        left_r: list[float] = []
        right_r: list[float] = []
        left_wins = right_wins = 0
        for record in scored:
            side = hypothesis.split(record)
            if side is None:
                continue
            value = getattr(record, "realized_r", None)
            if value is None:
                continue
            won = getattr(record, "state", None) == "WIN"
            if side:
                left_r.append(float(value))
                left_wins += int(won)
            else:
                right_r.append(float(value))
                right_wins += int(won)

        expectancy = compare_means(
            hypothesis.name, hypothesis.question, hypothesis.left_label,
            hypothesis.right_label, left_r, right_r, metric="expectancy")
        win_rate = compare_proportions(
            hypothesis.name, hypothesis.question, hypothesis.left_label,
            hypothesis.right_label, left_wins, len(left_r),
            right_wins, len(right_r), metric="win_rate")
        results.append(HypothesisResult(hypothesis, expectancy, win_rate))

    # -- Holm across the declared family, per metric ---------------------
    adjusted_expectancy = holm_bonferroni(
        [result.expectancy.p_value for result in results])
    adjusted_win_rate = holm_bonferroni(
        [result.win_rate.p_value for result in results])

    finished: list[HypothesisResult] = []
    for result, p_e, p_w in zip(results, adjusted_expectancy,
                                adjusted_win_rate):
        expectancy = _finish(result.expectancy, p_e)
        win_rate = _finish(result.win_rate, p_w)
        finished.append(HypothesisResult(result.hypothesis, expectancy,
                                         win_rate))

    significant = [r.hypothesis.name for r in finished
                   if r.expectancy.significant]
    return {
        "source": "derived",
        "method": {
            "declared_in_advance": True,
            "family_size": len(HYPOTHESES),
            "correction": "holm-bonferroni, applied per metric across the "
                          "declared family",
            "interval_methods": ["welch", "percentile_bootstrap"],
            "significance_rule": "adjusted p < 0.05 AND both interval methods "
                                 "agree on whether zero is excluded",
            "note": "the family is fixed in code and does not depend on the "
                    "data, so the correction is honest; a question added later "
                    "widens the correction for every other question",
        },
        "significant": significant,
        "results": [result.to_dict() for result in finished],
    }


def _finish(comparison: Comparison, adjusted: Optional[float]) -> Comparison:
    """Attach the adjusted p-value and write the plain-language reading."""
    import dataclasses

    updated = dataclasses.replace(comparison, p_adjusted=adjusted)
    return dataclasses.replace(updated, interpretation=_interpret(updated))


def _interpret(comparison: Comparison) -> str:
    """What the numbers say, including when they say nothing."""
    if comparison.left_n == 0 or comparison.right_n == 0:
        return "one side is empty; the question cannot be answered here"
    unit = comparison.effect_unit
    effect = comparison.effect or 0.0
    size = f"{effect:+.4f} {unit}"

    if not comparison.inferential:
        return (f"{size}, but the smaller group holds "
                f"{min(comparison.left_n, comparison.right_n)} trades — below "
                f"the inferential floor, so this is a description of these "
                f"trades and not evidence about the strategy")

    if comparison.significant:
        direction = "outperforms" if effect > 0 else "underperforms"
        return (f"{comparison.left_label} {direction} "
                f"{comparison.right_label} by {size} "
                f"(adjusted p = {comparison.p_adjusted:.4f}); both interval "
                f"methods exclude zero, so the difference survives correction "
                f"for the {len(HYPOTHESES)} declared comparisons")

    if comparison.methods_agree is False:
        return (f"{size}, but the analytic and bootstrap intervals disagree on "
                f"whether zero is excluded — the R distribution is heavy-tailed "
                f"enough here that the answer depends on the estimator, which "
                f"is not a finding")

    adjusted = comparison.p_adjusted
    tail = "" if adjusted is None else f" (adjusted p = {adjusted:.4f})"
    return (f"{size}, not distinguishable from zero after correction{tail}; "
            f"the data do not support a difference in either direction")
