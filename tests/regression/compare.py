"""The comparison engine — field-level PASS/FAIL between two observable results.

Given two flattened observable field maps (V1 vs V2, or live vs a stored
golden), this produces a `ComparisonReport`: one `FieldComparison` per observable
field, each PASS or FAIL with enough context to locate a regression (the path,
the expected value, the actual value, and a short reason).

Comparison policy (Roadmap §6): **exact equality is the default**, because V1's
values feed decision thresholds — "a rounding difference that flips a decision
tier is a real divergence, not noise." A `float_tolerance` is available strictly
for diagnosis (e.g. isolating a rounding drift from a logic error); it defaults
to 0.0 so the migration bar stays exact. NaN is treated as equal to NaN, and a
field present on only one side is always a FAIL (a dropped or added observable
is a divergence).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

PASS = "PASS"
FAIL = "FAIL"

_MISSING = object()


@dataclass(frozen=True)
class FieldComparison:
    """The verdict for one observable field."""

    path: str
    status: str
    expected: Any
    actual: Any
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PASS


@dataclass(frozen=True)
class ComparisonReport:
    """The verdict for one input: every field compared, most useful via `ok` /
    `failures`."""

    label: str
    fields: tuple[FieldComparison, ...]

    @property
    def failures(self) -> tuple[FieldComparison, ...]:
        return tuple(f for f in self.fields if f.status == FAIL)

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def passed(self) -> int:
        return sum(1 for f in self.fields if f.status == PASS)

    @property
    def total(self) -> int:
        return len(self.fields)

    @property
    def ok(self) -> bool:
        """True iff no observable field diverged."""
        return self.failed == 0


def compare_field_maps(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    label: str,
    float_tolerance: float = 0.0,
) -> ComparisonReport:
    """Compare two flattened observable maps field-by-field."""
    paths = sorted(set(expected) | set(actual))
    results: list[FieldComparison] = []
    for path in paths:
        exp = expected[path] if path in expected else _MISSING
        act = actual[path] if path in actual else _MISSING
        status, detail = _compare_leaf(exp, act, float_tolerance)
        results.append(
            FieldComparison(
                path=path,
                status=status,
                expected=_display(exp),
                actual=_display(act),
                detail=detail,
            )
        )
    return ComparisonReport(label=label, fields=tuple(results))


def compare_observables(
    expected: Any,
    actual: Any,
    *,
    label: str,
    float_tolerance: float = 0.0,
) -> ComparisonReport:
    """Compare two `ObservableBehavior` records (each must expose
    `as_field_map()`)."""
    return compare_field_maps(
        expected.as_field_map(),
        actual.as_field_map(),
        label=label,
        float_tolerance=float_tolerance,
    )


def _compare_leaf(expected: Any, actual: Any, tolerance: float) -> tuple[str, str]:
    if expected is _MISSING:
        return FAIL, "field present only in actual (added observable)"
    if actual is _MISSING:
        return FAIL, "field present only in expected (missing observable)"

    exp_is_num = isinstance(expected, (int, float)) and not isinstance(expected, bool)
    act_is_num = isinstance(actual, (int, float)) and not isinstance(actual, bool)

    if exp_is_num and act_is_num:
        exp_nan = isinstance(expected, float) and math.isnan(expected)
        act_nan = isinstance(actual, float) and math.isnan(actual)
        if exp_nan or act_nan:
            return (PASS, "") if exp_nan and act_nan else (FAIL, "NaN mismatch")
        if tolerance > 0.0:
            diff = abs(float(expected) - float(actual))
            if diff <= tolerance:
                return PASS, ""
            return FAIL, f"differs by {diff:g} (> tolerance {tolerance:g})"
        if expected == actual:
            return PASS, ""
        return FAIL, f"differs by {abs(float(expected) - float(actual)):g}"

    # Non-numeric (or bool vs number): require exact, type-aware equality.
    if type(expected) is not type(actual):
        return FAIL, f"type {type(expected).__name__} != {type(actual).__name__}"
    if expected == actual:
        return PASS, ""
    return FAIL, "values differ"


def _display(value: Any) -> Any:
    return "<missing>" if value is _MISSING else value
