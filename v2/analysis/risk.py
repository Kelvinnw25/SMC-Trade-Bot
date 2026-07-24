"""Risk stage — stop-loss, take-profit target, and risk/reward.

Pipeline position: 8 of 9 (consumes the entry price; scoring's RR gate consumes
this stage's output).

Produces: `context.risk` (`RiskPlan`).

Will port from V1: `SMCStrategy.find_liquidity_target` and the stop/target
derivation in `build_signal_context`, including its fallback target path.

Must not: assign a decision tier — it produces levels, not a verdict.
"""
from __future__ import annotations

from v2.analysis.context import AnalysisContext
from v2.analysis.stage import not_implemented


class RiskStage:
    """Derives the stop-loss, take-profit target, and risk/reward."""

    name = "risk"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        not_implemented(self.name)
