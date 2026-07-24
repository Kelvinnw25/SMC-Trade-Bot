"""AnalysisStage — the uniform contract every analysis stage implements.

One shape for every stage: `AnalysisContext -> AnalysisContext`. That uniformity
is what lets the pipeline be a plain ordered composition, lets stages be tested
and swapped independently, and lets a future stage be inserted without touching
the engine.

A stage must be a **pure function of its input context**: no I/O, no clock, no
state kept between calls. Stages are constructed once and may be reused, so they
must not accumulate anything on `self`.
"""
from __future__ import annotations

from typing import NoReturn, Protocol, runtime_checkable

from v2.analysis.context import AnalysisContext


@runtime_checkable
class AnalysisStage(Protocol):
    """One step of the analysis pipeline.

    Attributes:
        name: stable identifier, used for pipeline introspection and for
            locating a failure to a specific stage.
    """

    name: str

    def run(self, context: AnalysisContext) -> AnalysisContext:
        """Derive this stage's product and return an enriched context."""
        ...


def not_implemented(stage_name: str) -> NoReturn:
    """Raise the standard "skeleton only" error for an unimplemented stage.

    Phase 3.0 establishes the architecture; every stage's behavior is ported in
    a later, separately-approved milestone. Raising (rather than silently
    returning the input unchanged) keeps the absence of behavior loud — no
    caller can mistake a pass-through for a real result.
    """
    raise NotImplementedError(
        f"analysis stage '{stage_name}' has no behavior yet: Phase 3.0 is the "
        f"architecture skeleton. Behavior is ported in a later milestone."
    )
