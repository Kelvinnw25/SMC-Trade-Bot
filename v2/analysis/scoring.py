"""Scoring stage — assemble the candidate, score it, and emit the Signal.

Pipeline position: 9 of 9 (terminal).

Produces: `context.candidate` (`SignalCandidate`) and `context.signal`
(`Signal`) — the pipeline's terminal product, which the engine returns.

Will port from V1: `SMCStrategy.score_signal` and the decision-tier assignment,
plus the assembly of the confirmation flags that scoring consumes.

Boundary note: this stage is the only one that produces a `Signal`. It assigns
a decision tier but takes **no lifecycle action** — it does not consult memory,
alert history, or any previous signal (Decision #004/#005: analysis decides
*what the setup is*; lifecycle decides *what to do about it*).
"""
from __future__ import annotations

from v2.analysis.context import AnalysisContext
from v2.analysis.stage import not_implemented


class ScoringStage:
    """Assembles the candidate, scores it, and produces the terminal Signal."""

    name = "scoring"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        not_implemented(self.name)
