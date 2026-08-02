"""Bootstrap confidence intervals — the pipeline's only stochastic step.

REPLAY_CONTRACT.md §12, "Statistical reproducibility (normative)", which fixes
every parameter precisely because this is the one place randomness enters:

- **percentile method**;
- `n_resamples = 10_000`;
- `numpy.random.default_rng` seeded with `bootstrap_seed`, default **20260727**;
- resampling trades **with replacement within the segment**;
- reporting the **2.5 / 97.5** percentiles;
- the seed is recorded in `metrics.json`;
- segments with `n < min_segment_n` (default **30**) are printed with their `n` but
  flagged `inferential: false` and **MUST NOT** be used to justify a change;
- a multiple-comparison caution is mandatory whenever more than 20 segments are
  reported;
- **`scipy` is not available and MUST NOT be introduced** — this is a `numpy`
  reduction.

## Determinism

A fresh `default_rng(seed)` is constructed per interval, so a segment's CI depends
only on its own values and the seed — never on how many segments were computed
before it, or in what order. That is what makes the report byte-reproducible while
still being a resampling estimate.

## Why the flag matters more than the interval

At `n < 30` a percentile bootstrap is not merely wide, it is *unreliable* — it can
only resample the few values present, so a segment of 5 trades produces an interval
built from 5 numbers and will look far more precise than it is. The `inferential`
flag is therefore not advisory decoration: it is the gate that stops a 5-trade
segment from motivating a strategy change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

import numpy as np

__all__ = [
    "BOOTSTRAP_SEED",
    "CONFIDENCE",
    "ConfidenceInterval",
    "MIN_SEGMENT_N",
    "MULTIPLE_COMPARISON_THRESHOLD",
    "N_RESAMPLES",
    "bootstrap_mean",
    "bootstrap_statistic",
]

#: §12, all four normative and not configurable away from these defaults without
#: an amendment. They are parameters rather than literals so a sensitivity study
#: can state what it changed — never so a report can quietly differ.
BOOTSTRAP_SEED = 20260727
N_RESAMPLES = 10_000
MIN_SEGMENT_N = 30
CONFIDENCE = (2.5, 97.5)

#: §12: "A multiple-comparison caution is mandatory whenever more than 20 segments
#: are reported."
MULTIPLE_COMPARISON_THRESHOLD = 20

#: Resamples are drawn in blocks so a large segment cannot allocate an
#: `n_resamples × n` matrix at once. Behaviour-neutral: the same generator is
#: consumed in the same order, so the result is identical to the unchunked draw.
_BLOCK = 1_000


@dataclass(frozen=True)
class ConfidenceInterval:
    """A percentile bootstrap interval, with the honesty flag attached."""

    point: Optional[float]
    low: Optional[float]
    high: Optional[float]
    n: int
    inferential: bool
    method: str = "percentile_bootstrap"
    n_resamples: int = N_RESAMPLES
    seed: int = BOOTSTRAP_SEED
    confidence: tuple[float, float] = CONFIDENCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "ci_low": self.low,
            "ci_high": self.high,
            "n": self.n,
            "inferential": self.inferential,
            "method": self.method,
            "n_resamples": self.n_resamples,
            "bootstrap_seed": self.seed,
            "confidence": list(self.confidence),
        }


def bootstrap_mean(
    values: Sequence[float],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_resamples: int = N_RESAMPLES,
    min_n: int = MIN_SEGMENT_N,
) -> ConfidenceInterval:
    """A percentile CI for the mean — expectancy's interval."""
    return bootstrap_statistic(
        values, statistic=lambda block: block.mean(axis=1),
        seed=seed, n_resamples=n_resamples, min_n=min_n)


def bootstrap_statistic(
    values: Sequence[float],
    *,
    statistic: Callable[[np.ndarray], np.ndarray],
    seed: int = BOOTSTRAP_SEED,
    n_resamples: int = N_RESAMPLES,
    min_n: int = MIN_SEGMENT_N,
) -> ConfidenceInterval:
    """A percentile CI for any statistic expressible as a row-wise reduction.

    `statistic` receives a `(block, n)` array of resampled values and returns one
    value per row. A win *rate* is the mean of an indicator array, so it needs no
    separate implementation — pass the indicators as `values`.

    An empty population yields an all-`None` interval rather than raising: a segment
    with no trades is an ordinary outcome of segmenting, and the caller reports the
    absence.
    """
    data = np.asarray(values, dtype=np.float64)
    n = int(data.size)
    if n == 0:
        return ConfidenceInterval(
            point=None, low=None, high=None, n=0, inferential=False,
            n_resamples=n_resamples, seed=seed)

    point = float(data.mean()) if statistic is None else None
    # The point estimate is the statistic applied to the sample itself, not the
    # mean of the resamples: the bootstrap estimates the *interval*, and using the
    # resample mean would introduce a tiny stochastic wobble into a number that is
    # exactly computable.
    point = float(statistic(data.reshape(1, n))[0])

    if n == 1:
        # Every resample is the same single value, so the interval collapses to the
        # point. Reported rather than suppressed, and never inferential.
        return ConfidenceInterval(
            point=point, low=point, high=point, n=1, inferential=False,
            n_resamples=n_resamples, seed=seed)

    rng = np.random.default_rng(seed)
    stats = np.empty(n_resamples, dtype=np.float64)
    filled = 0
    while filled < n_resamples:
        size = min(_BLOCK, n_resamples - filled)
        index = rng.integers(0, n, size=(size, n))
        stats[filled:filled + size] = statistic(data[index])
        filled += size

    low, high = np.percentile(stats, CONFIDENCE)
    return ConfidenceInterval(
        point=point,
        low=float(low),
        high=float(high),
        n=n,
        inferential=n >= min_n,
        n_resamples=n_resamples,
        seed=seed,
    )
