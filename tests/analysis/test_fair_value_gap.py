"""Phase 3.6: Fair Value Gap stage — behavioral equivalence with V1.

V1's FVG is a boolean and nothing more (see `v2/analysis/fair_value_gap.py` for
the audit of what V1 does and does not implement). That boolean is published on
every path where V1 completes its setup, so equivalence is asserted directly
against `h1_setup["fvg_valid"]` and `debug["conf"]["fvg"]` across a deterministic
corpus of frames driving V1's real `build_signal_context`, in both directions.

Controlled frames then exercise each branch and threshold: the bullish and
bearish gap tests, the scan window, the ATR-scaled gap floor, and the
candle-colour requirement.

Also asserts the prior stages are untouched: HTF, Regime, Structure, Liquidity
and Order Block still produce V1's exact values.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from tests.regression.v1_recorded import SMCStrategy
from tests.analysis.test_order_block import (
    build_frame,
    corpus,
    htf_context,
    set_bar,
    snapshot_of,
    snapshot_with,
)
from tests.regression.snapshot import InputSnapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.fair_value_gap import (
    FVG_LOOKBACK,
    FVG_MIN_GAP_ATR_MULT,
    FairValueGapStage,
    detect_fvg,
    scan_window,
)
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage, detect_sweep
from v2.analysis.order_block import OrderBlockStage, fresh_ob
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.regime import RegimeStage
from v2.analysis.structure import (
    STRUCTURE_PROMINENCE_MULT,
    STRUCTURE_SWING_WINDOW,
    StructureStage,
    identify_swings,
    to_swings,
)
from v2.domain import FairValueGap

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def bullish_gap(df: pd.DataFrame, i: int, *, low: float, span: float = 2.0):
    """Lift bar `i` clear of bar `i-2`'s high and close it bullish."""
    return set_bar(
        df, i, low=low, high=low + span, open_=low + span * 0.25, close=low + span * 0.75
    )


def bearish_gap(df: pd.DataFrame, i: int, *, high: float, span: float = 2.0):
    """Drop bar `i` clear of bar `i-2`'s low and close it bearish."""
    return set_bar(
        df, i, high=high, low=high - span, open_=high - span * 0.25,
        close=high - span * 0.75,
    )


def v2_fvg(d1: pd.DataFrame, h1: pd.DataFrame) -> FairValueGap:
    """Run the real pipeline prefix and return the FVG product."""
    snapshot = snapshot_of(d1, h1)
    context = AnalysisContext.initial(snapshot)
    context = context.with_(htf=HTFStage.analyze(snapshot))
    context = StructureStage().run(context)
    return FairValueGapStage().run(context).fvg


# --------------------------------------------------------------------------
# V1 equivalence
# --------------------------------------------------------------------------

class TestFvgMatchesV1(unittest.TestCase):
    """`h1_setup["fvg_valid"]` is V1's published FVG output; it is the whole
    observable surface, so matching it is matching V1."""

    @classmethod
    def setUpClass(cls):
        strategy = SMCStrategy()
        cls.cases = []
        for name, d1, h1 in corpus():
            cls.cases.append((name, d1, h1, strategy.build_signal_context(d1, h1)))

    def test_matches_v1_h1_setup_across_corpus(self):
        compared = 0
        for name, d1, h1, v1 in self.cases:
            h1_setup = v1.get("h1_setup")
            if not h1_setup:
                continue
            compared += 1
            with self.subTest(frame=name):
                self.assertEqual(v2_fvg(d1, h1).valid, h1_setup["fvg_valid"])
        self.assertGreater(compared, 20)

    def test_matches_v1_debug_conf_across_corpus(self):
        for name, d1, h1, v1 in self.cases:
            conf = (v1.get("debug") or {}).get("conf")
            if not conf:
                continue
            with self.subTest(frame=name):
                self.assertEqual(v2_fvg(d1, h1).valid, conf["fvg"])

    def test_matches_v1_ob_audit_where_published(self):
        compared = 0
        for name, d1, h1, v1 in self.cases:
            audit = (v1.get("debug") or {}).get("ob_audit")
            if not audit:
                continue
            compared += 1
            with self.subTest(frame=name):
                self.assertEqual(v2_fvg(d1, h1).valid, audit["fvg_valid"])
        self.assertGreater(compared, 5)

    def test_corpus_covers_both_directions_and_both_outcomes(self):
        directions, outcomes = set(), set()
        for _, d1, h1, v1 in self.cases:
            directions.add((v1.get("h1_setup") or {}).get("direction"))
            outcomes.add(v2_fvg(d1, h1).valid)
        self.assertIn("LONG", directions)
        self.assertIn("SHORT", directions)
        self.assertEqual(outcomes, {True, False})

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        fvg = v2_fvg(snapshot.d1, snapshot.h1)
        self.assertEqual(fvg.valid, v1["h1_setup"]["fvg_valid"])
        self.assertEqual(fvg.valid, v1["debug"]["conf"]["fvg"])

    def test_is_deterministic(self):
        _, d1, h1 = corpus()[0]
        self.assertEqual(v2_fvg(d1, h1), v2_fvg(d1, h1))

    def test_v1_parameters_preserved(self):
        self.assertEqual((FVG_MIN_GAP_ATR_MULT, FVG_LOOKBACK), (0.16, 12))


