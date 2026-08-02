"""Phase 3.8: Risk stage — behavioral equivalence with V1.

Two layers of proof:

1. **The whole stage, end-to-end.** V1 publishes `sl`, `tp`, `tp_source` and
   `tp_rr` in `h1_setup` on every completed path, so the RiskStage output is
   compared field-for-field against the real `build_signal_context` across the
   deterministic corpus — covering ready continuation entries, ready retracement
   entries, and the not-ready paths that leave the levels at zero.

2. **Each ported routine in isolation.** `find_liquidity_target`,
   `cluster_liquidity_levels` and `previous_day_high_low` are compared directly
   against V1's methods (full return value), over a matrix of directions, entries
   and stops so every branch — EQH/EQL cluster, swing, PDH/PDL, RR floor, the 2R
   fallback trigger, and the mode-aware sort — is exercised under control.

Also asserts the prior stages are untouched, and that Risk owns no scoring.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from src.strategy import SMCStrategy
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
from v2.analysis.entry import EntryStage
from v2.analysis.fair_value_gap import FairValueGapStage
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage, detect_sweep
from v2.analysis.order_block import OrderBlockStage, fresh_ob
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.regime import RegimeStage
from v2.analysis.risk import (
    CONTINUATION_SL_ATR_MULT,
    FALLBACK_RR,
    RETRACEMENT_SL_ATR_MULT,
    TARGET_MIN_RR,
    RiskStage,
    cluster_liquidity_levels,
    compute_stop,
    find_liquidity_target,
    plan_risk,
    previous_day_high_low,
)
from v2.analysis.structure import (
    STRUCTURE_PROMINENCE_MULT,
    STRUCTURE_SWING_WINDOW,
    StructureStage,
    identify_swings,
    to_swings,
)
from v2.domain import EntryPlan, OrderBlock, RiskPlan, Swing

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)

RISK_FIELDS = ("sl", "tp", "tp_source", "tp_rr")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def to_v1_dicts(swings) -> list:
    return [{"index": int(s.index), "price": float(s.price)} for s in swings]


def swing(index: int, price: float, kind: str) -> Swing:
    return Swing(index=index, price=price, kind=kind)


def block(*, top: float, bottom: float, selected: bool = True) -> OrderBlock:
    return OrderBlock(
        index=25, rank_from_last=1, top=top, bottom=bottom,
        size=abs(top - bottom), fresh_status=True,
        passed_quality_filter=True, is_selected=selected,
        size_atr=abs(top - bottom), depth_from_price=1.0,
    )


def ready_continuation(entry_price=101.2, pullback_index=29) -> EntryPlan:
    return EntryPlan(
        entry_mode="CONTINUATION", entry_price=entry_price, entry_ready=True,
        entry_source="continuation_micro_pullback",
        entry_reason="continuation_micro_pullback",
        displacement_index=28, pullback_index=pullback_index,
    )


def ready_retracement(entry_price=99.0) -> EntryPlan:
    return EntryPlan(
        entry_mode="RETRACEMENT", entry_price=entry_price, entry_ready=True,
        entry_source="retracement_ob", entry_reason="no_entry",
    )


def not_ready(entry_mode="RETRACEMENT", reason="ob_quality_failed") -> EntryPlan:
    return EntryPlan(
        entry_mode=entry_mode, entry_price=0.0, entry_ready=False,
        entry_source="none", entry_reason=reason,
    )


def risk_of(plan: RiskPlan) -> tuple:
    return tuple(getattr(plan, f) for f in RISK_FIELDS)


def v1_risk_of(h1_setup: dict) -> tuple:
    return tuple(h1_setup[f] for f in RISK_FIELDS)


def v2_risk(d1: pd.DataFrame, h1: pd.DataFrame) -> RiskPlan:
    """Run the real pipeline prefix through RiskStage and return the plan."""
    snapshot = snapshot_of(d1, h1)
    context = AnalysisContext.initial(snapshot)
    context = context.with_(htf=HTFStage.analyze(snapshot))
    context = context.with_(regime=RegimeStage.analyze(snapshot, context.htf))
    context = StructureStage().run(context)
    context = OrderBlockStage().run(context)
    context = EntryStage().run(context)
    return RiskStage().run(context).risk


# --------------------------------------------------------------------------
# End-to-end V1 equivalence (the whole stage)
# --------------------------------------------------------------------------

class TestRiskMatchesV1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        strategy = SMCStrategy()
        cls.cases = []
        for name, d1, h1 in corpus():
            cls.cases.append((name, d1, h1, strategy.build_signal_context(d1, h1)))

    def test_matches_v1_h1_setup_field_for_field(self):
        # sl/tp match V1 exactly on every path. tp_source/tp_rr match V1 except
        # on a READY CONTINUATION, where V1's values are an accident (BUG-1: a
        # missing `nonlocal` forced None/None) and V2 now publishes the real
        # provenance — the one deliberate, documented divergence (metadata only;
        # REPLAY_CONTRACT.md C-1.3). There V2 must publish non-None values.
        compared = 0
        for name, d1, h1, v1 in self.cases:
            h1_setup = v1.get("h1_setup")
            if not h1_setup:
                continue
            compared += 1
            plan = v2_risk(d1, h1)
            ready_continuation_path = (
                h1_setup.get("entry_mode") == "CONTINUATION"
                and bool(h1_setup.get("entry_ready"))
            )
            with self.subTest(frame=name):
                self.assertEqual(
                    (plan.sl, plan.tp), (h1_setup["sl"], h1_setup["tp"])
                )
                if ready_continuation_path:
                    self.assertIsNone(h1_setup["tp_source"])  # V1's accident
                    self.assertIsNotNone(plan.tp_source)      # V2's fix
                    self.assertIsNotNone(plan.tp_rr)
                else:
                    self.assertEqual(
                        (plan.tp_source, plan.tp_rr),
                        (h1_setup["tp_source"], h1_setup["tp_rr"]),
                    )
        self.assertGreater(compared, 20)

    def test_corpus_covers_ready_and_not_ready_levels(self):
        ready_levels, zero_levels, fallback, targeted = False, False, False, False
        for _, d1, h1, v1 in self.cases:
            plan = v2_risk(d1, h1)
            if plan.sl > 0 and plan.tp > 0:
                ready_levels = True
                if plan.tp_source == "fallback_2r":
                    fallback = True
                else:
                    targeted = True
            if plan.sl == 0.0 and plan.tp == 0.0:
                zero_levels = True
        self.assertTrue(ready_levels, "no corpus frame produced live SL/TP")
        self.assertTrue(zero_levels, "no corpus frame left the levels at zero")
        self.assertTrue(fallback, "no corpus frame hit the 2R fallback")
        self.assertTrue(targeted, "no corpus frame selected a liquidity target")

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        self.assertEqual(
            risk_of(v2_risk(snapshot.d1, snapshot.h1)), v1_risk_of(v1["h1_setup"])
        )

    def test_is_deterministic(self):
        _, d1, h1 = corpus()[0]
        self.assertEqual(v2_risk(d1, h1), v2_risk(d1, h1))

    def test_v1_parameters_preserved(self):
        self.assertEqual(
            (CONTINUATION_SL_ATR_MULT, RETRACEMENT_SL_ATR_MULT, FALLBACK_RR,
             TARGET_MIN_RR),
            (0.30, 0.70, 2.0, 1.2),
        )


# --------------------------------------------------------------------------
# find_liquidity_target — direct V1 equivalence over a matrix
# --------------------------------------------------------------------------

class TestFindLiquidityTargetMatchesV1(unittest.TestCase):
    def setUp(self):
        self.v1 = SMCStrategy()

    def frames(self):
        for name, _d1, h1 in corpus()[:12]:
            yield name, h1

    def test_matches_v1_across_a_matrix(self):
        compared = 0
        for name, h1 in self.frames():
            raw_highs, raw_lows = identify_swings(
                h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
            )
            highs, lows = to_swings(raw_highs, "high"), to_swings(raw_lows, "low")
            atr = float(h1["atr"].iloc[-1])
            mid = float(h1["close"].mean())
            for direction in ("LONG", "SHORT"):
                for entry in (mid * 0.97, mid, mid * 1.03):
                    sl = entry * (0.99 if direction == "LONG" else 1.01)
                    for mode in ("CONTINUATION", "RETRACEMENT"):
                        for stop in (sl, None):
                            compared += 1
                            expected = self.v1.find_liquidity_target(
                                h1, direction, entry, raw_highs, raw_lows, atr,
                                sl=stop, entry_mode=mode,
                            )
                            actual = find_liquidity_target(
                                h1, direction, entry, highs, lows, atr,
                                sl=stop, entry_mode=mode,
                            )
                            with self.subTest(frame=name, direction=direction,
                                              entry=entry, mode=mode, stop=stop):
                                self.assertEqual(actual, expected)
        self.assertGreater(compared, 100)

    def test_invalid_entry(self):
        for entry in (0.0, -5.0, float("inf"), float("nan")):
            with self.subTest(entry=entry):
                self.assertEqual(
                    find_liquidity_target(
                        build_frame(n=120), "LONG", entry, [], [], 1.0
                    ),
                    {"tp": None, "source": None, "rr": None, "reason": "invalid_entry"},
                )

    def test_invalid_direction(self):
        self.assertEqual(
            find_liquidity_target(build_frame(n=120), "SIDEWAYS", 100.0, [], [], 1.0),
            {"tp": None, "source": None, "rr": None, "reason": "invalid_direction"},
        )

    def test_no_candidates(self):
        # No swings above entry and no PDH -> nothing to target.
        result = find_liquidity_target(build_frame(n=120), "LONG", 500.0, [], [], 1.0)
        self.assertEqual(result["reason"], "no_candidates")
        self.assertIsNone(result["tp"])

    def test_direction_is_normalized(self):
        highs = (swing(10, 105.0, "high"),)
        self.assertEqual(
            find_liquidity_target(build_frame(n=120), "long", 100.0, highs, [], 1.0),
            find_liquidity_target(build_frame(n=120), "LONG", 100.0, highs, [], 1.0),
        )

    def test_unknown_entry_mode_falls_back_to_retracement(self):
        highs = (swing(10, 105.0, "high"), swing(12, 110.0, "high"))
        self.assertEqual(
            find_liquidity_target(build_frame(n=120), "LONG", 100.0, highs, [], 1.0,
                                  entry_mode="WHATEVER"),
            find_liquidity_target(build_frame(n=120), "LONG", 100.0, highs, [], 1.0,
                                  entry_mode="RETRACEMENT"),
        )


# --------------------------------------------------------------------------
# cluster_liquidity_levels — direct V1 equivalence
# --------------------------------------------------------------------------

class TestClusterLiquidityLevels(unittest.TestCase):
    def setUp(self):
        self.v1 = SMCStrategy()

    def check(self, raw, tolerance):
        swings = to_swings(raw, "high")
        self.assertEqual(
            cluster_liquidity_levels(swings, tolerance),
            self.v1._cluster_liquidity_levels(raw, tolerance),
        )

    def test_empty(self):
        self.assertEqual(cluster_liquidity_levels([], 1.0), [])

    def test_single_level(self):
        self.check([{"index": 3, "price": 100.0}], 1.0)

    def test_merges_within_tolerance(self):
        raw = [{"index": 3, "price": 100.0}, {"index": 5, "price": 100.5}]
        self.check(raw, 1.0)  # merged: count 2

    def test_splits_outside_tolerance(self):
        raw = [{"index": 3, "price": 100.0}, {"index": 5, "price": 105.0}]
        self.check(raw, 1.0)  # two clusters

    def test_boundary_tolerance_is_inclusive(self):
        raw = [{"index": 3, "price": 100.0}, {"index": 5, "price": 101.0}]
        self.check(raw, 1.0)  # exactly 1.0 apart -> merged

    def test_chained_merge_uses_running_last(self):
        raw = [
            {"index": 1, "price": 100.0},
            {"index": 2, "price": 100.9},
            {"index": 3, "price": 101.8},
        ]
        self.check(raw, 1.0)  # each within 1.0 of the previous

    def test_sorts_before_clustering(self):
        raw = [
            {"index": 3, "price": 105.0},
            {"index": 5, "price": 100.0},
            {"index": 7, "price": 100.4},
        ]
        self.check(raw, 1.0)


# --------------------------------------------------------------------------
# previous_day_high_low — direct V1 equivalence
# --------------------------------------------------------------------------

class TestPreviousDayHighLow(unittest.TestCase):
    def setUp(self):
        self.v1 = SMCStrategy()

    def multiday_frame(self, hours=72):
        ts = pd.date_range("2026-03-01", periods=hours, freq="h")
        return pd.DataFrame({
            "timestamp": ts,
            "high": [100.0 + i * 0.1 for i in range(hours)],
            "low": [90.0 - i * 0.05 for i in range(hours)],
            "close": [95.0 for _ in range(hours)],
        })

    def test_matches_v1_on_a_multiday_frame(self):
        df = self.multiday_frame()
        self.assertEqual(previous_day_high_low(df), self.v1._get_previous_day_high_low(df))

    def test_none_below_minimum_rows(self):
        self.assertIsNone(previous_day_high_low(self.multiday_frame(hours=29)))

    def test_none_for_none_or_empty(self):
        self.assertIsNone(previous_day_high_low(None))
        self.assertIsNone(previous_day_high_low(pd.DataFrame()))

    def test_none_for_a_single_day(self):
        ts = pd.date_range("2026-03-01 00:00", periods=30, freq="20min")  # < 1 day
        df = pd.DataFrame({
            "timestamp": ts, "high": [100.0] * 30, "low": [90.0] * 30,
            "close": [95.0] * 30,
        })
        self.assertIsNone(previous_day_high_low(df))


# --------------------------------------------------------------------------
# Stop-loss derivation
# --------------------------------------------------------------------------

class TestStopLoss(unittest.TestCase):
    def test_continuation_long_sl(self):
        df = set_bar(build_frame(), 29, low=100.8)
        sl = compute_stop(ready_continuation(), "LONG", df, 1.0, None)
        self.assertAlmostEqual(sl, 100.8 - 0.30)

    def test_continuation_short_sl(self):
        df = set_bar(build_frame(), 29, high=99.2)
        sl = compute_stop(ready_continuation(), "SHORT", df, 1.0, None)
        self.assertAlmostEqual(sl, 99.2 + 0.30)

    def test_continuation_reads_the_pullback_bar(self):
        df = set_bar(build_frame(), 27, low=95.0)
        sl = compute_stop(
            ready_continuation(pullback_index=27), "LONG", df, 2.0, None
        )
        self.assertAlmostEqual(sl, 95.0 - 2.0 * 0.30)

    def test_retracement_long_sl(self):
        blocks = (block(top=99.0, bottom=96.0),)
        sl = compute_stop(ready_retracement(), "LONG", build_frame(), 1.0, blocks)
        self.assertAlmostEqual(sl, 96.0 - 0.70)

    def test_retracement_short_sl(self):
        blocks = (block(top=104.0, bottom=101.0),)
        sl = compute_stop(
            ready_retracement(entry_price=101.0), "SHORT", build_frame(), 1.0, blocks
        )
        self.assertAlmostEqual(sl, 104.0 + 0.70)

    def test_retracement_uses_the_selected_block(self):
        blocks = (
            block(top=99.0, bottom=96.0, selected=False),
            block(top=98.0, bottom=95.0, selected=True),
        )
        sl = compute_stop(ready_retracement(), "LONG", build_frame(), 1.0, blocks)
        self.assertAlmostEqual(sl, 95.0 - 0.70)  # the selected block's bottom


# --------------------------------------------------------------------------
# 2R fallback
# --------------------------------------------------------------------------

class TestTwoRFallback(unittest.TestCase):
    def test_long_fallback_when_no_target(self):
        # No swings, no PDH -> no_candidates -> tp None -> 2R fallback.
        df = build_frame(n=120)
        plan = plan_risk(
            ready_retracement(entry_price=100.0), "LONG", df, 1.0,
            (), (), (block(top=100.0, bottom=97.0),),
        )
        expected_sl = 97.0 - 0.70
        self.assertAlmostEqual(plan.sl, expected_sl)
        self.assertAlmostEqual(plan.tp, 100.0 + (100.0 - expected_sl) * 2.0)
        self.assertEqual(plan.tp_source, "fallback_2r")
        self.assertEqual(plan.tp_rr, 2.0)

    def test_short_fallback_when_no_target(self):
        df = build_frame(n=120)
        plan = plan_risk(
            ready_retracement(entry_price=100.0), "SHORT", df, 1.0,
            (), (), (block(top=103.0, bottom=100.0),),
        )
        expected_sl = 103.0 + 0.70
        self.assertAlmostEqual(plan.tp, 100.0 - (expected_sl - 100.0) * 2.0)
        self.assertEqual(plan.tp_source, "fallback_2r")
        self.assertEqual(plan.tp_rr, 2.0)

    def test_target_found_skips_the_fallback_retracement(self):
        # RETRACEMENT keeps tp_source, so a real target is visible.
        df = build_frame(n=120)
        highs = tuple(swing(i, 130.0, "high") for i in (40, 60, 80))
        plan = plan_risk(
            ready_retracement(entry_price=100.0), "LONG", df, 1.0,
            highs, (), (block(top=100.0, bottom=97.0),),
        )
        self.assertNotIn(plan.tp_source, {"fallback_2r", None})
        self.assertTrue(plan.tp > 0)

    def test_continuation_publishes_tp_source(self):
        # BUG-1 fix (re-based deliberately; REPLAY_CONTRACT.md C-1.3): V1
        # dropped continuation tp_source/tp_rr via a missing `nonlocal` — a
        # scoping accident. Post-migration the real provenance is published.
        # sl/tp themselves are still asserted V1-identical elsewhere.
        df = build_frame(n=120)
        highs = tuple(swing(i, 130.0, "high") for i in (40, 60, 80))
        targeted = plan_risk(
            ready_continuation(entry_price=100.0), "LONG", df, 1.0, highs, (), None,
        )
        self.assertTrue(targeted.tp > 0)
        self.assertIsNotNone(targeted.tp_source)
        self.assertNotEqual(targeted.tp_source, "fallback_2r")
        self.assertIsNotNone(targeted.tp_rr)

        # ...and the 2R fallback is attributed too.
        fell_back = plan_risk(
            ready_continuation(entry_price=100.0), "LONG", df, 1.0, (), (),
            (block(top=100.0, bottom=97.0),),
        )
        self.assertTrue(fell_back.tp > 0)
        self.assertEqual(fell_back.tp_source, "fallback_2r")
        self.assertEqual(fell_back.tp_rr, 2.0)


# --------------------------------------------------------------------------
# Target selection sources
# --------------------------------------------------------------------------

class TestTargetSelection(unittest.TestCase):
    def frame(self):
        return build_frame(n=120)

    def test_eqh_cluster_preferred_for_long_retracement(self):
        # Two near-equal highs cluster into an EQH target (score 4.0),
        # outranking a lone swing for RETRACEMENT (strength-first).
        highs = (
            swing(40, 110.0, "high"),
            swing(60, 110.2, "high"),
            swing(80, 108.0, "high"),
        )
        result = find_liquidity_target(
            self.frame(), "LONG", 100.0, highs, (), 1.0,
            sl=99.0, entry_mode="RETRACEMENT",
        )
        self.assertEqual(result["source"], "EQH_cluster")

    def test_eql_cluster_for_short(self):
        lows = (swing(40, 90.0, "low"), swing(60, 89.8, "low"))
        result = find_liquidity_target(
            self.frame(), "SHORT", 100.0, (), lows, 1.0,
            sl=101.0, entry_mode="RETRACEMENT",
        )
        self.assertEqual(result["source"], "EQL_cluster")

    def test_pdh_is_a_candidate_source(self):
        # build_frame spans >= 2 days, so a PDH exists and can be selected.
        df = self.frame()
        v1 = SMCStrategy()
        for entry, sl in ((100.0, 99.0), (100.5, 100.0)):
            with self.subTest(entry=entry):
                expected = v1.find_liquidity_target(
                    df, "LONG", entry, [], [], 1.0, sl=sl,
                )
                actual = find_liquidity_target(df, "LONG", entry, (), (), 1.0, sl=sl)
                self.assertEqual(actual, expected)

    def test_continuation_prefers_nearest(self):
        # Near and far swings both pass RR; continuation takes the nearest.
        highs = (swing(40, 103.0, "high"), swing(60, 130.0, "high"))
        result = find_liquidity_target(
            self.frame(), "LONG", 100.0, highs, (), 1.0,
            sl=99.0, entry_mode="CONTINUATION",
        )
        self.assertAlmostEqual(result["tp"], 103.0 - 1.0 * 0.10)

    def test_rr_below_floor_returns_no_tp_but_a_source(self):
        # Target just above entry with a wide stop -> RR under 1.2. A lone swing
        # also seeds a count-1 EQH_cluster, which outranks it (V1 behavior).
        highs = (swing(40, 100.5, "high"),)
        result = find_liquidity_target(
            self.frame(), "LONG", 100.0, highs, (), 1.0,
            sl=90.0, entry_mode="RETRACEMENT",
        )
        expected = SMCStrategy().find_liquidity_target(
            self.frame(), "LONG", 100.0, to_v1_dicts(highs), [], 1.0,
            sl=90.0, entry_mode="RETRACEMENT",
        )
        self.assertEqual(result, expected)
        self.assertIsNone(result["tp"])
        self.assertEqual(result["reason"], "rr_below_floor")
        self.assertIsNotNone(result["source"])


# --------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------

class TestDegenerateInputs(unittest.TestCase):
    def test_not_ready_entry_yields_zero_levels(self):
        plan = plan_risk(
            not_ready(), "LONG", build_frame(n=120), 1.0, (), (), None
        )
        self.assertEqual(risk_of(plan), (0.0, 0.0, None, None))

    def test_not_ready_ignores_frame_and_blocks(self):
        # A not-ready entry never touches the frame or the order blocks.
        plan = plan_risk(not_ready(), "LONG", None, 1.0, (), (), None)
        self.assertEqual(risk_of(plan), (0.0, 0.0, None, None))

    def test_watch_only_not_ready(self):
        plan = plan_risk(
            not_ready(entry_mode="WATCH_ONLY", reason="watch_only_regime"),
            "LONG", build_frame(n=120), 1.0, (), (), None,
        )
        self.assertEqual(risk_of(plan), (0.0, 0.0, None, None))

    def test_zero_atr_target_uses_bare_levels(self):
        # atr 0 -> tolerance from price, tp_level == c["level"] (no 0.10 shift).
        df = build_frame(n=120)
        highs = (swing(40, 130.0, "high"),)
        v1 = SMCStrategy().find_liquidity_target(
            df, "LONG", 100.0, to_v1_dicts(highs), [], 0.0, sl=99.0,
        )
        v2 = find_liquidity_target(df, "LONG", 100.0, highs, (), 0.0, sl=99.0)
        self.assertEqual(v2, v1)


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestRiskStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        context = AnalysisContext.initial(self.snapshot)
        context = context.with_(htf=HTFStage.analyze(self.snapshot))
        context = context.with_(
            regime=RegimeStage.analyze(self.snapshot, context.htf)
        )
        context = StructureStage().run(context)
        context = OrderBlockStage().run(context)
        self.context = EntryStage().run(context)

    def test_populates_risk(self):
        self.assertIsInstance(RiskStage().run(self.context).risk, RiskPlan)

    def test_matches_v1(self):
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        self.assertEqual(
            risk_of(RiskStage().run(self.context).risk), v1_risk_of(v1["h1_setup"])
        )

    def test_leaves_scoring_rr_none(self):
        self.assertIsNone(RiskStage().run(self.context).risk.rr)

    def test_populates_only_risk(self):
        result = RiskStage().run(self.context)
        for field in ("candidate", "signal"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_no_risk_without_entry(self):
        no_entry = AnalysisContext.initial(self.snapshot).with_(
            htf=self.context.htf, atr_last=1.0
        )
        self.assertIsNone(RiskStage().run(no_entry).risk)

    def test_no_risk_without_atr(self):
        bare = AnalysisContext.initial(self.snapshot)
        self.assertIsNone(RiskStage().run(bare).risk)

    def test_no_risk_when_h1_unusable(self):
        thin = AnalysisContext.initial(snapshot_with(build_frame(n=5))).with_(
            htf=htf_context(), entry=not_ready(), atr_last=1.0,
        )
        self.assertIsNone(RiskStage().run(thin).risk)

    def test_does_not_mutate_input_context(self):
        RiskStage().run(self.context)
        self.assertIsNone(self.context.risk)

    def test_is_deterministic(self):
        self.assertEqual(RiskStage().run(self.context), RiskStage().run(self.context))

    def test_stage_name(self):
        self.assertEqual(RiskStage().name, "risk")


# --------------------------------------------------------------------------
# Extraction neutrality + module ownership
# --------------------------------------------------------------------------

class TestPriorStagesUnchanged(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.v1 = SMCStrategy().build_signal_context(
            self.snapshot.d1, self.snapshot.h1
        )
        context = AnalysisContext.initial(self.snapshot)
        context = context.with_(htf=HTFStage.analyze(self.snapshot))
        self.context = context.with_(
            regime=RegimeStage.analyze(self.snapshot, context.htf)
        )

    def test_htf_unchanged(self):
        htf = self.context.htf
        for field in ("weekly_bias", "d1_bias", "h4_bias", "location",
                      "pdh", "pdl", "daily_eq", "current_price"):
            with self.subTest(field=field):
                self.assertEqual(getattr(htf, field), self.v1["htf_map"][field])
        self.assertEqual(htf.direction, self.v1["debug"]["htf"]["dir"])

    def test_regime_unchanged(self):
        self.assertEqual(self.context.regime.label, self.v1["debug"]["htf"]["regime"])
        self.assertEqual(
            self.context.regime.entry_mode, self.v1["h1_setup"]["entry_mode"]
        )

    def test_structure_unchanged(self):
        result = StructureStage().run(self.context)
        self.assertEqual(result.atr_last, self.v1["h1_setup"]["atr_last"])
        self.assertEqual(
            result.displacement_valid, self.v1["h1_setup"]["displacement_valid"]
        )
        self.assertEqual(result.volume_spike, self.v1["h1_setup"]["volume_spike"])

    def test_liquidity_unchanged(self):
        h1 = safe_frame(self.snapshot.h1, H1_MIN_ROWS)
        raw_highs, raw_lows = identify_swings(
            h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
        )
        sweep = detect_sweep(
            self.context.htf.direction, h1,
            to_swings(raw_highs, "high"), to_swings(raw_lows, "low"),
        )
        self.assertEqual(sweep.detected, self.v1["h1_setup"]["sweep_valid"])

    def test_order_block_unchanged(self):
        result = OrderBlockStage().run(StructureStage().run(self.context))
        self.assertEqual(
            fresh_ob(result.order_blocks), self.v1["h1_setup"]["fresh_ob"]
        )

    def test_fair_value_gap_unchanged(self):
        result = FairValueGapStage().run(StructureStage().run(self.context))
        self.assertEqual(result.fvg.valid, self.v1["h1_setup"]["fvg_valid"])

    def test_entry_unchanged(self):
        context = OrderBlockStage().run(StructureStage().run(self.context))
        result = EntryStage().run(context)
        for field in ("entry_ready", "entry_source", "entry_reason", "entry_price"):
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(result.entry, field), self.v1["h1_setup"][field]
                )


class TestModuleOwnership(unittest.TestCase):
    def test_risk_owns_the_target_machinery(self):
        import v2.analysis.risk as risk_module

        for name in ("find_liquidity_target", "cluster_liquidity_levels",
                     "previous_day_high_low"):
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(risk_module, name))

    def test_risk_reuses_the_order_block_selection_helper(self):
        import v2.analysis.order_block as ob_module
        import v2.analysis.risk as risk_module

        self.assertIs(risk_module.selected_block, ob_module.selected_block)

    def test_target_machinery_has_a_single_owner(self):
        import v2.analysis.entry as entry_module
        import v2.analysis.fair_value_gap as fvg_module
        import v2.analysis.htf as htf_module
        import v2.analysis.liquidity as liquidity_module
        import v2.analysis.order_block as ob_module
        import v2.analysis.regime as regime_module
        import v2.analysis.structure as structure_module

        for module in (htf_module, regime_module, structure_module,
                       liquidity_module, ob_module, fvg_module, entry_module):
            for name in ("find_liquidity_target", "cluster_liquidity_levels",
                         "previous_day_high_low", "plan_risk", "compute_stop"):
                with self.subTest(module=module.__name__, symbol=name):
                    self.assertFalse(hasattr(module, name))

    def test_risk_owns_no_scoring_or_signal(self):
        import v2.analysis.risk as risk_module

        for name in ("score_signal", "build_signal", "decision", "alert_tier"):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(risk_module, name))

    def test_risk_plan_carries_no_score_or_decision(self):
        plan = plan_risk(not_ready(), "LONG", build_frame(n=120), 1.0, (), (), None)
        for field in ("score", "decision", "notes", "reason"):
            with self.subTest(field=field):
                self.assertFalse(hasattr(plan, field))


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def stages(self):
        return [HTFStage(), RegimeStage(), StructureStage(), LiquidityStage(),
                OrderBlockStage(), FairValueGapStage(), EntryStage(), RiskStage()]

    def test_first_eight_stages_run_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot)
        )
        self.assertIsNotNone(result.htf)
        self.assertIsNotNone(result.entry)
        self.assertIsInstance(result.risk, RiskPlan)
        self.assertIsNone(result.candidate)
        self.assertIsNone(result.signal)

    def test_risk_matches_v1_through_the_pipeline(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot)
        )
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        self.assertEqual(risk_of(result.risk), v1_risk_of(v1["h1_setup"]))

    def test_risk_skipped_when_htf_bails(self):
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
        )
        self.assertIsNone(result.htf)
        self.assertIsNone(result.risk)


if __name__ == "__main__":
    unittest.main()
