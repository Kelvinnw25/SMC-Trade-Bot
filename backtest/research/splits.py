"""Walk-forward splits — deterministic, non-overlapping, leakage-free windows.

REPLAY_CONTRACT.md §11 step 8, C-5.7, C-4.10. Milestone M11, closing finding N12.

## The problem this solves

A backtest tuned and measured on the same window reports the fit, not the edge.
§11 step 8 requires "rolling out-of-sample windows plus a permanent holdout" before
anything is promoted, and nothing implemented it — so every result the platform
could produce was in-sample by construction. This module produces the windows; the
permanent holdout is `research.holdout`.

## Three windows per split, and why the third exists

    |<-- train -->|<-emb->|<-- validate -->|<-emb->|<-- test -->|

- **train** — where a hypothesis is formed and parameters are chosen.
- **validate** — where competing variants are compared. Selection happens here, so
  validate is *consumed* by the act of choosing: the winner's validate score is
  biased upward by the selection itself.
- **test** — evaluated **once**, after the choice is fixed. It is what the split
  actually reports.

Two windows would be enough for a single pre-registered hypothesis; three are
required the moment more than one variant is compared, which A1–A25 guarantees.
A caller who genuinely has one variant may set `validate_h1 = 0`.

## The embargo — the leakage this design exists to prevent

A trade opened in the last hour of `train` resolves up to `max_holding_h1` candles
later (C-4.10, default 168 = 7 days). Without a gap, its outcome is decided by
prices inside `validate`, and the split leaks. Every adjacent pair of windows is
therefore separated by an **embargo** defaulting to `max_holding_h1`, and the
default is the contract's cap rather than a round number precisely so that the gap
provably covers the longest trade the Outcome Engine can produce.

The embargo is dead time: it is scanned by nothing and evaluated by nothing.

## Burn-in is *inside* each window, not before it

Lifecycle state carries across cycles (C-5.7), so a window replayed from empty
memory produces unrepresentative early cycles. Each window therefore begins with
`burn_in_h1` candles whose trades are labeled `in_burn_in` and excluded from every
headline metric. `Window.evaluated_start` is where a window's *measurable* region
begins, and a window shorter than its own burn-in is rejected rather than silently
producing nothing.

**Consequence, stated plainly (C-5.7.4):** walk-forward splits are sharded runs,
each resetting lifecycle state at its own boundary. The contract requires the
canonical baseline to be a *single unsharded run*. Walk-forward therefore
**validates** a strategy; it never produces the baseline. `Splits.is_baseline` is
`False`, permanently, so no caller can confuse the two.

## Determinism

`walk_forward` is a pure function of `(window, spec)` — integer arithmetic on hour
boundaries, no clock, no data, no randomness. `splits_hash` covers the spec and
every derived boundary, so two researchers quoting the same hash are looking at the
same windows.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Sequence

from backtest.data.repository import canonical_json

__all__ = [
    "H1_SECONDS",
    "SPLIT_SCHEMA_VERSION",
    "Split",
    "SplitError",
    "Splits",
    "WalkForwardSpec",
    "Window",
    "walk_forward",
]

H1_SECONDS = 3600

#: Bumped when the split arithmetic or the serialized shape changes. It is part of
#: `splits_hash`, so a change in how windows are cut can never be mistaken for a
#: change in the strategy being measured.
SPLIT_SCHEMA_VERSION = 1

#: C-9.7's `max_holding_h1`. Restated as the embargo default because the two must
#: move together: an embargo shorter than the holding cap admits leakage.
DEFAULT_MAX_HOLDING_H1 = 168


class SplitError(ValueError):
    """A split configuration cannot describe a valid evaluation."""


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """A closed range of H1 scan boundaries `[start, end]`, epoch seconds.

    Inclusive at both ends, matching `ReplayConfig.start`/`end` (C-5.1), so a
    window converts to a replay window without an off-by-one. Both ends must lie
    on an hour, because a scan boundary that is not on an hour does not exist.
    """

    start: int
    end: int
    burn_in_h1: int = 0
    role: str = ""

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            if value % H1_SECONDS:
                raise SplitError(
                    f"{name}={value} is not on an hour boundary (C-5.1)")
        if self.end < self.start:
            raise SplitError(f"window end {self.end} precedes start {self.start}")
        if self.burn_in_h1 < 0:
            raise SplitError(f"burn_in_h1 must be >= 0, got {self.burn_in_h1}")
        if self.burn_in_h1 >= self.cycles:
            raise SplitError(
                f"{self.role or 'window'} [{self.start}, {self.end}] holds "
                f"{self.cycles} cycle(s) but declares burn_in_h1="
                f"{self.burn_in_h1}: every trade in it would be excluded from "
                f"every metric (C-5.7.1), so the window measures nothing.")

    @property
    def cycles(self) -> int:
        """Scan boundaries in the window, both ends inclusive."""
        return (self.end - self.start) // H1_SECONDS + 1

    @property
    def evaluated_start(self) -> int:
        """The first boundary whose trades are **not** `in_burn_in` (C-5.7.1)."""
        return self.start + self.burn_in_h1 * H1_SECONDS

    @property
    def evaluated_cycles(self) -> int:
        return self.cycles - self.burn_in_h1

    def overlaps(self, other: "Window") -> bool:
        """Any shared boundary at all. Touching endpoints count as overlapping:
        a boundary belongs to exactly one window, or the split is not a split."""
        return self.start <= other.end and other.start <= self.end

    def contains(self, boundary: int) -> bool:
        return self.start <= boundary <= self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "start": self.start,
            "end": self.end,
            "cycles": self.cycles,
            "burn_in_h1": self.burn_in_h1,
            "evaluated_start": self.evaluated_start,
            "evaluated_cycles": self.evaluated_cycles,
        }


# ---------------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkForwardSpec:
    """How a window is cut into splits. Pure configuration; hashed into identity.

    `anchored=True` grows the training window from a fixed origin (an expanding
    window: every split trains on all history to date). `anchored=False` rolls a
    fixed-length window forward, which adapts to regime change but discards old
    evidence. Neither is universally right, so neither is a hidden default —
    it is recorded in `splits_hash` and travels with every result.
    """

    train_h1: int
    test_h1: int
    validate_h1: int = 0
    step_h1: Optional[int] = None
    burn_in_h1: int = 336
    embargo_h1: int = DEFAULT_MAX_HOLDING_H1
    anchored: bool = False

    def __post_init__(self) -> None:
        if self.train_h1 <= 0:
            raise SplitError(f"train_h1 must be > 0, got {self.train_h1}")
        if self.test_h1 <= 0:
            raise SplitError(
                f"test_h1 must be > 0, got {self.test_h1}: a split with no "
                f"out-of-sample window measures nothing out of sample")
        if self.validate_h1 < 0:
            raise SplitError(f"validate_h1 must be >= 0, got {self.validate_h1}")
        if self.burn_in_h1 < 0:
            raise SplitError(f"burn_in_h1 must be >= 0, got {self.burn_in_h1}")
        if self.embargo_h1 < 0:
            raise SplitError(f"embargo_h1 must be >= 0, got {self.embargo_h1}")
        if self.step_h1 is not None and self.step_h1 <= 0:
            raise SplitError(f"step_h1 must be > 0, got {self.step_h1}")
        for name, length in (("train_h1", self.train_h1),
                             ("test_h1", self.test_h1)):
            if length <= self.burn_in_h1:
                raise SplitError(
                    f"{name}={length} does not exceed burn_in_h1="
                    f"{self.burn_in_h1}: the window would be entirely burn-in "
                    f"and contribute nothing to any metric (C-5.7.1)")
        if 0 < self.validate_h1 <= self.burn_in_h1:
            raise SplitError(
                f"validate_h1={self.validate_h1} does not exceed burn_in_h1="
                f"{self.burn_in_h1}")

    @property
    def step(self) -> int:
        """How far a split advances. Defaults to `test_h1`, which makes the
        out-of-sample windows exactly tile the period: every hour is tested once
        and no hour is tested twice."""
        return self.test_h1 if self.step_h1 is None else self.step_h1

    @property
    def span_h1(self) -> int:
        """One split's total length, embargoes included."""
        gaps = 2 * self.embargo_h1 if self.validate_h1 else self.embargo_h1
        return self.train_h1 + self.validate_h1 + self.test_h1 + gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_h1": self.train_h1,
            "validate_h1": self.validate_h1,
            "test_h1": self.test_h1,
            "step_h1": self.step,
            "burn_in_h1": self.burn_in_h1,
            "embargo_h1": self.embargo_h1,
            "anchored": self.anchored,
        }


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Split:
    """One fold: where to fit, where to choose, where to report."""

    index: int
    train: Window
    test: Window
    validate: Optional[Window] = None

    @property
    def windows(self) -> tuple[Window, ...]:
        parts = [self.train]
        if self.validate is not None:
            parts.append(self.validate)
        parts.append(self.test)
        return tuple(parts)

    @property
    def start(self) -> int:
        return self.train.start

    @property
    def end(self) -> int:
        return self.test.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "train": self.train.to_dict(),
            "validate": None if self.validate is None else self.validate.to_dict(),
            "test": self.test.to_dict(),
        }


