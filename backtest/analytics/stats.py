"""Statistical primitives for the research workbench — M15.

Nothing here is a *metric*. `backtest.metrics` owns expectancy, win rate and
their per-segment confidence intervals; this module owns the machinery for
comparing **two populations**, which no metric can express because a metric
describes one population at a time.

## What a comparison must carry

§12 permits a segment to motivate a hypothesis and forbids it from justifying a
change. A number that could justify anything therefore has to arrive with the
evidence for it: the sample sizes, the effect size in the unit the reader thinks
in (R), a confidence interval on the **difference**, and a p-value that has been
corrected for the fact that several hypotheses were asked at once.

Overlapping per-segment intervals are *not* the test. Two intervals can overlap
while the difference is significant, and the reverse is rarer but possible. Only
an interval on the difference answers the question, so that is what is computed.

## Two methods, both reported

`welch` — the analytic interval. Its assumption is the central limit theorem for
the *sampling distribution of the mean*, not normality of the data, which holds
comfortably at the sample sizes this project produces.

`bootstrap` — percentile resampling, which assumes nothing about the shape. It
matters here because the R distribution is heavily right-tailed: a single trade
in the official baseline realised +103R, and heavy tails slow CLT convergence.

Both are emitted. When they agree the result is robust; when they disagree the
disagreement is the finding, and hiding it behind one preferred number would be
the opposite of what a research artifact is for.

## Determinism

The bootstrap draws from `numpy.random.default_rng(seed)` with the seed fixed at
`backtest.metrics.BOOTSTRAP_SEED`, so the same bundle produces the same interval
on every run and on every machine. Resampling is chunked because the naive
`(n_resamples, n)` matrix for a 20,000-trade group is gigabytes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from backtest.metrics import BOOTSTRAP_SEED, CONFIDENCE, MIN_SEGMENT_N

__all__ = [
    "COMPARISON_RESAMPLES",
    "Comparison",
    "cohens_d",
    "compare_means",
    "compare_proportions",
    "describe",
    "holm_bonferroni",
    "percentiles",
]

#: Fewer than the 10,000 the metrics engine uses for a one-sample interval. A
#: two-sample difference needs two resampled groups per draw, and at 30,000
#: trades the extra precision is far below the width of the interval itself.
COMPARISON_RESAMPLES = 2_000

#: Resamples per chunk. Bounds peak memory at roughly `_CHUNK * n * 8` bytes.
_CHUNK = 200


@dataclass(frozen=True)
class Comparison:
    """One two-sample question, with everything needed to judge the answer."""

    name: str
    question: str
    left_label: str
    right_label: str
    metric: str

    left_n: int = 0
    right_n: int = 0
    left_value: Optional[float] = None
    right_value: Optional[float] = None

    #: `left − right`, in the metric's own unit.
    effect: Optional[float] = None
    effect_unit: str = "R"
    cohens_d: Optional[float] = None

    welch_ci: Optional[tuple[float, float]] = None
    bootstrap_ci: Optional[tuple[float, float]] = None
    p_value: Optional[float] = None
    p_adjusted: Optional[float] = None

    inferential: bool = False
    methods_agree: Optional[bool] = None
    interpretation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "question": self.question,
            "metric": self.metric,
            "left": {"label": self.left_label, "n": self.left_n,
                     "value": self.left_value},
            "right": {"label": self.right_label, "n": self.right_n,
                      "value": self.right_value},
            "effect": self.effect,
            "effect_unit": self.effect_unit,
            "cohens_d": self.cohens_d,
            "welch_ci": list(self.welch_ci) if self.welch_ci else None,
            "bootstrap_ci": (list(self.bootstrap_ci)
                             if self.bootstrap_ci else None),
            "p_value": self.p_value,
            "p_adjusted": self.p_adjusted,
            "significant": self.significant,
            "inferential": self.inferential,
            "methods_agree": self.methods_agree,
            "interpretation": self.interpretation,
        }

    @property
    def significant(self) -> Optional[bool]:
        """Significant **after** correction, and only when both intervals agree.

        Deliberately conservative. A result that one method calls significant and
        the other does not is not a finding, it is a disagreement about method,
        and this project has no use for a claim that depends on which estimator
        was chosen.
        """
        if self.p_adjusted is None or not self.inferential:
            return None
        if not self.methods_agree:
            return False
        return self.p_adjusted < 0.05


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

def describe(values: Sequence[float]) -> dict[str, Any]:
    """Location, spread and shape of one sample. Counts and moments only."""
    array = np.asarray(list(values), dtype=float)
    if array.size == 0:
        return {"n": 0, "mean": None, "stdev": None, "min": None, "max": None}
    return {
        "n": int(array.size),
        "mean": float(array.mean()),
        "stdev": float(array.std(ddof=1)) if array.size > 1 else 0.0,
        "min": float(array.min()),
        "max": float(array.max()),
        "sum": float(array.sum()),
    }


def percentiles(values: Sequence[float],
                points: Sequence[int] = (5, 25, 50, 75, 90, 95, 99),
                ) -> dict[str, Optional[float]]:
    """Fixed percentile points. `linear` interpolation, stated so a reader can
    reproduce the number rather than guess the convention."""
    array = np.asarray(list(values), dtype=float)
    if array.size == 0:
        return {f"p{point}": None for point in points}
    return {f"p{point}": float(np.percentile(array, point, method="linear"))
            for point in points}


# ---------------------------------------------------------------------------
# Effect size
# ---------------------------------------------------------------------------

def cohens_d(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    """Pooled standardised mean difference.

    Reported alongside the raw difference rather than instead of it: R is already
    a normalised unit (one unit of risk), so the raw difference is the number a
    reader should act on. `d` is here because it makes two questions in different
    units comparable, which the raw difference cannot do.
    """
    a = np.asarray(list(left), dtype=float)
    b = np.asarray(list(right), dtype=float)
    if a.size < 2 or b.size < 2:
        return None
    pooled_var = (((a.size - 1) * a.var(ddof=1)
                   + (b.size - 1) * b.var(ddof=1))
                  / (a.size + b.size - 2))
    if pooled_var <= 0:
        return None
    return float((a.mean() - b.mean()) / math.sqrt(pooled_var))


# ---------------------------------------------------------------------------
# Two-sample comparison
# ---------------------------------------------------------------------------

def compare_means(
    name: str, question: str, left_label: str, right_label: str,
    left: Sequence[float], right: Sequence[float], *,
    metric: str = "expectancy", seed: int = BOOTSTRAP_SEED,
    n_resamples: int = COMPARISON_RESAMPLES,
    min_n: int = MIN_SEGMENT_N,
) -> Comparison:
    """Compare two samples' means, with both interval methods."""
    a = np.asarray(list(left), dtype=float)
    b = np.asarray(list(right), dtype=float)
    base = dict(name=name, question=question, left_label=left_label,
                right_label=right_label, metric=metric,
                left_n=int(a.size), right_n=int(b.size))

    if a.size == 0 or b.size == 0:
        return Comparison(
            **base,
            interpretation="one side is empty; no comparison is possible")

    left_mean, right_mean = float(a.mean()), float(b.mean())
    effect = left_mean - right_mean
    inferential = a.size >= min_n and b.size >= min_n

    welch = _welch(a, b)
    boot = _bootstrap_diff(a, b, seed=seed, n_resamples=n_resamples)
    agree = None
    if welch["ci"] and boot:
        agree = _excludes_zero(welch["ci"]) == _excludes_zero(boot)

    return Comparison(
        **base,
        left_value=left_mean, right_value=right_mean,
        effect=effect, cohens_d=cohens_d(a, b),
        welch_ci=welch["ci"], bootstrap_ci=boot,
        p_value=welch["p"], inferential=inferential, methods_agree=agree,
    )


