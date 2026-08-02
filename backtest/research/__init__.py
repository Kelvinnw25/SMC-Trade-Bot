"""backtest.research — evaluation methodology (Milestone M11).

REPLAY_CONTRACT.md §11 steps 8–9, closing finding N12.

The evaluation *stack* answers "what happened". This package answers a different
question — "what may be concluded from it" — and the two are deliberately separate
modules, because a methodology that can see the results is a methodology that gets
adjusted until the results are good.

    walk_forward(window, spec) -> Splits      rolling train / validate / test
    holdout.declare(...)       -> Holdout     declared once, never optimized against
    protocol.evaluate_holdout  -> checked, recorded, counted

## Three properties worth stating

- **Nothing here computes a metric.** Every check reads a report's lineage: its
  window, its snapshot, its strategy version. Never its numbers.
- **Every window is integer arithmetic on hour boundaries.** No clock, no data, no
  randomness, so a split plan is reproducible from its `splits_hash` alone.
- **Walk-forward never produces the baseline.** Splits are sharded runs and C-5.7.4
  requires the canonical baseline to be a single unsharded run. `Splits.is_baseline`
  is permanently `False`.

## Dependency direction

`research -> metrics + data`. Nothing imports `research`, and `research` imports
nothing from `replay` or `outcome`: it reasons about finished reports, so it has no
reason to know how they were produced.
"""
from backtest.research.holdout import (
    HOLDOUT_LOCK_NAME,
    HOLDOUT_USES_NAME,
    Holdout,
    HoldoutError,
    HoldoutMismatch,
    HoldoutUse,
    HoldoutViolation,
    assert_is_holdout,
    assert_untouched,
    guard_splits,
    holdout_uses,
    usage_caution,
)
from backtest.research.protocol import (
    HoldoutEvaluation,
    ProtocolError,
    assert_optimization_report,
    assert_test_report,
    evaluate_holdout,
    report_window,
)
from backtest.research.splits import (
    DEFAULT_MAX_HOLDING_H1,
    H1_SECONDS,
    SPLIT_SCHEMA_VERSION,
    Split,
    SplitError,
    Splits,
    WalkForwardSpec,
    Window,
    walk_forward,
)

__all__ = [
    "DEFAULT_MAX_HOLDING_H1",
    "H1_SECONDS",
    "HOLDOUT_LOCK_NAME",
    "HOLDOUT_USES_NAME",
    "SPLIT_SCHEMA_VERSION",
    "Holdout",
    "HoldoutError",
    "HoldoutEvaluation",
    "HoldoutMismatch",
    "HoldoutUse",
    "HoldoutViolation",
    "ProtocolError",
    "Split",
    "SplitError",
    "Splits",
    "WalkForwardSpec",
    "Window",
    "assert_is_holdout",
    "assert_optimization_report",
    "assert_test_report",
    "assert_untouched",
    "evaluate_holdout",
    "guard_splits",
    "holdout_uses",
    "report_window",
    "usage_caution",
    "walk_forward",
]
