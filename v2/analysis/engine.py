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
from v2.analysis.scoring import enrich_with_m5
from v2.domain import MarketSnapshot, Signal

__all__ = ["V2Analyzer", "AnalysisError"]

# V1 requests M5 enrichment only at its decision gate: score >= 8
# (main.py:352). Mirrors the same gate the regression oracle applies.
_M5_SCORE_GATE = 8


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
        self,
        snapshot: MarketSnapshot,
        *,
        prior: Optional[Signal] = None,
        session_active: bool = False,
    ) -> Signal:
        """Run the analysis chain over `snapshot` and return its `Signal`.

        The pipeline produces the pre-M5 Signal (mirroring V1's
        `build_signal_context`); this method then applies the M5 re-score under
        V1's exact gate — `score >= 8` and an M5 frame present — exactly as V1's
        `run()` loop invokes `enrich_with_m5` after building the context. That
        one conditional is the analysis-layer counterpart of V1's own two-call
        structure, not business logic leaking into the engine.

        `session_active` is the injected session flag (V1's `get_session_mode()
        == "SNIPER"`, a wall-clock read analysis must not perform); it drives the
        scoring session bonus and the M5 swing parameters, defaulting to False.

        `prior` is accepted for interface compatibility and is intentionally
        unused — see the module docstring. Raises `AnalysisError` if the pipeline
        completes without producing a Signal.
        """
        context = AnalysisContext.initial(snapshot, session_active=session_active)
        context = self._pipeline.run(context)

        signal = context.signal
        if signal is None:
            raise AnalysisError(
                "analysis pipeline completed without producing a Signal "
                f"(stages: {self._pipeline.stage_names})"
            )

        if signal.score >= _M5_SCORE_GATE and snapshot.m5 is not None:
            signal = enrich_with_m5(signal, snapshot.m5, session_active=session_active)

        return signal
