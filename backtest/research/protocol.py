"""The evaluation protocol — what a report is allowed to be used for.

REPLAY_CONTRACT.md §11 steps 6–9. Milestone M11.

`splits` produces windows and `holdout` guards one of them; this module is the
join between those two and the metrics engine. It answers one question about a
finished `BaselineReport`:

    is this report evidence for the claim someone is about to make with it?

Three claims, three checks:

| Claim | Function |
|---|---|
| "I tuned this on the training window" | `assert_optimization_report` |
| "this is the out-of-sample result of split *k*" | `assert_test_report` |
| "this is the permanent-holdout result" | `evaluate_holdout` |

The metrics engine is untouched: nothing here computes a number, and every check
reads a report's *lineage* rather than its values. That separation matters —
a guard that could see the results is a guard that could be tuned.

## Why the window comes from the data

A report's window is `lineage["window"]`, derived from the boundaries of the
records themselves. So a claim is checked against where the trades actually
were, not against a window someone declared. `declared_start`/`declared_end` are
cross-checked when present, and a disagreement is an error: it means the report
covers a different period than its author believed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from backtest.metrics.report import BaselineReport
from backtest.research import holdout as holdout_module
from backtest.research.holdout import (
    Holdout, HoldoutError, HoldoutMismatch, HoldoutUse,
)
from backtest.research.splits import H1_SECONDS, Split, Splits, Window

__all__ = [
    "HoldoutEvaluation",
    "ProtocolError",
    "assert_optimization_report",
    "assert_test_report",
    "evaluate_holdout",
    "report_window",
]


class ProtocolError(Exception):
    """A report is being used as evidence for something it cannot support."""


def report_window(report: BaselineReport, *, burn_in_h1: int = 0) -> Window:
    """The window a report actually covers, taken from its records.

    Built from `first_boundary`/`last_boundary` — where the trades are — because
    that is what the numbers describe. When the report also carries a declared
    window, the two must agree: a mismatch means the report is not about the period
    its author thinks it is, and every downstream comparison inherits that error.

    An empty population has no window; that is an error here rather than a
    permissive `None`, since "zero trades" can otherwise satisfy any claim.
    """
    window_info = report.lineage.get("window", {})
    first, last = window_info.get("first_boundary"), window_info.get("last_boundary")
    if first is None or last is None:
        raise ProtocolError(
            "the report covers no trades, so it has no window and cannot be "
            "evidence for a claim about one")

    declared_start = window_info.get("declared_start")
    declared_end = window_info.get("declared_end")
    if declared_start is not None and first < declared_start:
        raise ProtocolError(
            f"the report holds a trade at {first}, before its declared window "
            f"start {declared_start}")
    if declared_end is not None and last > declared_end:
        raise ProtocolError(
            f"the report holds a trade at {last}, after its declared window end "
            f"{declared_end}")

    return Window(
        start=int(declared_start if declared_start is not None else first),
        end=int(declared_end if declared_end is not None else last),
        burn_in_h1=burn_in_h1,
        role="report",
    )


def _snapshot(report: BaselineReport) -> Optional[str]:
    ids = report.lineage.get("data_snapshot_ids") or []
    if len(ids) != 1:
        return None
    return str(ids[0])


def _strategy_version(report: BaselineReport) -> str:
    versions = report.lineage.get("strategy_versions") or []
    return str(versions[0]) if len(versions) == 1 else "mixed"


# ---------------------------------------------------------------------------
# In-sample claims
# ---------------------------------------------------------------------------

def assert_optimization_report(report: BaselineReport, declared: Holdout) -> None:
    """This report may be looked at while choosing. It must clear the holdout."""
    holdout_module.assert_untouched(
        declared, report_window(report), purpose="optimization report")


def assert_test_report(report: BaselineReport, split: Split,
                       *, declared: Optional[Holdout] = None) -> None:
    """This report is split *k*'s out-of-sample result.

    The report's window must be the split's **test** window — not its train
    window, and not the whole span. Reporting a train-window result under a test
    label is the single easiest mistake to make in a walk-forward loop, and it is
    invisible in the numbers themselves.
    """
    window = report_window(report)
    if (window.start, window.end) != (split.test.start, split.test.end):
        role = "unknown"
        if (window.start, window.end) == (split.train.start, split.train.end):
            role = "the split's TRAIN window"
        elif (split.validate is not None
              and (window.start, window.end) == (split.validate.start,
                                                 split.validate.end)):
            role = "the split's VALIDATE window"
        raise ProtocolError(
            f"split {split.index}: the report covers [{window.start}, "
            f"{window.end}], which is {role}, not the test window "
            f"[{split.test.start}, {split.test.end}]. An in-sample result "
            f"labeled out-of-sample is worse than no result.")
    if declared is not None:
        holdout_module.assert_untouched(
            declared, window, purpose=f"split {split.index} test")


# ---------------------------------------------------------------------------
# The holdout claim
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HoldoutEvaluation:
    """The record of one sanctioned look at the permanent holdout."""

    holdout: Holdout
    use: HoldoutUse
    previous_uses: int
    caution: Optional[str]

    @property
    def is_first_look(self) -> bool:
        return self.previous_uses == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdout_id": self.holdout.holdout_id,
            "use": self.use.to_dict(),
            "previous_uses": self.previous_uses,
            "is_first_look": self.is_first_look,
            "caution": self.caution,
        }


def evaluate_holdout(
    root,
    report: BaselineReport,
    *,
    declared: Optional[Holdout] = None,
    note: str = "",
    now: Optional[str] = None,
) -> HoldoutEvaluation:
    """Check a report against the declared holdout and record the look.

    Order matters: every check runs **before** anything is written, so a rejected
    evaluation does not consume a use. The write is the last step and is
    append-only.

    Returns the evaluation, including the caution when this is not the first look.
    Callers are expected to print it — the count is only useful if someone sees it.
    """
    declared = declared if declared is not None else holdout_module.load(root)

    snapshot = _snapshot(report)
    if snapshot is None:
        raise HoldoutMismatch(
            "the report mixes data_snapshot_ids (or declares none), so it cannot "
            "be checked against a holdout, which is bound to exactly one "
            "snapshot (C-6.10)")

    window = report_window(report, burn_in_h1=declared.window.burn_in_h1)
    holdout_module.assert_is_holdout(declared, window, data_snapshot_id=snapshot)

    previous = [use for use in holdout_module.holdout_uses(root)
                if use.holdout_id == declared.holdout_id]
    caution = holdout_module.usage_caution(root, declared)
    use = holdout_module.record_use(
        root, declared,
        run_id=report.inputs.run_id or "unknown",
        strategy_version=_strategy_version(report),
        metrics_hash=report.metrics_hash,
        note=note, now=now)
    return HoldoutEvaluation(holdout=declared, use=use,
                             previous_uses=len(previous), caution=caution)
