"""AnalysisPipeline — the ordered composition of analysis stages.

This module is the single place the analysis **order** is declared. The order is
not incidental: it follows the dependency chain of the roadmap's build order
(§2) — each stage consumes what the previous ones produced.

    HTF → Regime → Structure → Liquidity → Order Block → Fair Value Gap
        → Entry → Risk → Scoring → Signal

Running the pipeline is a plain left fold of stages over an `AnalysisContext`.
There is no branching, no conditional dispatch, and no state held between runs —
so the pipeline itself can never contain business logic. Anything that decides a
*signal outcome* belongs inside a stage.

Stages are injected (constructor) with `default()` supplying the canonical
chain, so a caller can compose a partial or instrumented pipeline — e.g. running
the first N stages when validating a single stage against V1 — without touching
the engine.
"""
from __future__ import annotations

from collections.abc import Sequence

from v2.analysis.context import AnalysisContext
from v2.analysis.entry import EntryStage
from v2.analysis.fair_value_gap import FairValueGapStage
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage
from v2.analysis.order_block import OrderBlockStage
from v2.analysis.regime import RegimeStage
from v2.analysis.risk import RiskStage
from v2.analysis.scoring import ScoringStage
from v2.analysis.stage import AnalysisStage
from v2.analysis.structure import StructureStage


def default_stages() -> tuple[AnalysisStage, ...]:
    """The canonical analysis chain, in dependency order.

    Adding a stage means inserting it here (and adding its context field) —
    nothing else in the layer changes.
    """
    return (
        HTFStage(),
        RegimeStage(),
        StructureStage(),
        LiquidityStage(),
        OrderBlockStage(),
        FairValueGapStage(),
        EntryStage(),
        RiskStage(),
        ScoringStage(),
    )


class AnalysisPipeline:
    """An ordered, immutable sequence of stages applied to a context."""

    def __init__(self, stages: Sequence[AnalysisStage]) -> None:
        self._stages: tuple[AnalysisStage, ...] = tuple(stages)

    @classmethod
    def default(cls) -> "AnalysisPipeline":
        """The full analysis chain."""
        return cls(default_stages())

    @property
    def stages(self) -> tuple[AnalysisStage, ...]:
        return self._stages

    @property
    def stage_names(self) -> tuple[str, ...]:
        """Stage identifiers in execution order (pipeline introspection)."""
        return tuple(stage.name for stage in self._stages)

    def run(self, context: AnalysisContext) -> AnalysisContext:
        """Apply every stage in order, threading the enriched context through."""
        for stage in self._stages:
            context = stage.run(context)
        return context
