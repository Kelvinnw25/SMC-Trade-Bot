"""Entry stage — the structural entry plan.

Pipeline position: 7 of 9 (consumes the structural stages above).

Produces: `context.entry` (`EntryPlan`).

Will port from V1: the entry engine inside `build_signal_context` (the
mode-specific entry derivation and its `entry_source` / readiness outcome).

Contract note: per Decision #001 ("Contract B") the entry price is a
**structural reference, not an executable order price**. The plan carries that
marker; nothing here implies order placement.

Must not: compute stop-loss or take-profit (risk stage) or assign a score.
"""
from __future__ import annotations

from v2.analysis.context import AnalysisContext
from v2.analysis.stage import not_implemented


class EntryStage:
    """Derives the structural entry plan."""

    name = "entry"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        not_implemented(self.name)
