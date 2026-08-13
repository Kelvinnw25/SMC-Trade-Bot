"""TRAIN / DEV / HOLDOUT partition, quarterly slices, and the sign test.

Phase 3.5 preparation. Two jobs, both evaluation-time only: label a trade's
window role, and turn a set of per-slice results into a promote / flag / reject
verdict.

## Why slices instead of walk-forward replays

Walk-forward exists to stop a researcher tuning on the test window. When a variant
is *fully pre-specified* — a hashed `strategy_inputs`, a written prediction, no
per-fold fitting — evaluating it on time slices of one full-window run carries the
same protection and costs nothing. Fold replays would cost ~82 h for the R1-R3
programme and each fold's test window is individually powerless anyway: at
sigma(realized_r) = 1.667 R a 3-month test slice resolves about +-0.14 R, which is
noise against a predicted +0.06.

Slicing one unsharded run also avoids the lifecycle reset that `research.splits`
warns about (C-5.7.4: sharded runs are not the baseline, `is_baseline` is
permanently False).

## Why the sign test, and why ten slices

Per-slice magnitudes are noise; per-slice *signs* are not. The old "positive in
4/4 years" screen looked like evidence and could never be significance — with four
observations the smallest attainable p is 1/16 = 0.0625, above 0.05 no matter how
clean the data. Ten slices fix that: 9/10 is p = 0.0107, 8/10 is p = 0.0547.

The thresholds here are therefore **derived, not typed**: `sign_test` finds the
smallest k whose one-sided binomial p is at or below `alpha`. At ten slices that
reproduces 9 promote / 8 flag exactly, and it stays correct if the partition
boundary ever moves — a twelve-slice TRAIN needs 10, not 9, and a hardcoded 9
would silently become a weaker test.

## The partition

| Role | Window | Authority |
|---|---|---|
| TRAIN | 2023-01-01 -> 2025-06-30 | selects the candidate |
| DEV | 2025-07-01 -> 2026-06-28 | **none** - contaminated by prior research |
| HOLDOUT | after `T_n` = 1782687600 | veto only, and not yet acquired |

DEV has no promotion authority because every Phase 3 study read it. It is kept as
a labelled diagnostic rather than deleted, because a period that disagrees with
TRAIN is worth seeing — it already does, for the continuation-only gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "DEV_END",
    "DEV_START",
    "HOLDOUT_START",
    "ROLES",
    "TRAIN_END",
    "TRAIN_START",
    "PartitionError",
    "SignTest",
    "Slice",
    "quarter_of",
    "role_of",
    "sign_test",
    "slice_quarters",
]

#: 2023-01-01T00:00:00Z - the official run's `T_0`.
TRAIN_START = 1672531200
#: 2025-06-30T23:00:00Z - the last H1 boundary of 2025 Q2.
TRAIN_END = 1751324400
#: 2025-07-01T00:00:00Z
DEV_START = 1751328000
#: 2026-06-28T23:00:00Z - the official run's `T_n`.
DEV_END = 1782687600
#: Everything after `T_n`. Not in any snapshot yet; acquired once, at the end.
HOLDOUT_START = DEV_END + 3600

ROLES = ("TRAIN", "DEV", "HOLDOUT", "OUT_OF_WINDOW")


class PartitionError(Exception):
    """A partition or sign test cannot be computed as requested."""


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def role_of(boundary: int) -> str:
    """Which evaluation role a trade's instantiation boundary falls in.

    Boundaries are half-open on the right at each internal edge, so no trade can
    be counted in two roles — the failure that would let a leaked trade improve
    both the selection set and the diagnostic set.
    """
    epoch = int(boundary)
    if epoch < TRAIN_START:
        return "OUT_OF_WINDOW"
    if epoch <= TRAIN_END:
        return "TRAIN"
    if epoch <= DEV_END:
        return "DEV"
    return "HOLDOUT"


def quarter_of(boundary: int) -> str:
    """`YYYY-Qn` of instantiation, matching `metrics.segments`' `quarter` key."""
    moment = _utc(boundary)
    return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"


