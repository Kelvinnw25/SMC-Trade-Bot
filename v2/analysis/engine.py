"""V2Analyzer — the analysis layer's public entry point.

Architecture ref: 03-implementation-roadmap.md Phase 3 ("a pure function chain
that turns a MarketSnapshot into a Signal"); 02-software-architecture.md §4.

The engine **only orchestrates**: build the initial context, run the pipeline,
return the terminal `Signal`. It contains no calculation and no branching on
market data. If a conditional that decides a *signal outcome* ever appears here,
logic has leaked upward and must be pushed back into a stage.

Two interface decisions worth understanding before changing this file:

1. **It returns a domain `Signal`, not the harness's `ObservableBehavior`.**
   The regression harness lives outside runtime and no runtime module may import
   it (PROJECT_STATE.md §5 rule A8). So analysis produces the domain-native
   result, and the `Signal → ObservableBehavior` translation belongs to a
   harness-side adapter built when the comparison is wired up. Keeping the
   dependency pointing that way is what stops verification code from becoming a
   runtime dependency.

2. **`prior` is accepted but never used by analysis.** Phase 3's Definition of
   Done requires that no analysis module reference previous-signal state
   (Decision #004; PROJECT_STATE.md §11) — analysis decides *what the setup is*,
   lifecycle decides *what to do about it*. The parameter exists so this
   analyzer matches the comparison seam's shape; it is deliberately not placed
   into `AnalysisContext`, so no stage can reach it. Consuming it here would be
   an architecture violation, not a feature.
"""
from __future__ import annotations

from typing import Optional

from v2.analysis.context import AnalysisContext
from v2.analysis.pipeline import AnalysisPipeline
from v2.domain import MarketSnapshot, Signal

__all__ = ["V2Analyzer", "AnalysisError"]


class AnalysisError(Exception):
    """The analysis pipeline did not produce a Signal.

    Signals the pipeline was misconfigured (e.g. composed without a terminal
    scoring stage) rather than any market condition — a SKIP is a perfectly
    valid `Signal`, not an error.
    """


class V2Analyzer:
    """Turns a `MarketSnapshot` into a `Signal` by running the analysis pipeline.

    The pipeline is injected (defaulting to the canonical chain) so tests and
    stage-by-stage validation can supply a partial or instrumented pipeline —
    no globals, no singletons, no hidden state.
    """

    def __init__(self, pipeline: Optional[AnalysisPipeline] = None) -> None:
        self._pipeline = pipeline if pipeline is not None else AnalysisPipeline.default()

    @property
    def pipeline(self) -> AnalysisPipeline:
        return self._pipeline

    def analyze(
        self, snapshot: MarketSnapshot, *, prior: Optional[Signal] = None
    ) -> Signal:
        """Run the analysis chain over `snapshot` and return its `Signal`.

        `prior` is accepted for interface compatibility and is intentionally
        unused — see the module docstring. Raises `AnalysisError` if the
        pipeline completes without producing a Signal.
        """
        context = AnalysisContext.initial(snapshot)
        context = self._pipeline.run(context)

        if context.signal is None:
            raise AnalysisError(
                "analysis pipeline completed without producing a Signal "
                f"(stages: {self._pipeline.stage_names})"
            )
        return context.signal
