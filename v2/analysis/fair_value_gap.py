"""Fair Value Gap stage — imbalance detection.

Pipeline position: 6 of 9 (the last of the structural confirmations).

Produces: `context.fvg` (`FairValueGap`).

Will port from V1: the fair-value-gap scan inside `build_signal_context`.

Must not: influence entry or scoring directly — it contributes a confirmation
that later stages consume.
"""
from __future__ import annotations

from v2.analysis.context import AnalysisContext
from v2.analysis.stage import not_implemented


class FairValueGapStage:
    """Detects the fair value gap (imbalance) confirmation."""

    name = "fair_value_gap"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        not_implemented(self.name)