# --------------------------------------------------------------------------
# Bullish FVG (LONG)
# --------------------------------------------------------------------------

class TestBullishFvg(unittest.TestCase):
    def test_gap_above_the_bar_two_back_is_valid(self):
        # bar 23 high is 101; bar 25 low is 102 -> gap 1.0 >= 0.16
        df = bullish_gap(build_frame(), 25, low=102.0)
        self.assertTrue(detect_fvg("LONG", df, 1.0).valid)

    def test_no_gap_is_not_valid(self):
        self.assertFalse(detect_fvg("LONG", build_frame(), 1.0).valid)

    def test_bearish_close_rejects_the_gap(self):
        # Same displacement, but the candle closes down.
        df = set_bar(build_frame(), 25, low=102.0, high=104.0, open_=103.5, close=102.5)
        self.assertFalse(detect_fvg("LONG", df, 1.0).valid)

    def test_doji_close_rejects_the_gap(self):
        # V1 requires strict `close > open`.
        df = set_bar(build_frame(), 25, low=102.0, high=104.0, open_=103.0, close=103.0)
        self.assertFalse(detect_fvg("LONG", df, 1.0).valid)

    def test_gap_floor_is_atr_scaled(self):
        # Gap is 0.5: min_gap 0.16 accepts it, min_gap 0.64 rejects it.
        df = bullish_gap(build_frame(), 25, low=101.5)
        self.assertTrue(detect_fvg("LONG", df, 1.0).valid)
        self.assertFalse(detect_fvg("LONG", df, 4.0).valid)

    def test_gap_exactly_at_the_floor_passes(self):
        # V1 uses `>=`. 1.5625 * 0.16 is exactly 0.25 in binary floating point,
        # so the comparison sits precisely on the boundary.
        atr = 1.5625
        self.assertEqual(atr * FVG_MIN_GAP_ATR_MULT, 0.25)
        self.assertTrue(
            detect_fvg("LONG", bullish_gap(build_frame(), 25, low=101.25), atr).valid
        )
        self.assertFalse(
            detect_fvg("LONG", bullish_gap(build_frame(), 25, low=101.2), atr).valid
        )

    def test_bearish_gap_does_not_satisfy_a_long(self):
        df = bearish_gap(build_frame(), 25, high=97.0)
        self.assertFalse(detect_fvg("LONG", df, 1.0).valid)

    def test_measures_against_two_bars_back_not_one(self):
        # A tall bar two back blocks the gap...
        blocked = build_frame()
        set_bar(blocked, 24, high=200.0)
        bullish_gap(blocked, 26, low=102.0)
        self.assertFalse(detect_fvg("LONG", blocked, 1.0).valid)

        # ...while the immediate neighbour is ignored entirely.
        allowed = build_frame()
        set_bar(allowed, 25, high=200.0)
        bullish_gap(allowed, 26, low=102.0)
        self.assertTrue(detect_fvg("LONG", allowed, 1.0).valid)


