"""Phase 3.4: Liquidity stage — behavioral equivalence with V1.

Sweep detection is compared field-by-field against V1's `SMCStrategy._detect_sweep`
across a corpus of frames in both directions, and again with hand-built swing
references so every branch and threshold is exercised under full control.

Also asserts the prior stages are untouched: HTF, Regime and Structure still
produce V1's exact values.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from src.strategy import SMCStrategy
from tests.regression.snapshot import InputSnapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import (
    SWEEP_ATR_DEPTH_MULT,
    SWEEP_LOOKBACK,
    SWEEP_MAX_SWING_AGE,
    SWEEP_RECLAIM_RATIO,
    LiquidityStage,
    detect_sweep,
)
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.regime import RegimeStage
from v2.analysis.structure import (
    STRUCTURE_PROMINENCE_MULT,
    STRUCTURE_SWING_WINDOW,
    StructureStage,
    identify_swings,
    to_swings,
)
from v2.domain import HTFContext, LiquiditySweep, MarketSnapshot, Swing

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def build_frame(
    *,
    n: int = 20,
    close: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    atr: float | None = 1.0,
) -> pd.DataFrame:
    """A flat V1-shaped frame; individual bars are then overridden per test."""
    ts = pd.date_range("2026-01-01", periods=n, freq="h")
    data = {
        "timestamp": ts,
        "open": [close] * n,
        "high": [high] * n,
        "low": [low] * n,
        "close": [close] * n,
        "v": [1000.0] * n,
    }
    if atr is not None:
        data["atr"] = [atr] * n
    return pd.DataFrame(data)


def set_bar(df: pd.DataFrame, i: int, *, open_=None, high=None, low=None, close=None):
    for col, value in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if value is not None:
            df.loc[df.index[i], col] = value
    return df


def trend_frame(n: int, *, base: float, step: float, wobble: float, spread: float,
                atr: float = 1.0, with_atr: bool = True) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="h")
    closes = [base + step * i + (wobble if i % 2 else -wobble) for i in range(n)]
    data = {
        "timestamp": ts,
        "open": [c - step * 0.3 for c in closes],
        "high": [c + spread for c in closes],
        "low": [c - spread for c in closes],
        "close": closes,
        "v": [1000.0 + i for i in range(n)],
    }
    if with_atr:
        data["atr"] = [atr] * n
    return pd.DataFrame(data)


def to_v1_dicts(swings) -> list:
    """V1 takes raw {"index","price"} dicts; V2 takes domain Swings."""
    return [{"index": s.index, "price": s.price} for s in swings]


def as_tuple(sweep: LiquiditySweep):
    return (
        sweep.detected, sweep.index, sweep.ref_index,
        sweep.ref_price, sweep.sweep_depth, sweep.reclaim_strength,
    )


def v1_as_tuple(detected: bool, meta) -> tuple:
    if not detected or meta is None:
        return (detected, None, None, None, None, None)
    return (
        detected, meta["index"], meta["ref_index"],
        meta["ref_price"], meta["sweep_depth"], meta["reclaim_strength"],
    )


def htf_context(direction: str | None = "LONG") -> HTFContext:
    return HTFContext(
        pdh=110.0, pdl=90.0, daily_eq=100.0,
        weekly_bias="BULLISH", d1_bias="BULLISH", h4_bias="CHOPPY",
        location="discount", current_price=100.0, momentum_weakening=False,
        bull=8.5, bear=0.0, direction_source="weighted_bull",
        anchor_override=False, veto_active=False, direction=direction,
    )


def snapshot_with(h1: pd.DataFrame) -> MarketSnapshot:
    last = pd.to_datetime(h1["timestamp"].iloc[-1]) if len(h1) else pd.Timestamp("2026-01-01")
    return MarketSnapshot(
        symbol="LIQ/USDT", d1=h1, h1=h1,
        h1_last_ts=float(last.timestamp()), h1_last_ts_dt=last, live_price=100.0,
    )


CORPUS = {
    "flat": trend_frame(40, base=100.0, step=0.0, wobble=0.0, spread=1.0),
    "uptrend": trend_frame(40, base=100.0, step=1.5, wobble=0.0, spread=2.0),
    "downtrend": trend_frame(40, base=300.0, step=-1.5, wobble=0.0, spread=2.0),
    "choppy": trend_frame(40, base=100.0, step=0.0, wobble=3.0, spread=2.0),
    "choppy_up": trend_frame(40, base=100.0, step=0.8, wobble=2.0, spread=3.0),
    "wide": trend_frame(40, base=100.0, step=0.0, wobble=5.0, spread=8.0, atr=0.5),
    "tight": trend_frame(40, base=100.0, step=0.0, wobble=0.2, spread=0.1, atr=3.0),
    "no_atr": trend_frame(40, base=100.0, step=0.0, wobble=3.0, spread=2.0, with_atr=False),
    "short": trend_frame(6, base=100.0, step=1.0, wobble=1.0, spread=1.0),
    "too_short": trend_frame(4, base=100.0, step=1.0, wobble=1.0, spread=1.0),
}


# --------------------------------------------------------------------------
# V1 equivalence
# --------------------------------------------------------------------------

class TestSweepMatchesV1(unittest.TestCase):
    def test_matches_v1_across_corpus_both_directions(self):
        v1 = SMCStrategy()
        for name, df in CORPUS.items():
            raw_highs, raw_lows = identify_swings(
                df, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
            )
            highs, lows = to_swings(raw_highs, "high"), to_swings(raw_lows, "low")
            for direction in ("LONG", "SHORT"):
                with self.subTest(frame=name, direction=direction):
                    expected = v1_as_tuple(
                        *v1._detect_sweep(direction, df, raw_highs, raw_lows)
                    )
                    actual = as_tuple(detect_sweep(direction, df, highs, lows))
                    self.assertEqual(actual, expected)

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        htf = HTFStage.analyze(snapshot)
        raw_highs, raw_lows = identify_swings(
            h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT
        )
        expected = v1_as_tuple(
            *SMCStrategy()._detect_sweep(htf.direction, h1, raw_highs, raw_lows)
        )
        actual = as_tuple(
            detect_sweep(
                htf.direction, h1,
                to_swings(raw_highs, "high"), to_swings(raw_lows, "low"),
            )
        )
        self.assertEqual(actual, expected)

    def test_sweep_valid_matches_v1_h1_setup(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        htf = HTFStage.analyze(snapshot)
        raw_highs, raw_lows = identify_swings(h1, 2, 0.3)
        sweep = detect_sweep(
            htf.direction, h1, to_swings(raw_highs, "high"), to_swings(raw_lows, "low")
        )
        self.assertEqual(sweep.detected, v1["h1_setup"]["sweep_valid"])

    def test_is_deterministic(self):
        df = CORPUS["choppy"]
        raw_highs, raw_lows = identify_swings(df, 2, 0.3)
        highs, lows = to_swings(raw_highs, "high"), to_swings(raw_lows, "low")
        self.assertEqual(
            detect_sweep("LONG", df, highs, lows),
            detect_sweep("LONG", df, highs, lows),
        )


# --------------------------------------------------------------------------
# Buy-side (LONG) sweep — controlled references
# --------------------------------------------------------------------------

class TestBuySideSweep(unittest.TestCase):
    def setUp(self):
        self.ref = [Swing(index=5, price=95.0, kind="low")]

    def long_frame(self):
        df = build_frame()
        # Final bar pierces 95.0 and closes back well above it.
        return set_bar(df, 19, open_=95.5, high=99.5, low=94.0, close=99.0)

    def test_detected_with_metadata(self):
        sweep = detect_sweep("LONG", self.long_frame(), [], self.ref)
        self.assertTrue(sweep.detected)
        self.assertEqual(sweep.index, 19)
        self.assertEqual(sweep.ref_index, 5)
        self.assertEqual(sweep.ref_price, 95.0)
        self.assertAlmostEqual(sweep.sweep_depth, 1.0)
        self.assertAlmostEqual(sweep.reclaim_strength, 5.0 / 5.5)

    def test_matches_v1(self):
        df = self.long_frame()
        expected = v1_as_tuple(
            *SMCStrategy()._detect_sweep("LONG", df, [], to_v1_dicts(self.ref))
        )
        self.assertEqual(as_tuple(detect_sweep("LONG", df, [], self.ref)), expected)

    def test_no_pierce_is_no_sweep(self):
        df = set_bar(build_frame(), 19, low=95.5, high=99.5, close=99.0)
        self.assertFalse(detect_sweep("LONG", df, [], self.ref).detected)

    def test_close_below_reference_is_no_reclaim(self):
        df = set_bar(build_frame(), 19, low=94.0, high=96.0, close=94.5)
        self.assertFalse(detect_sweep("LONG", df, [], self.ref).detected)

    def test_weak_body_reclaim_rejected(self):
        # Pierces and reclaims, but close sits low in the range (ratio < 0.40).
        df = set_bar(build_frame(), 19, low=94.0, high=120.0, close=95.5)
        sweep = detect_sweep("LONG", df, [], self.ref)
        self.assertFalse(sweep.detected)

    def test_depth_below_atr_threshold_rejected(self):
        # Depth 0.05 < atr(1.0) * 0.12.
        df = set_bar(build_frame(), 19, low=94.95, high=99.5, close=99.0)
        self.assertFalse(detect_sweep("LONG", df, [], self.ref).detected)

    def test_reclaim_buffer_uses_atr(self):
        # close must exceed ref + atr*0.03 = 95.03; 95.02 fails, 95.6 passes.
        near = set_bar(build_frame(), 19, low=94.0, high=96.0, close=95.02)
        self.assertFalse(detect_sweep("LONG", near, [], self.ref).detected)
        clear = set_bar(build_frame(), 19, low=94.0, high=96.0, close=95.6)
        self.assertTrue(detect_sweep("LONG", clear, [], self.ref).detected)

    def test_scans_backwards_and_returns_first_match(self):
        df = build_frame()
        set_bar(df, 17, open_=95.5, high=99.5, low=94.0, close=99.0)
        set_bar(df, 19, open_=95.5, high=99.5, low=94.0, close=99.0)
        # Scanning from the last bar downwards, bar 19 is found first.
        self.assertEqual(detect_sweep("LONG", df, [], self.ref).index, 19)

    def test_outside_lookback_is_ignored(self):
        # Sweep bar at index 3 is older than the 12-bar lookback from 19.
        df = set_bar(build_frame(), 3, open_=95.5, high=99.5, low=94.0, close=99.0)
        self.assertFalse(detect_sweep("LONG", df, [], self.ref).detected)


# --------------------------------------------------------------------------
# Sell-side (SHORT) sweep — controlled references
# --------------------------------------------------------------------------

class TestSellSideSweep(unittest.TestCase):
    def setUp(self):
        self.ref = [Swing(index=5, price=105.0, kind="high")]

    def short_frame(self):
        df = build_frame()
        return set_bar(df, 19, open_=104.5, high=106.0, low=100.5, close=101.0)

    def test_detected_with_metadata(self):
        sweep = detect_sweep("SHORT", self.short_frame(), self.ref, [])
        self.assertTrue(sweep.detected)
        self.assertEqual(sweep.index, 19)
        self.assertEqual(sweep.ref_index, 5)
        self.assertEqual(sweep.ref_price, 105.0)
        self.assertAlmostEqual(sweep.sweep_depth, 1.0)
        self.assertAlmostEqual(sweep.reclaim_strength, 5.0 / 5.5)

    def test_matches_v1(self):
        df = self.short_frame()
        expected = v1_as_tuple(
            *SMCStrategy()._detect_sweep("SHORT", df, to_v1_dicts(self.ref), [])
        )
        self.assertEqual(as_tuple(detect_sweep("SHORT", df, self.ref, [])), expected)

    def test_no_pierce_is_no_sweep(self):
        df = set_bar(build_frame(), 19, high=104.5, low=100.5, close=101.0)
        self.assertFalse(detect_sweep("SHORT", df, self.ref, []).detected)

    def test_close_above_reference_is_no_reclaim(self):
        df = set_bar(build_frame(), 19, high=106.0, low=105.2, close=105.5)
        self.assertFalse(detect_sweep("SHORT", df, self.ref, []).detected)

    def test_weak_body_reclaim_rejected(self):
        df = set_bar(build_frame(), 19, high=106.0, low=80.0, close=104.5)
        self.assertFalse(detect_sweep("SHORT", df, self.ref, []).detected)

    def test_depth_below_atr_threshold_rejected(self):
        df = set_bar(build_frame(), 19, high=105.05, low=100.5, close=101.0)
        self.assertFalse(detect_sweep("SHORT", df, self.ref, []).detected)

    def test_uses_wrong_swing_list_for_direction(self):
        # SHORT reads swing_highs only; passing the reference as a low finds nothing.
        df = self.short_frame()
        self.assertFalse(detect_sweep("SHORT", df, [], self.ref).detected)


# --------------------------------------------------------------------------
# Reference selection + boundaries
# --------------------------------------------------------------------------

class TestReferenceSelection(unittest.TestCase):
    def frame_with_sweep(self):
        return set_bar(build_frame(), 19, open_=95.5, high=99.5, low=94.0, close=99.0)

    def test_freshest_qualifying_swing_wins(self):
        refs = [
            Swing(index=3, price=90.0, kind="low"),
            Swing(index=5, price=95.0, kind="low"),
        ]
        sweep = detect_sweep("LONG", self.frame_with_sweep(), [], refs)
        self.assertEqual(sweep.ref_index, 5)  # last qualifying entry

    def test_swing_on_final_bar_is_excluded(self):
        refs = [Swing(index=19, price=95.0, kind="low")]
        self.assertFalse(detect_sweep("LONG", self.frame_with_sweep(), [], refs).detected)

    def test_swing_older_than_max_age_excluded(self):
        df = build_frame(n=60)
        set_bar(df, 59, open_=95.5, high=99.5, low=94.0, close=99.0)
        stale = [Swing(index=5, price=95.0, kind="low")]  # age 54 > 36
        self.assertFalse(detect_sweep("LONG", df, [], stale).detected)
        fresh = [Swing(index=30, price=95.0, kind="low")]  # age 29 <= 36
        self.assertTrue(detect_sweep("LONG", df, [], fresh).detected)

    def test_no_swings_means_no_sweep(self):
        self.assertFalse(detect_sweep("LONG", self.frame_with_sweep(), [], []).detected)
        self.assertFalse(detect_sweep("SHORT", self.frame_with_sweep(), [], []).detected)

    def test_v1_default_parameters_preserved(self):
        self.assertEqual(
            (SWEEP_LOOKBACK, SWEEP_ATR_DEPTH_MULT, SWEEP_RECLAIM_RATIO, SWEEP_MAX_SWING_AGE),
            (12, 0.12, 0.40, 36),
        )


class TestDegenerateInputs(unittest.TestCase):
    def setUp(self):
        self.ref = [Swing(index=1, price=95.0, kind="low")]

    def test_none_frame(self):
        self.assertFalse(detect_sweep("LONG", None, [], self.ref).detected)

    def test_empty_frame(self):
        self.assertFalse(detect_sweep("LONG", build_frame(n=0), [], self.ref).detected)

    def test_frame_below_minimum_rows(self):
        df = set_bar(build_frame(n=4), 3, low=94.0, high=99.5, close=99.0)
        self.assertFalse(detect_sweep("LONG", df, [], self.ref).detected)

    def test_unknown_direction_falls_through(self):
        # V1's branches only handle LONG/SHORT; anything else returns no sweep.
        df = set_bar(build_frame(), 19, open_=95.5, high=99.5, low=94.0, close=99.0)
        for direction in (None, "", "NONE", "long"):
            with self.subTest(direction=direction):
                self.assertFalse(detect_sweep(direction, df, [], self.ref).detected)

    def test_missing_atr_column_treats_atr_as_zero(self):
        # atr 0 -> no reclaim buffer and depth_valid whenever depth >= 0.
        df = build_frame(atr=None)
        set_bar(df, 19, open_=95.5, high=99.5, low=94.0, close=99.0)
        sweep = detect_sweep("LONG", df, [], [Swing(index=5, price=95.0, kind="low")])
        self.assertTrue(sweep.detected)
        expected = v1_as_tuple(
            *SMCStrategy()._detect_sweep(
                "LONG", df, [], [{"index": 5, "price": 95.0}]
            )
        )
        self.assertEqual(as_tuple(sweep), expected)

    def test_nan_atr_treated_as_zero(self):
        df = build_frame()
        df["atr"] = float("nan")
        set_bar(df, 19, open_=95.5, high=99.5, low=94.0, close=99.0)
        ref = [Swing(index=5, price=95.0, kind="low")]
        expected = v1_as_tuple(
            *SMCStrategy()._detect_sweep("LONG", df, [], to_v1_dicts(ref))
        )
        self.assertEqual(as_tuple(detect_sweep("LONG", df, [], ref)), expected)

    def test_zero_range_candle_uses_epsilon_guard(self):
        # high == low would divide by zero without V1's max(..., 1e-9).
        df = build_frame()
        set_bar(df, 19, open_=94.0, high=94.0, low=94.0, close=94.0)
        sweep = detect_sweep("LONG", df, [], [Swing(index=5, price=95.0, kind="low")])
        self.assertFalse(sweep.detected)  # no reclaim; but must not raise


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestLiquidityStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        base = AnalysisContext.initial(self.snapshot)
        self.context = StructureStage().run(
            base.with_(htf=HTFStage.analyze(self.snapshot))
        )

    def test_populates_sweep(self):
        result = LiquidityStage().run(self.context)
        self.assertIsInstance(result.sweep, LiquiditySweep)

    def test_matches_v1_sweep_valid(self):
        result = LiquidityStage().run(self.context)
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        self.assertEqual(result.sweep.detected, v1["h1_setup"]["sweep_valid"])

    def test_populates_only_sweep(self):
        result = LiquidityStage().run(self.context)
        for field in ("order_blocks", "fvg", "entry", "risk", "candidate", "signal"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_no_sweep_without_htf(self):
        bare = AnalysisContext.initial(self.snapshot)
        self.assertIsNone(LiquidityStage().run(bare).sweep)

    def test_no_sweep_without_structure(self):
        no_structure = AnalysisContext.initial(self.snapshot).with_(
            htf=HTFStage.analyze(self.snapshot)
        )
        self.assertIsNone(LiquidityStage().run(no_structure).sweep)

    def test_no_sweep_when_h1_unusable(self):
        thin = (
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
            .with_(htf=htf_context(), swing_highs=(), swing_lows=())
        )
        self.assertIsNone(LiquidityStage().run(thin).sweep)

    def test_does_not_mutate_input_context(self):
        LiquidityStage().run(self.context)
        self.assertIsNone(self.context.sweep)

    def test_is_deterministic(self):
        self.assertEqual(
            LiquidityStage().run(self.context), LiquidityStage().run(self.context)
        )

    def test_stage_name(self):
        self.assertEqual(LiquidityStage().name, "liquidity")


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
        v1_map, v1_debug = self.v1["htf_map"], self.v1["debug"]["htf"]
        for field in ("weekly_bias", "d1_bias", "h4_bias", "location",
                      "pdh", "pdl", "daily_eq", "current_price"):
            with self.subTest(field=field):
                self.assertEqual(getattr(htf, field), v1_map[field])
        self.assertEqual(htf.direction, v1_debug["dir"])
        self.assertEqual(round(htf.bull, 1), v1_debug["bull"])

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


class TestModuleBoundary(unittest.TestCase):
    def test_liquidity_owns_no_target_selection(self):
        # V1's EQH/EQL clustering and TP-target selection belong to analysis.risk
        # (Architecture §3) and must not be pulled in here early.
        import v2.analysis.liquidity as liquidity_module

        for name in (
            "cluster_liquidity_levels",
            "find_liquidity_target",
            "previous_day_high_low",
        ):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(liquidity_module, name))

    def test_sweep_detection_has_a_single_owner(self):
        import v2.analysis.htf as htf_module
        import v2.analysis.regime as regime_module
        import v2.analysis.structure as structure_module

        for module in (htf_module, regime_module, structure_module):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "detect_sweep"))


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def test_first_four_stages_run_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        pipeline = AnalysisPipeline(
            [HTFStage(), RegimeStage(), StructureStage(), LiquidityStage()]
        )
        result = pipeline.run(AnalysisContext.initial(snapshot))

        self.assertIsNotNone(result.htf)
        self.assertIsNotNone(result.regime)
        self.assertIsNotNone(result.swing_highs)
        self.assertIsInstance(result.sweep, LiquiditySweep)
        self.assertIsNone(result.order_blocks)
        self.assertIsNone(result.signal)

    def test_liquidity_skipped_when_htf_bails(self):
        pipeline = AnalysisPipeline(
            [HTFStage(), RegimeStage(), StructureStage(), LiquidityStage()]
        )
        result = pipeline.run(
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
        )
        self.assertIsNone(result.htf)
        self.assertIsNone(result.sweep)


if __name__ == "__main__":
    unittest.main()
