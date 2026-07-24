"""Phase 3.0 architecture: pipeline wiring and V2Analyzer orchestration.

Wiring is proven with injected recording stages — no analysis behavior exists to
test. The real stages are asserted to have *no* behavior, which is the point of
this milestone.
"""
from __future__ import annotations

import unittest
from typing import Optional

from tests.analysis.test_analysis_context import make_snapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.engine import AnalysisError, V2Analyzer
from v2.analysis.pipeline import AnalysisPipeline, default_stages
from v2.analysis.stage import AnalysisStage
from v2.domain import Regime, Signal

EXPECTED_STAGE_ORDER = (
    "htf",
    "regime",
    "structure",
    "liquidity",
    "order_block",
    "fair_value_gap",
    "entry",
    "risk",
    "scoring",
)

# Stages whose behavior has been ported. Extend this as each phase lands, so the
# "no behavior yet" guarantee keeps protecting every stage still to come.
IMPLEMENTED_STAGES = frozenset(
    {"htf", "regime", "structure", "liquidity", "order_block", "fair_value_gap",
     "entry"}
)  # Phases 3.1-3.7

FIRST_UNIMPLEMENTED = next(
    name for name in EXPECTED_STAGE_ORDER if name not in IMPLEMENTED_STAGES
)


class RecordingStage:
    """Injected stand-in for a real stage: records invocation order and the
    context it received, and optionally enriches."""

    def __init__(self, name: str, *, sets: Optional[dict] = None, log: Optional[list] = None) -> None:
        self.name = name
        self._sets = sets or {}
        self.log = log if log is not None else []
        self.seen: list[AnalysisContext] = []

    def run(self, context: AnalysisContext) -> AnalysisContext:
        self.log.append(self.name)
        self.seen.append(context)
        return context.with_(**self._sets) if self._sets else context


def terminal_signal() -> Signal:
    return Signal(symbol="TEST/USDT", score=0, decision="SKIP", entry_mode="NONE")


class TestDefaultPipeline(unittest.TestCase):
    def test_declares_the_documented_stage_order(self):
        self.assertEqual(AnalysisPipeline.default().stage_names, EXPECTED_STAGE_ORDER)

    def test_default_stages_are_distinct_instances(self):
        stages = default_stages()
        self.assertEqual(len(stages), len(EXPECTED_STAGE_ORDER))
        self.assertEqual(len({id(s) for s in stages}), len(stages))

    def test_every_stage_satisfies_the_stage_contract(self):
        for stage in default_stages():
            with self.subTest(stage=stage.name):
                self.assertIsInstance(stage, AnalysisStage)
                self.assertTrue(hasattr(stage, "run"))

    def test_arbitrary_object_is_not_a_stage(self):
        self.assertNotIsInstance(object(), AnalysisStage)


class TestStagesHaveNoBehavior(unittest.TestCase):
    """Every stage not yet ported must still be unimplemented — the guarantee
    that a milestone implemented only what it claimed."""

    def test_unported_stages_raise_not_implemented(self):
        context = AnalysisContext.initial(make_snapshot())
        for stage in default_stages():
            if stage.name in IMPLEMENTED_STAGES:
                continue
            with self.subTest(stage=stage.name):
                with self.assertRaises(NotImplementedError) as caught:
                    stage.run(context)
                self.assertIn(stage.name, str(caught.exception))

    def test_ported_stages_run(self):
        context = AnalysisContext.initial(make_snapshot())
        for stage in default_stages():
            if stage.name not in IMPLEMENTED_STAGES:
                continue
            with self.subTest(stage=stage.name):
                self.assertIsInstance(stage.run(context), AnalysisContext)


class TestPipelineWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.context = AnalysisContext.initial(make_snapshot())

    def test_runs_stages_in_order(self):
        log: list[str] = []
        pipeline = AnalysisPipeline(
            [RecordingStage(n, log=log) for n in ("first", "second", "third")]
        )
        pipeline.run(self.context)
        self.assertEqual(log, ["first", "second", "third"])

    def test_threads_the_enriched_context_forward(self):
        regime = Regime(label="unknown", entry_mode="NONE")
        producer = RecordingStage("producer", sets={"regime": regime})
        consumer = RecordingStage("consumer")
        AnalysisPipeline([producer, consumer]).run(self.context)
        # The second stage saw what the first produced.
        self.assertIsNone(producer.seen[0].regime)
        self.assertEqual(consumer.seen[0].regime, regime)

    def test_returns_the_final_context(self):
        signal = terminal_signal()
        pipeline = AnalysisPipeline([RecordingStage("scoring", sets={"signal": signal})])
        self.assertEqual(pipeline.run(self.context).signal, signal)

    def test_empty_pipeline_is_identity(self):
        self.assertIs(AnalysisPipeline([]).run(self.context), self.context)

    def test_stage_names_reflect_injected_stages(self):
        pipeline = AnalysisPipeline([RecordingStage("a"), RecordingStage("b")])
        self.assertEqual(pipeline.stage_names, ("a", "b"))


class TestV2Analyzer(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = make_snapshot()

    def test_defaults_to_the_canonical_pipeline(self):
        self.assertEqual(V2Analyzer().pipeline.stage_names, EXPECTED_STAGE_ORDER)

    def test_uses_an_injected_pipeline(self):
        pipeline = AnalysisPipeline([RecordingStage("only")])
        self.assertIs(V2Analyzer(pipeline).pipeline, pipeline)

    def test_returns_the_pipelines_terminal_signal(self):
        signal = terminal_signal()
        analyzer = V2Analyzer(
            AnalysisPipeline([RecordingStage("scoring", sets={"signal": signal})])
        )
        self.assertEqual(analyzer.analyze(self.snapshot), signal)

    def test_raises_when_pipeline_produces_no_signal(self):
        analyzer = V2Analyzer(AnalysisPipeline([RecordingStage("noop")]))
        with self.assertRaises(AnalysisError):
            analyzer.analyze(self.snapshot)

    def test_default_pipeline_fails_at_the_first_unported_stage(self):
        # Proves the engine is wired to the pipeline and enters it in order:
        # ported stages run, and the chain stops at the first one that is not.
        with self.assertRaises(NotImplementedError) as caught:
            V2Analyzer().analyze(self.snapshot)
        self.assertIn(FIRST_UNIMPLEMENTED, str(caught.exception))

    def test_prior_is_accepted_but_never_reaches_the_pipeline(self):
        # `prior` exists for interface compatibility only (Phase 3 DoD: analysis
        # references no previous-signal state).
        signal = terminal_signal()
        without = RecordingStage("scoring", sets={"signal": signal})
        with_prior = RecordingStage("scoring", sets={"signal": signal})

        V2Analyzer(AnalysisPipeline([without])).analyze(self.snapshot)
        V2Analyzer(AnalysisPipeline([with_prior])).analyze(
            self.snapshot, prior=terminal_signal()
        )

        self.assertEqual(without.seen[0], with_prior.seen[0])


if __name__ == "__main__":
    unittest.main()
