"""Rendering — turn `ComparisonReport`s into readable PASS/FAIL text.

Pure formatting: these functions return strings and never touch I/O, except the
optional `print_report` convenience. A report shows the overall verdict, a
per-field breakdown (failures always; passes on request), and a summary count —
enough to diagnose a regression down to the exact observable field.
"""
from __future__ import annotations

from typing import Iterable

from tests.regression.compare import ComparisonReport, FieldComparison

_BAR = "=" * 72


def render_report(report: ComparisonReport, *, show_passes: bool = False) -> str:
    """Render one input's comparison. Failures are always listed; passes only
    when `show_passes` is set."""
    verdict = "PASS" if report.ok else "FAIL"
    lines = [
        _BAR,
        f"[{verdict}] {report.label}",
        f"  fields: {report.passed}/{report.total} passed, {report.failed} failed",
    ]

    shown: Iterable[FieldComparison] = (
        report.fields if show_passes else report.failures
    )
    for fc in shown:
        lines.append(_render_field(fc))

    if report.ok and not show_passes:
        lines.append("  (no divergences)")
    lines.append(_BAR)
    return "\n".join(lines)


def render_suite(reports: Iterable[ComparisonReport], *, show_passes: bool = False) -> str:
    """Render a batch of comparisons plus an aggregate verdict line."""
    reports = list(reports)
    blocks = [render_report(r, show_passes=show_passes) for r in reports]

    total_failed = sum(r.failed for r in reports)
    diverged = [r.label for r in reports if not r.ok]
    summary = [
        "",
        f"SUITE: {len(reports)} input(s), {total_failed} field failure(s) across "
        f"{len(diverged)} diverging input(s).",
    ]
    if diverged:
        summary.append("  diverged: " + ", ".join(diverged))
    else:
        summary.append("  all inputs behaviorally equivalent.")
    return "\n".join(blocks + summary)


def _render_field(fc: FieldComparison) -> str:
    if fc.ok:
        return f"    PASS {fc.path} = {fc.expected!r}"
    detail = f" — {fc.detail}" if fc.detail else ""
    return (
        f"    FAIL {fc.path}: expected {fc.expected!r}, "
        f"got {fc.actual!r}{detail}"
    )


def print_report(report: ComparisonReport, *, show_passes: bool = False) -> None:
    print(render_report(report, show_passes=show_passes))