@dataclass(frozen=True)
class Splits:
    """A complete walk-forward plan over one period."""

    spec: WalkForwardSpec
    window: Window
    splits: tuple[Split, ...]

    #: Permanently `False`. C-5.7.4 requires the canonical baseline to be a single
    #: unsharded run, and every split resets lifecycle state at its own boundary.
    #: Walk-forward validates; it does not baseline.
    is_baseline: bool = False

    def __len__(self) -> int:
        return len(self.splits)

    def __iter__(self) -> Iterator[Split]:
        return iter(self.splits)

    def __getitem__(self, index: int) -> Split:
        return self.splits[index]

    @property
    def test_windows(self) -> tuple[Window, ...]:
        return tuple(split.test for split in self.splits)

    @property
    def optimization_windows(self) -> tuple[Window, ...]:
        """Every window a decision may be taken on — train and validate.

        The holdout guard consumes exactly this: `test` is out-of-sample but is
        still *reported*, whereas these are the windows a human is allowed to look
        at while choosing. Naming them explicitly is what lets
        `holdout.assert_untouched` mean something precise.
        """
        windows: list[Window] = []
        for split in self.splits:
            windows.append(split.train)
            if split.validate is not None:
                windows.append(split.validate)
        return tuple(windows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "split_schema_version": SPLIT_SCHEMA_VERSION,
            "spec": self.spec.to_dict(),
            "window": self.window.to_dict(),
            "splits": [split.to_dict() for split in self.splits],
            "count": len(self.splits),
            "is_baseline": self.is_baseline,
            "splits_hash": self.splits_hash,
        }

    @property
    def splits_hash(self) -> str:
        """Identity over the spec and every derived boundary.

        Excludes `splits_hash` itself and includes the schema version, so a change
        in split arithmetic surfaces as a different hash rather than as two plans
        that look the same and are not.
        """
        payload = {
            "split_schema_version": SPLIT_SCHEMA_VERSION,
            "spec": self.spec.to_dict(),
            "window": [self.window.start, self.window.end],
            "splits": [
                [split.index,
                 split.train.start, split.train.end,
                 -1 if split.validate is None else split.validate.start,
                 -1 if split.validate is None else split.validate.end,
                 split.test.start, split.test.end]
                for split in self.splits
            ],
        }
        return hashlib.sha256(
            canonical_json(payload).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

def walk_forward(window: Window, spec: WalkForwardSpec, *,
                 exclude: Sequence[Window] = ()) -> Splits:
    """Cut `window` into walk-forward splits. Pure and deterministic.

    Splits are emitted while a whole one fits. A trailing remainder is **dropped**,
    never stretched into a short final split: a test window of a different length
    is not comparable with the others, and silently comparing them is how a
    walk-forward result stops meaning anything.

    `exclude` names windows no split may touch — the permanent holdout. A split
    overlapping one is dropped, and dropping is correct rather than an error: the
    holdout is normally the tail of the period, so the last few splits *should*
    disappear. `holdout.assert_untouched` is what turns a violation into a failure;
    this is what makes the common case need no special handling.
    """
    if spec.span_h1 > window.cycles:
        raise SplitError(
            f"one split spans {spec.span_h1} cycle(s) but the window holds "
            f"{window.cycles}. Shorten train/validate/test or lengthen the "
            f"window; a split that does not fit cannot be shortened to fit "
            f"without changing what it measures.")

    splits: list[Split] = []
    origin = window.start
    offset = 0
    index = 0
    while True:
        train_start = origin if spec.anchored else origin + offset * H1_SECONDS
        train_end_exclusive = (origin + (offset + spec.train_h1) * H1_SECONDS)
        cursor = train_end_exclusive + spec.embargo_h1 * H1_SECONDS

        validate: Optional[Window] = None
        if spec.validate_h1:
            validate_end_exclusive = cursor + spec.validate_h1 * H1_SECONDS
            validate = Window(
                start=cursor, end=validate_end_exclusive - H1_SECONDS,
                burn_in_h1=spec.burn_in_h1, role="validate")
            cursor = validate_end_exclusive + spec.embargo_h1 * H1_SECONDS

        test_end_exclusive = cursor + spec.test_h1 * H1_SECONDS
        if test_end_exclusive - H1_SECONDS > window.end:
            break

        train = Window(
            start=train_start, end=train_end_exclusive - H1_SECONDS,
            burn_in_h1=spec.burn_in_h1, role="train")
        test = Window(
            start=cursor, end=test_end_exclusive - H1_SECONDS,
            burn_in_h1=spec.burn_in_h1, role="test")
        candidate = Split(index=index, train=train, validate=validate, test=test)

        if not any(part.overlaps(blocked)
                   for part in candidate.windows for blocked in exclude):
            splits.append(candidate)
            index += 1
        offset += spec.step

    if not splits:
        raise SplitError(
            "no split fits: every candidate either exceeded the window or "
            "overlapped an excluded window (the holdout). Shorten the splits or "
            "extend the corpus — never shrink the holdout to make room.")

    result = Splits(spec=spec, window=window, splits=tuple(splits))
    _assert_disjoint(result)
    return result


def _assert_disjoint(splits: Splits) -> None:
    """Strict non-overlap *within* a split, checked rather than assumed.

    Across splits, train windows deliberately overlap — that is what rolling
    forward means, and with `anchored=True` every train window shares an origin.
    What may never overlap is the three windows of one split, and the test windows
    of different splits: a test window evaluated twice would be counted twice.
    """
    for split in splits:
        parts = split.windows
        for i, first in enumerate(parts):
            for second in parts[i + 1:]:
                if first.overlaps(second):
                    raise SplitError(
                        f"split {split.index}: {first.role} "
                        f"[{first.start}, {first.end}] overlaps {second.role} "
                        f"[{second.start}, {second.end}] — the embargo is too "
                        f"short or the arithmetic is wrong")
    tests = splits.test_windows
    for i, first in enumerate(tests):
        for second in tests[i + 1:]:
            if first.overlaps(second):
                raise SplitError(
                    f"test windows [{first.start}, {first.end}] and "
                    f"[{second.start}, {second.end}] overlap: with step_h1 < "
                    f"test_h1 the same hours are reported by two splits and "
                    f"out-of-sample evidence is double-counted")