def compare_proportions(
    name: str, question: str, left_label: str, right_label: str,
    left_successes: int, left_n: int, right_successes: int, right_n: int, *,
    metric: str = "win_rate", min_n: int = MIN_SEGMENT_N,
) -> Comparison:
    """Two-proportion comparison — a normal-approximation z interval.

    Used for win rate. The bootstrap column is left empty rather than filled
    with a resampled Bernoulli, which would reproduce the analytic answer to
    three decimals and imply a second opinion that does not exist.
    """
    base = dict(name=name, question=question, left_label=left_label,
                right_label=right_label, metric=metric,
                left_n=int(left_n), right_n=int(right_n))
    if left_n <= 0 or right_n <= 0:
        return Comparison(**base, effect_unit="rate",
                          interpretation="one side is empty")

    p_left = left_successes / left_n
    p_right = right_successes / right_n
    effect = p_left - p_right
    se = math.sqrt(p_left * (1 - p_left) / left_n
                   + p_right * (1 - p_right) / right_n)
    z = _z_for(CONFIDENCE)
    ci = ((effect - z * se, effect + z * se) if se > 0 else None)
    p_value = None
    if se > 0:
        pooled = (left_successes + right_successes) / (left_n + right_n)
        pooled_se = math.sqrt(pooled * (1 - pooled)
                              * (1 / left_n + 1 / right_n))
        if pooled_se > 0:
            p_value = _two_sided_normal(effect / pooled_se)

    return Comparison(
        **base, left_value=p_left, right_value=p_right, effect=effect,
        effect_unit="rate", welch_ci=ci, p_value=p_value,
        inferential=left_n >= min_n and right_n >= min_n,
        methods_agree=True,
    )


