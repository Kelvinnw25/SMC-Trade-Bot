"""AnalysisContext — the immutable intermediate state threaded through the
analysis pipeline.

Architecture ref: 02-software-architecture.md §4 (analysis is pure);
03-implementation-roadmap.md Phase 3 ("a pure function chain that turns a
MarketSnapshot into a Signal, producing every intermediate ... as inspectable
immutable objects").

Design: **incremental enrichment, never mutation.** A stage receives a context,
produces exactly the domain object it owns, and returns a NEW context carrying
it (`context.with_(htf=...)`). Nothing is ever overwritten in place, so the
context at any point is a complete, inspectable record of what has been derived
so far — which is precisely what makes stage-by-stage regression diffing
possible later.

Every enrichment field is `Optional` and starts as None: a context is valid at
any stage of completion, and an early exit simply leaves later fields unset.

**Deliberately absent: prior-signal state.** Phase 3's Definition of Done
requires that no analysis module reference memory, persistence, notification,
clock, or previous-signal state (Decision #004; PROJECT_STATE.md §11). The
engine's public API accepts a `prior` argument for interface compatibility, but
it is never placed here, so no stage can reach it.

Extension rule: when a stage needs to carry a value that is not one of the
domain objects below, add an explicit, typed field here — never a generic
"extras" bag. Extension must stay reviewable.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional, Tuple

from v2.domain import (
    EntryPlan,
    FairValueGap,
    HTFContext,
    LiquiditySweep,
    MarketSnapshot,
    OrderBlock,
    Regime,
    RiskPlan,
    Signal,
    SignalCandidate,
    Swing,
)


@dataclass(frozen=True)
class AnalysisContext:
    """Immutable carrier of the analysis input plus everything derived so far.

    Fields:
        snapshot: the analysis input; always present, never replaced.
        htf .. risk: one field per pipeline stage, populated by that stage.
        candidate: the assembled `SignalCandidate` (pre-decision).
        signal: the terminal product; set by the final stage and returned by
            the engine.
    """

    # -- input (always present) --
    snapshot: MarketSnapshot

    # -- stage products, in pipeline order --
    htf: Optional[HTFContext] = None
    regime: Optional[Regime] = None
    # structure stage: V1 publishes these as separate values (and
    # SignalCandidate consumes them as separate fields), so they are separate
    # typed fields here rather than one bundled object.
    swing_highs: Optional[Tuple[Swing, ...]] = None
    swing_lows: Optional[Tuple[Swing, ...]] = None
    atr_last: Optional[float] = None
    displacement_valid: Optional[bool] = None
    volume_spike: Optional[bool] = None
    sweep: Optional[LiquiditySweep] = None
    order_blocks: Optional[Tuple[OrderBlock, ...]] = None
    fvg: Optional[FairValueGap] = None
    entry: Optional[EntryPlan] = None
    risk: Optional[RiskPlan] = None

    # -- terminal products --
    candidate: Optional[SignalCandidate] = None
    signal: Optional[Signal] = None

    @classmethod
    def initial(cls, snapshot: MarketSnapshot) -> "AnalysisContext":
        """The entry state: the input and nothing derived yet."""
        return cls(snapshot=snapshot)

    def with_(self, **changes: object) -> "AnalysisContext":
        """Return a new context with `changes` applied — the single enrichment
        primitive. Raises TypeError for an unknown field name, so a typo cannot
        silently create hidden state.

        By convention a stage sets only the field(s) it owns; it never rewrites
        an upstream stage's product.
        """
        return dataclasses.replace(self, **changes)