# --------------------------------------------------------------------------
# Bearish FVG (SHORT)
# --------------------------------------------------------------------------

class TestBearishFvg(unittest.TestCase):
    def test_gap_below_the_bar_two_back_is_valid(self):
        # bar 23 low is 99; bar 25 high is 97 -> gap 2.0 >= 0.16
        df = bearish_gap(build_frame(), 25, high=97.0)
        self.assertTrue(detect_fvg("SHORT", df, 1.0).valid)

    def test_no_gap_is_not_valid(self):
        self.assertFalse(detect_fvg("SHORT", build_frame(), 1.0).valid)

    def test_bullish_close_rejects_the_gap(self):
        df = set_bar(build_frame(), 25, high=97.0, low=95.0, open_=95.5, close=96.5)
        self.assertFalse(detect_fvg("SHORT", df, 1.0).valid)

    def test_doji_close_rejects_the_gap(self):
        df = set_bar(build_frame(), 25, high=97.0, low=95.0, open_=96.0, close=96.0)
        self.assertFalse(detect_fvg("SHORT", df, 1.0).valid)

    def test_gap_floor_is_atr_scaled(self):
        # bar 23 low is 99; a high of 98.5 leaves a gap of 0.5.
        df = bearish_gap(build_frame(), 25, high=98.5)
        self.assertTrue(detect_fvg("SHORT", df, 1.0).valid)
        self.assertFalse(detect_fvg("SHORT", df, 4.0).valid)

    def test_gap_exactly_at_the_floor_passes(self):
        atr = 1.5625  # min_gap is exactly 0.25
        self.assertTrue(
            detect_fvg("SHORT", bearish_gap(build_frame(), 25, high=98.75), atr).valid
        )
        self.assertFalse(
            detect_fvg("SHORT", bearish_gap(build_frame(), 25, high=98.8), atr).valid
        )

    def test_bullish_gap_does_not_satisfy_a_short(self):
        df = bullish_gap(build_frame(), 25, low=102.0)
        self.assertFalse(detect_fvg("SHORT", df, 1.0).valid)


# --------------------------------------------------------------------------
# The scan window
# --------------------------------------------------------------------------

class TestScanWindow(unittest.TestCase):
    def test_bounds_match_v1(self):
        self.assertEqual(scan_window(29), (17, 29))
        self.assertEqual(scan_window(20), (8, 20))

    def test_floor_keeps_two_bars_of_history(self):
        self.assertEqual(scan_window(10), (2, 10))
        self.assertEqual(scan_window(3), (2, 3))

    def test_last_bar_is_included(self):
        # Unlike the order-block window, the FVG scan reaches the final bar.
        df = bullish_gap(build_frame(), 29, low=102.0)
        self.assertTrue(detect_fvg("LONG", df, 1.0).valid)

    def test_oldest_bar_in_window_is_included(self):
        df = bullish_gap(build_frame(), 17, low=102.0)
        self.assertTrue(detect_fvg("LONG", df, 1.0).valid)

    def test_bar_older_than_the_window_is_ignored(self):
        df = bullish_gap(build_frame(), 16, low=102.0)
        self.assertFalse(detect_fvg("LONG", df, 1.0).valid)

    def test_window_spans_thirteen_bars(self):
        start, end = scan_window(29)
        self.assertEqual(end - start + 1, 13)


# --------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------