# ---------------------------------------------------------------------------
# Multiple comparisons
# ---------------------------------------------------------------------------

def holm_bonferroni(p_values: Sequence[Optional[float]]
                    ) -> list[Optional[float]]:
    """Holm's step-down adjustment, order preserved.

    Holm rather than plain Bonferroni: it controls the same family-wise error
    rate and is uniformly more powerful, so nothing is given up by using it.
    `None` inputs (a comparison that could not be computed) stay `None` and are
    excluded from the family size — correcting for a test that never ran would
    penalise the ones that did.
    """
    indexed = [(index, p) for index, p in enumerate(p_values) if p is not None]
    adjusted: list[Optional[float]] = [None] * len(p_values)
    if not indexed:
        return adjusted
    indexed.sort(key=lambda item: item[1])
    family = len(indexed)
    running = 0.0
    for rank, (index, p) in enumerate(indexed):
        value = min(1.0, (family - rank) * p)
        running = max(running, value)      # enforce monotonicity
        adjusted[index] = running
    return adjusted


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _welch(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """Welch's unequal-variance interval and two-sided p-value.

    The p-value uses the normal tail rather than Student's t. At the sample
    sizes here the two agree to more decimal places than are reported, and the
    normal tail needs no special-function dependency.
    """
    if a.size < 2 or b.size < 2:
        return {"ci": None, "p": None}
    se = math.sqrt(a.var(ddof=1) / a.size + b.var(ddof=1) / b.size)
    effect = float(a.mean() - b.mean())
    if se <= 0:
        return {"ci": None, "p": None}
    z = _z_for(CONFIDENCE)
    return {"ci": (effect - z * se, effect + z * se),
            "p": _two_sided_normal(effect / se)}


def _bootstrap_diff(a: np.ndarray, b: np.ndarray, *, seed: int,
                    n_resamples: int) -> Optional[tuple[float, float]]:
    """Percentile interval for the difference of means, chunked.

    Each side is resampled independently with replacement — the two groups are
    unpaired, so a shared resample index would invent a correspondence that does
    not exist.
    """
    if a.size < 2 or b.size < 2:
        return None
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_resamples, dtype=float)
    done = 0
    while done < n_resamples:
        take = min(_CHUNK, n_resamples - done)
        left = a[rng.integers(0, a.size, size=(take, a.size))].mean(axis=1)
        right = b[rng.integers(0, b.size, size=(take, b.size))].mean(axis=1)
        diffs[done:done + take] = left - right
        done += take
    low, high = CONFIDENCE
    return (float(np.percentile(diffs, low)),
            float(np.percentile(diffs, high)))


def _excludes_zero(interval: tuple[float, float]) -> bool:
    return interval[0] > 0.0 or interval[1] < 0.0


def _z_for(confidence: tuple[float, float]) -> float:
    """The two-sided normal critical value for the engine's confidence band.

    Derived from `CONFIDENCE` rather than hardcoded at 1.96 so that changing the
    engine's band changes this too, instead of leaving two definitions of "95%".
    """
    tail = confidence[0] / 100.0
    return abs(_normal_quantile(tail))


def _normal_quantile(p: float) -> float:
    """Inverse standard normal CDF — Acklam's rational approximation.

    Accurate to ~1e-9 over the whole range, which is far tighter than any
    interval it is used to build. Written out rather than imported because
    `statistics.NormalDist` exists but the project pins its own numerics, and a
    closed form keeps this module free of hidden version-dependent behaviour.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"quantile requires 0 < p < 1, got {p}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                 + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r
            + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r
                            + b[4]) * r + 1)


def _two_sided_normal(z: float) -> float:
    """`P(|Z| > |z|)` via the complementary error function."""
    return float(math.erfc(abs(z) / math.sqrt(2.0)))
