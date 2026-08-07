"""Shared machinery for the Phase 3 studies — M16.

## What a study is allowed to do

Read the bundle, join the ledger labels the metrics engine already joined, and
**compare populations**. Nothing here proposes a parameter, edits a weight, or
touches strategy code. The output of Phase 3 is evidence; acting on it is a
separate decision made by a person.

## The join

Studies need the strategy context that lives in the signal ledger — which
confirmations fired, the selected order block's metadata, the HTF biases, ATR.
`backtest.dataset.LabeledDataset` is the sanctioned join (§11 step 4) and is
reused verbatim, so a study sees exactly the labels the metrics engine saw.

## Why classification is a rule, not a judgement

Every factor is classified by the same pre-declared decision procedure:

    HARMFUL   present is significantly worse than absent
    USEFUL    present is significantly better than absent
    NEUTRAL   the difference is not distinguishable from zero
    INVARIANT the factor never varies, so it carries no information at all
    SPARSE    one side is below the inferential floor

"Significant" means the Holm-adjusted p is below 0.05 **and** both interval
methods agree on whether zero is excluded — the same conservative rule the
hypothesis registry uses. A factor is never called useful because its expectancy
looks higher; the difference has to survive correction across the whole factor
family, which is exactly the protection §12 asks for.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from backtest.analytics.stats import Comparison, compare_means, holm_bonferroni
from backtest.metrics import MIN_SEGMENT_N

__all__ = [
    "CLASSES",
    "FactorResult",
    "Study",
    "classify_family",
    "labelled_trades",
    "point_biserial",
    "summarise",
]

#: The verdicts a factor can receive. Closed list — a study may not invent one.
CLASSES = ("USEFUL", "HARMFUL", "NEUTRAL", "INVARIANT", "SPARSE")

#: Practical-significance floor in R. A difference smaller than this is reported
#: with its statistics but flagged `material: false`: at 31,209 trades a
#: statistically real 0.005R difference is not a reason to change anything.
MATERIAL_R = 0.02


@dataclass(frozen=True)
class LabelledTrade:
    """One scored trade with its ledger context attached.

    A thin carrier rather than a new domain object: studies read attributes, and
    inventing a record type here would be a second definition of a trade.
    """

    record: Any
    labels: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.record, name)

    def label(self, name: str, default: Any = None) -> Any:
        value = self.labels.get(name)
        return default if value is None else value


def labelled_trades(run_dir: Any) -> list[LabelledTrade]:
    """Scored trades in C-9.8 order, each carrying its joined ledger labels."""
    from backtest.dataset import LabeledDataset, canonical_key
    from backtest.metrics import Populations, c98_order_key

    dataset = LabeledDataset.from_run(run_dir)
    scored = [record for record in Populations.of(dataset.records).scored
              if record.realized_r is not None]
    scored.sort(key=c98_order_key)
    out: list[LabelledTrade] = []
    for record in scored:
        key = canonical_key(record.run_id, record.symbol, record.h1_last_ts)
        out.append(LabelledTrade(record, dataset.labels.get(key) or {}))
    return out


# ---------------------------------------------------------------------------
# Factor comparison
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FactorResult:
    """One factor, split two ways, with the verdict and the evidence for it."""

    factor: str
    description: str
    present_label: str
    absent_label: str
    comparison: Comparison
    correlation: Optional[float]
    frequency: Optional[float]
    verdict: str = "NEUTRAL"
    material: bool = False

    def to_dict(self) -> dict[str, Any]:
        block = self.comparison.to_dict()
        return {
            "factor": self.factor,
            "description": self.description,
            "verdict": self.verdict,
            "material": self.material,
            "frequency": self.frequency,
            "point_biserial_r": self.correlation,
            "present": {"label": self.present_label,
                        "n": block["left"]["n"],
                        "expectancy": block["left"]["value"]},
            "absent": {"label": self.absent_label,
                       "n": block["right"]["n"],
                       "expectancy": block["right"]["value"]},
            "effect_r": block["effect"],
            "cohens_d": block["cohens_d"],
            "welch_ci": block["welch_ci"],
            "bootstrap_ci": block["bootstrap_ci"],
            "p_value": block["p_value"],
            "p_adjusted": block["p_adjusted"],
            "significant": block["significant"],
            "inferential": block["inferential"],
            "interpretation": block["interpretation"],
        }


def point_biserial(flags: Sequence[bool],
                   values: Sequence[float]) -> Optional[float]:
    """Correlation between a binary factor and realised R.

    Reported because a correlation is the shape people ask for, and withheld
    from the verdict because at n = 31,209 an r of 0.02 is "significant" and
    means nothing. The classification uses the effect size in R instead.
    """
    if len(flags) != len(values) or len(flags) < 3:
        return None
    x = np.asarray(flags, dtype=float)
    y = np.asarray(values, dtype=float)
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def classify_family(
    trades: Sequence[LabelledTrade],
    factors: Sequence[tuple[str, str, Callable[[LabelledTrade], Optional[bool]]]],
    *, min_n: int = MIN_SEGMENT_N,
) -> list[FactorResult]:
    """Evaluate a family of binary factors and classify each one.

    Holm is applied **across the family**, so a factor that looks significant
    only because twenty were tested does not survive. The family is the argument
    list, which each study declares statically.
    """
    staged: list[tuple[str, str, Comparison, Optional[float], Optional[float]]] = []
    for name, description, predicate in factors:
        present: list[float] = []
        absent: list[float] = []
        flags: list[bool] = []
        values: list[float] = []
        for trade in trades:
            side = predicate(trade)
            if side is None:
                continue
            value = float(trade.record.realized_r)
            (present if side else absent).append(value)
            flags.append(bool(side))
            values.append(value)
        comparison = compare_means(
            name, description, "present", "absent", present, absent,
            metric="expectancy", min_n=min_n)
        total = len(present) + len(absent)
        frequency = (len(present) / total) if total else None
        staged.append((name, description, comparison, frequency,
                       point_biserial(flags, values)))

    adjusted = holm_bonferroni([item[2].p_value for item in staged])
    results: list[FactorResult] = []
    for (name, description, comparison, frequency, correlation), p in zip(
            staged, adjusted):
        import dataclasses

        finished = dataclasses.replace(comparison, p_adjusted=p)
        finished = dataclasses.replace(
            finished, interpretation=_read(finished, len(staged)))
        verdict, material = _verdict(finished)
        results.append(FactorResult(
            factor=name, description=description, present_label="present",
            absent_label="absent", comparison=finished,
            correlation=correlation, frequency=frequency,
            verdict=verdict, material=material))
    return results


def _verdict(comparison: Comparison) -> tuple[str, bool]:
    """The pre-declared decision procedure. No judgement, no exceptions."""
    if comparison.left_n == 0 or comparison.right_n == 0:
        return "INVARIANT", False
    if not comparison.inferential:
        return "SPARSE", False
    effect = comparison.effect or 0.0
    material = abs(effect) >= MATERIAL_R
    if not comparison.significant:
        return "NEUTRAL", material
    return ("USEFUL" if effect > 0 else "HARMFUL"), material


def _read(comparison: Comparison, family: int) -> str:
    if comparison.left_n == 0 or comparison.right_n == 0:
        return ("the factor never varies across the population, so it cannot "
                "carry information about the outcome")
    if not comparison.inferential:
        return (f"only {min(comparison.left_n, comparison.right_n)} trades on "
                f"the smaller side — described, not tested")
    effect = comparison.effect or 0.0
    if comparison.significant:
        word = "better" if effect > 0 else "worse"
        material = ("" if abs(effect) >= MATERIAL_R else
                    ", though the size is below the "
                    f"{MATERIAL_R} R practical floor")
        return (f"present is {effect:+.4f} R {word} than absent, surviving "
                f"correction across {family} factors{material}")
    return (f"{effect:+.4f} R, not distinguishable from zero after correcting "
            f"across {family} factors")


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def summarise(trades: Iterable[LabelledTrade]) -> dict[str, Any]:
    """Counts and R for an arbitrary subset. Never a published metric name."""
    values = [float(t.record.realized_r) for t in trades
              if t.record.realized_r is not None]
    if not values:
        return {"n": 0, "net_r": 0.0, "avg_r": None, "win_rate": None}
    wins = sum(1 for t in trades if getattr(t.record, "state", None) == "WIN")
    return {
        "n": len(values),
        "net_r": float(sum(values)),
        "avg_r": float(sum(values) / len(values)),
        "win_rate": wins / len(values),
    }


def group_by(trades: Sequence[LabelledTrade],
             key: Callable[[LabelledTrade], Optional[str]],
             *, min_n: int = 0) -> list[dict[str, Any]]:
    """Group, summarise, and sort by average R. `None` keys are dropped."""
    buckets: dict[str, list[LabelledTrade]] = {}
    for trade in trades:
        name = key(trade)
        if name is None:
            continue
        buckets.setdefault(str(name), []).append(trade)
    rows = []
    for name, members in buckets.items():
        row = summarise(members)
        if row["n"] < min_n:
            continue
        row["key"] = name
        row["inferential"] = row["n"] >= MIN_SEGMENT_N
        rows.append(row)
    rows.sort(key=lambda row: (row["avg_r"] is None, row["avg_r"]))
    return rows


@dataclass(frozen=True)
class Study:
    """One research output: a name, a payload and its markdown."""

    name: str
    title: str
    payload: dict[str, Any]
    markdown: str
