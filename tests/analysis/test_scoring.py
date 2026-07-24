"""Phase 3.9: Scoring + Final Analyzer — behavioral equivalence with V1.

Layers of proof:

1. **`score_signal` in isolation** — full return-dict equality against V1's
   method over a large seeded matrix of controlled htf_map / regime / h1_setup /
   m5_setup dicts, exercising every gate, weight, bonus, note, RR branch and
   decision tier without any clock or pipeline involved.
2. **The whole analyzer, end-to-end** — the V2 pipeline's Signal compared
   field-for-field against V1's `build_signal_context` result across the
   deterministic corpus.
3. **Regression compatibility** — the V2 result, mapped to `ObservableBehavior`
   by the test-side bridge, is compared to `V1Oracle` on the committed fixture
   and across the corpus via the frozen comparison engine (`compare_observables`).
4. **M5 re-score** — `enrich_with_m5` compared against V1's method on scored
   contexts.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import random
import unittest
from pathlib import Path

import pandas as pd

from src.strategy import SMCStrategy
from tests.analysis.observable_bridge import v2_observable
from tests.analysis.test_order_block import (
    build_frame,
    corpus,
    snapshot_of,
    snapshot_with,
)
from tests.regression.compare import compare_observables
from tests.regression.oracle import V1Oracle
from tests.regression.snapshot import InputSnapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.engine import V2Analyzer
from v2.analysis.entry import EntryStage
from v2.analysis.fair_value_gap import FairValueGapStage
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage
from v2.analysis.order_block import OrderBlockStage
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.regime import RegimeStage
from v2.analysis.risk import RiskStage
from v2.analysis.scoring import (
    ScoringStage,
    enrich_with_m5,
    is_market_choppy,
    score_signal,
)
from v2.analysis.structure import StructureStage
from v2.domain import MarketSnapshot, Signal, SignalCandidate

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)

# V1's session bonus and M5 swing params read the wall clock; capture V1's value
# once so end-to-end comparisons inject exactly what V1 used this run.
SESSION_ACTIVE = SMCStrategy().get_session_mode() == "SNIPER"


def _diff(report) -> str:
    """One-line-per-field summary of a failing ComparisonReport."""
    return "\n".join(
        f"{fc.path}: v1={fc.expected!r} v2={fc.actual!r}" for fc in report.failures
    )


def v1_context(d1, h1):
    """V1's context from a **fresh** SMCStrategy.

    V1's `_cached_bias` memoises D1/H4 bias keyed by the last timestamp. The
    synthetic corpus deliberately reuses identical timestamps across frames, so a
    *shared* instance would serve the first frame's cached bias to every later
    frame (a latent V1 collision that never fires in production, where distinct
    symbols carry distinct timestamps). V2 is cacheless and computes the true
    bias, so the faithful reference is a fresh instance per frame.
    """
    return SMCStrategy().build_signal_context(d1, h1)


# --------------------------------------------------------------------------
# Random dict generation for direct score_signal equivalence
# --------------------------------------------------------------------------

_BIASES = ("BULLISH", "BEARISH", "CHOPPY")
_LOCATIONS = ("discount", "premium", "equilibrium")
_REGIMES = (
    "expansion_up", "expansion_down", "accumulation", "distribution",
    "rotational", "compression", "neutral", "unknown",
)
_MODES = ("CONTINUATION", "RETRACEMENT", "WATCH_ONLY", "NONE", "")
_ENTRY_SOURCES = ("continuation_micro_pullback", "retracement_ob", "none")


def _rand_htf_map(rng: random.Random) -> dict:
    return {
        "weekly_bias": rng.choice(_BIASES),
        "d1_bias": rng.choice(_BIASES),
        "h4_bias": rng.choice(_BIASES),
        "location": rng.choice(_LOCATIONS),
        "momentum_weakening": rng.random() < 0.4,
    }


def _rand_h1_setup(rng: random.Random) -> dict:
    direction = rng.choice(("LONG", "SHORT", "", "NONE"))
    entry = rng.choice((0.0, 100.0, 250.0))
    sl_delta = rng.choice((0.0, 2.0, 5.0, 10.0))
    tp_delta = rng.choice((0.0, 3.0, 8.0, 20.0, 40.0))
    if direction == "LONG":
        sl = entry - sl_delta
        tp = entry + tp_delta
    else:
        sl = entry + sl_delta
        tp = entry - tp_delta
    return {
        "direction": direction,
        "entry_mode": rng.choice(_MODES),
        "entry_source": rng.choice(_ENTRY_SOURCES),
        "displacement_valid": rng.random() < 0.5,
        "volume_spike": rng.random() < 0.5,
        "sweep_valid": rng.random() < 0.5,
        "fvg_valid": rng.random() < 0.5,
        "fresh_ob": rng.random() < 0.5,
        "target_clear": rng.random() < 0.5,
        "session_active": rng.random() < 0.5,
        "htf_context_clean": rng.random() < 0.5,
        "entry_price": entry,
        "sl": sl,
        "tp": tp,
        "tp_source": rng.choice(("EQH_cluster", "fallback_2r", None)),
        "tp_rr": rng.choice((None, 2.0, 3.4)),
    }


def _rand_m5_setup(rng: random.Random, entry_mode: str) -> dict | None:
    if rng.random() < 0.4:
        return None
    return {"choch_confirmed": rng.random() < 0.6, "entry_mode": entry_mode}


class TestScoreSignalMatchesV1(unittest.TestCase):
    def test_matches_v1_over_a_large_random_matrix(self):
        v1 = SMCStrategy()
        rng = random.Random(20260725)
        for _ in range(3000):
            htf_map = _rand_htf_map(rng)
            regime = rng.choice(_REGIMES)
            h1_setup = _rand_h1_setup(rng)
            m5_setup = _rand_m5_setup(rng, h1_setup["entry_mode"])
            expected = v1.score_signal(htf_map, regime, dict(h1_setup), m5_setup)
            actual = score_signal(htf_map, regime, dict(h1_setup), m5_setup)
            if actual != expected:
                self.fail(
                    f"mismatch\nhtf={htf_map}\nregime={regime}\n"
                    f"h1={h1_setup}\nm5={m5_setup}\nv1={expected}\nv2={actual}"
                )

    def test_missing_input(self):
        v1 = SMCStrategy()
        for htf_map, h1_setup in (({}, {"direction": "LONG"}), ({"weekly_bias": "BULLISH"}, {})):
            self.assertEqual(
                score_signal(htf_map, "rotational", h1_setup),
                v1.score_signal(htf_map, "rotational", h1_setup),
            )

    def test_missing_direction(self):
        htf = {"weekly_bias": "BULLISH", "d1_bias": "BULLISH", "h4_bias": "BULLISH",
               "location": "discount", "momentum_weakening": False}
        for direction in ("", "SIDEWAYS", "none"):
            with self.subTest(direction=direction):
                h1 = {"direction": direction, "entry_mode": "RETRACEMENT"}
                self.assertEqual(
                    score_signal(htf, "rotational", h1),
                    SMCStrategy().score_signal(htf, "rotational", h1),
                )

    def test_bad_rr_short_circuits(self):
        htf = {"weekly_bias": "BULLISH", "d1_bias": "BULLISH", "h4_bias": "BULLISH",
               "location": "discount", "momentum_weakening": False}
        h1 = {"direction": "LONG", "entry_mode": "RETRACEMENT", "sweep_valid": True,
               "fresh_ob": True, "entry_price": 100.0, "sl": 90.0, "tp": 101.0}
        result = score_signal(htf, "rotational", dict(h1))
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reason"], "bad_rr")
        self.assertEqual(result, SMCStrategy().score_signal(htf, "rotational", dict(h1)))


# --------------------------------------------------------------------------
# is_market_choppy
# --------------------------------------------------------------------------

class TestIsMarketChoppy(unittest.TestCase):
    def test_matches_v1_across_corpus(self):
        v1 = SMCStrategy()
        for name, _d1, h1 in corpus():
            with self.subTest(frame=name):
                self.assertEqual(is_market_choppy(h1), v1._is_market_choppy(h1))

    def test_degenerate_frames(self):
        v1 = SMCStrategy()
        for df in (None, pd.DataFrame(), build_frame(n=5), build_frame(n=25)):
            self.assertEqual(is_market_choppy(df), v1._is_market_choppy(df))


# --------------------------------------------------------------------------
# End-to-end: the analyzer's Signal vs V1's build_signal_context
# --------------------------------------------------------------------------

class TestSignalMatchesV1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = [
            (name, d1, h1, v1_context(d1, h1)) for name, d1, h1 in corpus()
        ]

    def v2_signal(self, d1, h1) -> Signal:
        snapshot = snapshot_of(d1, h1)
        return V2Analyzer().analyze(snapshot, session_active=SESSION_ACTIVE)

    def test_score_decision_reason_notes_match_v1(self):
        for name, d1, h1, v1 in self.cases:
            result = v1["result"]
            signal = self.v2_signal(d1, h1)
            with self.subTest(frame=name):
                self.assertEqual(signal.score, result["score"])
                self.assertEqual(signal.decision, result["decision"])
                self.assertEqual(signal.reason, result.get("reason"))
                self.assertEqual(list(signal.notes), list(result.get("notes", [])))

    def test_direction_regime_entry_mode_match_v1(self):
        for name, d1, h1, v1 in self.cases:
            result = v1["result"]
            signal = self.v2_signal(d1, h1)
            with self.subTest(frame=name):
                self.assertEqual(signal.direction, v1["debug"]["htf"]["dir"])
                self.assertEqual(signal.regime, v1["debug"]["htf"]["regime"])
                self.assertEqual(signal.entry_mode, result["entry_mode"])

    def test_corpus_covers_every_decision_tier(self):
        tiers = {c[3]["result"]["decision"] for c in self.cases}
        for tier in ("SKIP", "WATCH", "VALID", "A+"):
            self.assertIn(tier, tiers)

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = v1_context(snapshot.d1, snapshot.h1)
        signal = V2Analyzer().analyze(snapshot, session_active=SESSION_ACTIVE)
        self.assertEqual(signal.score, v1["result"]["score"])
        self.assertEqual(signal.decision, v1["result"]["decision"])
        self.assertEqual(signal.reason, v1["result"]["reason"])


# --------------------------------------------------------------------------
# Regression compatibility (the bridge vs V1Oracle)
# --------------------------------------------------------------------------

class TestRegressionCompatibility(unittest.TestCase):
    def test_bridge_matches_v1oracle_on_the_committed_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        expected = V1Oracle().analyze(snapshot)
        actual = v2_observable(snapshot)
        report = compare_observables(expected, actual, label="fixture")
        self.assertTrue(report.ok, _diff(report))

    def test_bridge_matches_v1oracle_across_the_corpus(self):
        for name, d1, h1 in corpus():
            snapshot = snapshot_of(d1, h1)
            expected = V1Oracle().analyze(snapshot)  # fresh: see v1_context
            actual = v2_observable(snapshot, session_active=SESSION_ACTIVE)
            report = compare_observables(expected, actual, label=name)
            with self.subTest(frame=name):
                self.assertTrue(report.ok, _diff(report))

    def test_bridge_is_deterministic(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.assertEqual(v2_observable(snapshot), v2_observable(snapshot))


# --------------------------------------------------------------------------
# M5 enrichment vs V1
# --------------------------------------------------------------------------

def _m5_frame(n: int, rng: random.Random, base: float, drift: float) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="5min")
    closes, p = [], base
    for _ in range(n):
        p = max(1.0, p + drift + rng.gauss(0, 0.5))
        closes.append(p)
    opens = [base] + closes[:-1]
    highs = [max(o, c) + abs(rng.gauss(0, 0.5)) for o, c in zip(opens, closes)]
    lows = [min(o, c) - abs(rng.gauss(0, 0.5)) for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "timestamp": ts, "open": opens, "high": highs, "low": lows, "close": closes,
        "v": [500.0] * n, "atr": [0.8] * n,
    })


class TestM5Enrichment(unittest.TestCase):
    """`enrich_with_m5` re-scores a scored Signal exactly as V1's method does."""

    def test_matches_v1_on_scored_contexts(self):
        rng = random.Random(7)
        checked = 0
        for name, d1, h1 in corpus():
            # Fresh V1 instance for the base AND the enrich (see v1_context).
            strategy = SMCStrategy()
            context = strategy.build_signal_context(d1, h1)
            if int(context["result"].get("score", 0)) < 8:
                continue
            m5 = _m5_frame(
                40, random.Random(hash(name) & 0xFFFF), 200.0, rng.choice((-0.3, 0.3))
            )
            v1_result = strategy.enrich_with_m5(context, m5)

            snapshot = snapshot_of(d1, h1)
            base = V2Analyzer().analyze(snapshot, session_active=SESSION_ACTIVE)
            enriched = enrich_with_m5(base, m5, session_active=SESSION_ACTIVE)
            with self.subTest(frame=name):
                self.assertEqual(enriched.score, v1_result["score"])
                self.assertEqual(enriched.decision, v1_result["decision"])
                self.assertEqual(list(enriched.notes), list(v1_result.get("notes", [])))
                self.assertEqual(enriched.m5_choch, v1_result["m5_choch"])
                self.assertEqual(enriched.m5_reason, v1_result["m5_reason"])
            checked += 1
        self.assertGreater(checked, 0, "corpus produced no scored (>=8) context")

    def test_short_m5_returns_base_unchanged(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        base = V2Analyzer().analyze(snapshot)
        for m5 in (None, pd.DataFrame(), _m5_frame(4, random.Random(1), 100.0, 0.1)):
            self.assertIs(enrich_with_m5(base, m5, session_active=False), base)

    def test_analyzer_applies_m5_only_at_the_score_gate(self):
        # A SKIP (score < 8) snapshot with M5 present is not enriched.
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        with_m5 = MarketSnapshot(
            symbol=snapshot.symbol, d1=snapshot.d1, h1=snapshot.h1,
            h1_last_ts=snapshot.h1_last_ts, h1_last_ts_dt=snapshot.h1_last_ts_dt,
            live_price=snapshot.live_price, m5=_m5_frame(40, random.Random(2), 170.0, 0.1),
        )
        signal = V2Analyzer().analyze(with_m5)
        self.assertIsNone(signal.m5_choch)  # gate not passed -> no enrichment


# --------------------------------------------------------------------------
# The choppy-penalty quirk
# --------------------------------------------------------------------------

class TestChoppyPenalty(unittest.TestCase):
    def test_choppy_penalty_note_matches_v1_across_corpus(self):
        # The penalty is applied on any scored dict result (SKIP included); the
        # note's presence must agree with V1 on every frame that reaches scoring.
        for name, d1, h1 in corpus():
            v1_notes = v1_context(d1, h1)["result"].get("notes", [])
            signal = V2Analyzer().analyze(snapshot_of(d1, h1), session_active=SESSION_ACTIVE)
            with self.subTest(frame=name):
                self.assertEqual(
                    "choppy_penalty" in signal.notes, "choppy_penalty" in v1_notes
                )


# --------------------------------------------------------------------------
# Early exits
# --------------------------------------------------------------------------

class TestEarlyExits(unittest.TestCase):
    def test_invalid_input_when_frames_too_short(self):
        snapshot = snapshot_with(build_frame(n=5))
        signal = V2Analyzer().analyze(snapshot)
        self.assertEqual(signal.decision, "SKIP")
        self.assertEqual(signal.reason, "invalid_input")
        self.assertIsNone(signal.candidate)

    def test_not_ready_skip_matches_v1_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        signal = V2Analyzer().analyze(snapshot)
        self.assertEqual((signal.decision, signal.reason), ("SKIP", "ob_quality_failed"))
        self.assertIsNotNone(signal.candidate)  # not-ready keeps the candidate

    def test_early_exit_reasons_seen_in_v1(self):
        # The corpus exercises the not-ready and scored paths; the pure early
        # exits (invalid_input etc.) are covered by the degenerate test above.
        reasons = {
            v1_context(d1, h1)["result"].get("reason") for _n, d1, h1 in corpus()
        }
        # Not-ready entry exits surface as their entry_reason on the result.
        self.assertTrue(
            reasons
            & {"no_continuation_displacement", "no_micro_pullback",
               "non_actionable_regime", "ob_quality_failed"}
        )


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestScoringStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.context = self._pipeline_to_risk(self.snapshot)

    def _pipeline_to_risk(self, snapshot) -> AnalysisContext:
        context = AnalysisContext.initial(snapshot, session_active=SESSION_ACTIVE)
        for stage in (HTFStage(), RegimeStage(), StructureStage(), LiquidityStage(),
                      OrderBlockStage(), FairValueGapStage(), EntryStage(), RiskStage()):
            context = stage.run(context)
        return context

    def test_populates_signal_and_candidate(self):
        result = ScoringStage().run(self.context)
        self.assertIsInstance(result.signal, Signal)
        self.assertIsInstance(result.candidate, SignalCandidate)

    def test_signal_carries_the_candidate(self):
        result = ScoringStage().run(self.context)
        self.assertIs(result.signal.candidate, result.candidate)

    def test_does_not_mutate_input_context(self):
        ScoringStage().run(self.context)
        self.assertIsNone(self.context.signal)
        self.assertIsNone(self.context.candidate)

    def test_is_deterministic(self):
        self.assertEqual(ScoringStage().run(self.context), ScoringStage().run(self.context))

    def test_stage_name(self):
        self.assertEqual(ScoringStage().name, "scoring")

    def test_htf_none_yields_invalid_htf_map(self):
        # A frame long enough to pass safe_frame but with no HTF map.
        thin = AnalysisContext.initial(snapshot_with(build_frame(n=25)))
        signal = ScoringStage().run(thin).signal
        self.assertEqual(signal.decision, "SKIP")
        self.assertIn(signal.reason, {"invalid_input", "invalid_htf_map"})
        self.assertIsNone(signal.candidate)


class TestSignalCandidate(unittest.TestCase):
    def test_candidate_flags_match_v1_h1_setup(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)["h1_setup"]
        candidate = V2Analyzer().analyze(
            snapshot, session_active=SESSION_ACTIVE
        ).candidate
        for flag in ("sweep_valid", "fvg_valid", "displacement_valid", "fresh_ob",
                     "volume_spike", "target_clear", "htf_context_clean"):
            with self.subTest(flag=flag):
                self.assertEqual(getattr(candidate, flag), v1[flag])
        self.assertEqual(candidate.atr_last, v1["atr_last"])


# --------------------------------------------------------------------------
# Prior stages unchanged + module ownership
# --------------------------------------------------------------------------

class TestPriorStagesUnchanged(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)

    def test_all_eight_prior_stages_match_v1(self):
        context = AnalysisContext.initial(self.snapshot, session_active=SESSION_ACTIVE)
        for stage in (HTFStage(), RegimeStage(), StructureStage(), LiquidityStage(),
                      OrderBlockStage(), FairValueGapStage(), EntryStage(), RiskStage()):
            context = stage.run(context)
        h1 = self.v1["h1_setup"]
        self.assertEqual(context.htf.direction, self.v1["debug"]["htf"]["dir"])
        self.assertEqual(context.regime.label, self.v1["debug"]["htf"]["regime"])
        self.assertEqual(context.regime.entry_mode, h1["entry_mode"])
        self.assertEqual(context.atr_last, h1["atr_last"])
        self.assertEqual(context.sweep.detected, h1["sweep_valid"])
        self.assertEqual(context.fvg.valid, h1["fvg_valid"])
        self.assertEqual(context.entry.entry_source, h1["entry_source"])
        self.assertEqual(context.risk.sl, h1["sl"])
        self.assertEqual(context.risk.tp, h1["tp"])


class TestModuleOwnership(unittest.TestCase):
    def test_scoring_is_the_single_owner_of_score_signal(self):
        import v2.analysis.entry as entry_module
        import v2.analysis.htf as htf_module
        import v2.analysis.risk as risk_module

        for module in (htf_module, entry_module, risk_module):
            for name in ("score_signal", "is_market_choppy", "enrich_with_m5"):
                with self.subTest(module=module.__name__, symbol=name):
                    self.assertFalse(hasattr(module, name))

    def test_scoring_imports_no_lifecycle_io_or_clock(self):
        # Inspect the actual import statements (not the docstring prose, which
        # legitimately explains the injected-session rationale).
        import ast

        import v2.analysis.scoring as scoring_module

        tree = ast.parse(Path(scoring_module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden_prefixes = ("v2.infra", "v2.lifecycle", "v2.app", "tests",
                              "src", "datetime", "time", "os")
        for module in imported:
            with self.subTest(module=module):
                self.assertFalse(
                    any(module == p or module.startswith(p + ".") for p in forbidden_prefixes),
                    f"scoring must not import {module}",
                )


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def test_full_pipeline_produces_signal_and_candidate(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        context = AnalysisPipeline.default().run(
            AnalysisContext.initial(snapshot, session_active=SESSION_ACTIVE)
        )
        self.assertIsInstance(context.signal, Signal)
        self.assertIsInstance(context.candidate, SignalCandidate)

    def test_analyzer_returns_the_terminal_signal(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        context = AnalysisPipeline.default().run(
            AnalysisContext.initial(snapshot, session_active=SESSION_ACTIVE)
        )
        self.assertEqual(
            V2Analyzer().analyze(snapshot, session_active=SESSION_ACTIVE), context.signal
        )

    def test_analyzer_is_deterministic(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.assertEqual(V2Analyzer().analyze(snapshot), V2Analyzer().analyze(snapshot))


if __name__ == "__main__":
    unittest.main()