@dataclass(frozen=True)
class Slice:
    """One evaluation slice: a quarter, its role, and the values inside it."""

    label: str
    role: str
    values: tuple[float, ...]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def net_r(self) -> float:
        return math.fsum(self.values)

    @property
    def mean_r(self) -> Optional[float]:
        return (self.net_r / self.n) if self.n else None

    @property
    def win_rate(self) -> Optional[float]:
        if not self.n:
            return None
        return sum(1 for v in self.values if v > 0) / self.n

    @property
    def positive(self) -> Optional[bool]:
        mean = self.mean_r
        return None if mean is None else mean > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "role": self.role, "n": self.n,
            "net_r": self.net_r, "mean_r": self.mean_r,
            "win_rate": self.win_rate, "positive": self.positive,
        }


def slice_quarters(
    pairs: Iterable[tuple[int, float]], *, role: str = "TRAIN",
    min_n: int = 30,
) -> list[Slice]:
    """Group `(boundary, realized_r)` pairs into the quarters of one role.

    `min_n` drops a quarter too small to carry a sign — an under-populated slice
    would contribute a coin flip to the sign test and dilute it. Dropped slices
    are absent rather than counted as failures, and `sign_test` reports the
    surviving count, so the family size stays honest.
    """
    if role not in ROLES:
        raise PartitionError(f"role must be one of {ROLES}, got {role!r}")
    buckets: dict[str, list[float]] = {}
    for boundary, value in pairs:
        if role_of(boundary) != role:
            continue
        buckets.setdefault(quarter_of(boundary), []).append(float(value))
    return [Slice(label=label, role=role, values=tuple(values))
            for label, values in sorted(buckets.items())
            if len(values) >= min_n]


# ---------------------------------------------------------------------------
# the sign test
# ---------------------------------------------------------------------------

def _binomial_tail(k: int, n: int) -> float:
    """P(X >= k) for X ~ Binomial(n, 0.5), exact in integer arithmetic."""
    if n <= 0:
        return 1.0
    k = max(k, 0)
    if k > n:
        return 0.0
    total = sum(math.comb(n, i) for i in range(k, n + 1))
    return total / (2 ** n)


def _threshold(n: int, alpha: float) -> Optional[int]:
    """Smallest k whose one-sided binomial p is <= alpha, or None if unreachable.

    `None` is the important return: with four slices no k satisfies alpha = 0.05,
    and saying so is the whole reason this function exists rather than a constant.
    """
    for k in range(n + 1):
        if _binomial_tail(k, n) <= alpha:
            return k
    return None


@dataclass(frozen=True)
class SignTest:
    """The verdict, its thresholds, and the evidence behind both."""

    slices: tuple[Slice, ...]
    alpha: float
    positive: int
    evaluated: int
    promote_at: Optional[int]
    flag_at: Optional[int]
    p_value: float

    @property
    def verdict(self) -> str:
        if self.promote_at is None:
            return "UNDERPOWERED"
        if self.positive >= self.promote_at:
            return "PROMOTE"
        if self.flag_at is not None and self.positive >= self.flag_at:
            return "FLAG"
        return "REJECT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "test": "one_sided_binomial_sign_test",
            "alpha": self.alpha,
            "slices_evaluated": self.evaluated,
            "slices_positive": self.positive,
            "promote_at": self.promote_at,
            "flag_at": self.flag_at,
            "p_value": self.p_value,
            "verdict": self.verdict,
            "thresholds_are_derived": (
                "promote_at is the smallest k with one-sided binomial p <= alpha; "
                "at 10 slices this is 9 (p=0.0107) with 8 as the flag (p=0.0547). "
                "A 4-slice screen cannot reach alpha=0.05 at all - its smallest "
                "attainable p is 0.0625 - and reports UNDERPOWERED."),
            "slices": [s.to_dict() for s in self.slices],
        }


def sign_test(slices: Sequence[Slice], *, alpha: float = 0.05) -> SignTest:
    """Promote / flag / reject from the signs of per-slice mean R.

    Magnitudes are deliberately ignored. A slice holds too few trades to resolve
    the effect sizes in play, so its mean is noise; its sign still carries one bit,
    and ten bits is a real test.
    """
    if not 0.0 < alpha < 1.0:
        raise PartitionError(f"alpha must be in (0, 1), got {alpha}")
    usable = tuple(s for s in slices if s.positive is not None)
    positive = sum(1 for s in usable if s.positive)
    n = len(usable)
    return SignTest(
        slices=usable, alpha=alpha, positive=positive, evaluated=n,
        promote_at=_threshold(n, alpha),
        flag_at=(None if n == 0 else max(0, (_threshold(n, alpha) or n + 1) - 1)),
        p_value=_binomial_tail(positive, n),
    )