class TestDegenerateInputs(unittest.TestCase):
    def test_none_frame(self):
        self.assertFalse(detect_fvg("LONG", None, 1.0).valid)

    def test_empty_frame(self):
        self.assertFalse(detect_fvg("LONG", build_frame(n=0), 1.0).valid)

    def test_frames_too_short_to_scan(self):
        for n in (1, 2):
            with self.subTest(rows=n):
                self.assertFalse(detect_fvg("LONG", build_frame(n=n), 1.0).valid)

    def test_three_bar_frame_is_scannable(self):
        df = bullish_gap(build_frame(n=3), 2, low=102.0)
        self.assertTrue(detect_fvg("LONG", df, 1.0).valid)

    def test_zero_atr_accepts_a_touching_gap(self):
        # min_gap becomes 0, so `low - high >= 0` is enough.
        df = bullish_gap(build_frame(), 25, low=101.0)
        self.assertTrue(detect_fvg("LONG", df, 0.0).valid)
        self.assertFalse(detect_fvg("LONG", df, 1.0).valid)

    def test_zero_atr_still_needs_the_right_candle_colour(self):
        df = set_bar(build_frame(), 25, low=101.0, high=103.0, open_=102.5, close=101.5)
        self.assertFalse(detect_fvg("LONG", df, 0.0).valid)

    def test_non_long_direction_takes_the_bearish_scan(self):
        bearish = bearish_gap(build_frame(), 25, high=97.0)
        bullish = bullish_gap(build_frame(), 25, low=102.0)
        for direction in (None, "", "NONE", "long"):
            with self.subTest(direction=direction):
                self.assertTrue(detect_fvg(direction, bearish, 1.0).valid)
                self.assertFalse(detect_fvg(direction, bullish, 1.0).valid)

    def test_reports_no_bounds(self):
        # V1 breaks out of the loop without recording index/top/bottom.
        fvg = detect_fvg("LONG", bullish_gap(build_frame(), 25, low=102.0), 1.0)
        self.assertTrue(fvg.valid)
        self.assertIsNone(fvg.index)
        self.assertIsNone(fvg.top)
        self.assertIsNone(fvg.bottom)


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestFairValueGapStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        base = AnalysisContext.initial(self.snapshot)
        self.context = StructureStage().run(
            base.with_(htf=HTFStage.analyze(self.snapshot))
        )

    def test_populates_fvg(self):
        result = FairValueGapStage().run(self.context)
        self.assertIsInstance(result.fvg, FairValueGap)

    def test_matches_v1_fvg_valid(self):
        result = FairValueGapStage().run(self.context)
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        self.assertEqual(result.fvg.valid, v1["h1_setup"]["fvg_valid"])

    def test_uses_the_prepared_h1_and_structural_atr(self):
        h1 = safe_frame(self.snapshot.h1, H1_MIN_ROWS)
        expected = detect_fvg(
            HTFStage.analyze(self.snapshot).direction, h1, self.context.atr_last
        )
        self.assertEqual(FairValueGapStage().run(self.context).fvg, expected)

    def test_populates_only_fvg(self):
        result = FairValueGapStage().run(self.context)
        for field in ("entry", "risk", "candidate", "signal"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_no_fvg_without_htf(self):
        bare = AnalysisContext.initial(self.snapshot)
        self.assertIsNone(FairValueGapStage().run(bare).fvg)

    def test_no_fvg_without_structure(self):
        no_structure = AnalysisContext.initial(self.snapshot).with_(
            htf=HTFStage.analyze(self.snapshot)
        )
        self.assertIsNone(FairValueGapStage().run(no_structure).fvg)

    def test_no_fvg_when_h1_unusable(self):
        thin = (
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
            .with_(htf=htf_context(), atr_last=1.0)
        )
        self.assertIsNone(FairValueGapStage().run(thin).fvg)

    def test_does_not_mutate_input_context(self):
        FairValueGapStage().run(self.context)
        self.assertIsNone(self.context.fvg)

    def test_is_deterministic(self):
        self.assertEqual(
            FairValueGapStage().run(self.context),
            FairValueGapStage().run(self.context),
        )

    def test_stage_name(self):
        self.assertEqual(FairValueGapStage().name, "fair_value_gap")


# --------------------------------------------------------------------------
# Extraction neutrality + module boundary
# --------------------------------------------------------------------------

class TestPriorStagesUnchanged(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.v1 = SMCStrategy().build_signal_context(
            self.snapshot.d1, self.snapshot.h1
        )

    def test_htf_unchanged(self):
        htf = HTFStage.analyze(self.snapshot)
        v1_map = self.v1["htf_map"]
        for field in ("weekly_bias", "d1_bias", "h4_bias", "location",
                      "pdh", "pdl", "daily_eq", "current_price"):
            with self.subTest(field=field):
                self.assertEqual(getattr(htf, field), v1_map[field])
        self.assertEqual(htf.direction, self.v1["debug"]["htf"]["dir"])

    def test_regime_unchanged(self):
        htf = HTFStage.analyze(self.snapshot)
        regime = RegimeStage.analyze(self.snapshot, htf)
        self.assertEqual(regime.label, self.v1["debug"]["htf"]["regime"])
        self.assertEqual(regime.entry_mode, self.v1["h1_setup"]["entry_mode"])

    def test_structure_unchanged(self):
        context = AnalysisContext.initial(self.snapshot).with_(
            htf=HTFStage.analyze(self.snapshot)
        )
        result = StructureStage().run(context)
        self.assertEqual(result.atr_last, self.v1["h1_setup"]["atr_last"])
        self.assertEqual(
            result.displacement_valid, self.v1["h1_setup"]["displacement_valid"]
        )
        self.assertEqual(result.volume_spike, self.v1["h1_setup"]["volume_spike"])

    def test_liquidity_unchanged(self):
        htf = HTFStage.analyze(self.snapshot)
        h1 = safe_frame(self.snapshot.h1, H1_MIN_ROWS)
        raw_highs, raw_lows = identify_swings(
            h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
        )
        sweep = detect_sweep(
            htf.direction, h1, to_swings(raw_highs, "high"), to_swings(raw_lows, "low")
        )
        self.assertEqual(sweep.detected, self.v1["h1_setup"]["sweep_valid"])

    def test_order_block_unchanged(self):
        context = AnalysisContext.initial(self.snapshot).with_(
            htf=HTFStage.analyze(self.snapshot)
        )
        result = OrderBlockStage().run(StructureStage().run(context))
        self.assertEqual(
            fresh_ob(result.order_blocks), self.v1["h1_setup"]["fresh_ob"]
        )

    def test_running_fvg_does_not_disturb_the_order_block_result(self):
        # V1 scans the FVG before the OB inside each direction branch; V2 runs
        # them the other way round. Both are pure, so the products are identical.
        context = StructureStage().run(
            AnalysisContext.initial(self.snapshot).with_(
                htf=HTFStage.analyze(self.snapshot)
            )
        )
        ob_first = FairValueGapStage().run(OrderBlockStage().run(context))
        fvg_first = OrderBlockStage().run(FairValueGapStage().run(context))
        self.assertEqual(ob_first.order_blocks, fvg_first.order_blocks)
        self.assertEqual(ob_first.fvg, fvg_first.fvg)


class TestModuleBoundary(unittest.TestCase):
    def test_fvg_owns_no_later_stage_behavior(self):
        import v2.analysis.fair_value_gap as fvg_module

        for name in (
            "entry_price", "build_entry", "find_liquidity_target",
            "score_signal", "build_signal", "detect_order_blocks",
        ):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(fvg_module, name))

    def test_fvg_detection_has_a_single_owner(self):
        import v2.analysis.htf as htf_module
        import v2.analysis.liquidity as liquidity_module
        import v2.analysis.order_block as ob_module
        import v2.analysis.regime as regime_module
        import v2.analysis.structure as structure_module

        for module in (htf_module, regime_module, structure_module,
                       liquidity_module, ob_module):
            for name in ("detect_fvg", "scan_window"):
                with self.subTest(module=module.__name__, symbol=name):
                    self.assertFalse(hasattr(module, name))


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def stages(self):
        return [HTFStage(), RegimeStage(), StructureStage(), LiquidityStage(),
                OrderBlockStage(), FairValueGapStage()]

    def test_first_six_stages_run_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot)
        )
        self.assertIsNotNone(result.htf)
        self.assertIsNotNone(result.regime)
        self.assertIsNotNone(result.swing_highs)
        self.assertIsNotNone(result.sweep)
        self.assertIsNotNone(result.order_blocks)
        self.assertIsInstance(result.fvg, FairValueGap)
        self.assertIsNone(result.entry)
        self.assertIsNone(result.signal)

    def test_fvg_skipped_when_htf_bails(self):
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
        )
        self.assertIsNone(result.htf)
        self.assertIsNone(result.fvg)


if __name__ == "__main__":
    unittest.main()
