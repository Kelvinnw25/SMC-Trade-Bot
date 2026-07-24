"""Phase 3.3: Structure stage — behavioral equivalence with V1 after extraction.

Swing detection is compared directly against V1's `SMCStrategy.identify_swings`
across a corpus of synthetic frames and both V1 parameter sets. Displacement,
the ATR read and the volume-spike test are compared against V1's own inline
expressions on the committed fixture and on crafted edge frames.

Also asserts the extraction was behavior-neutral: HTF still produces V1's exact
values now that it imports swing detection instead of owning it.

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
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.regime import RegimeStage
from v2.analysis.structure import (
    STRUCTURE_PROMINENCE_MULT,
    STRUCTURE_SWING_WINDOW,
    StructureStage,
    detect_volume_spike,
    identify_swings,
    is_displacement_candle,
    last_atr,
    to_swings,
)
from v2.domain import HTFContext, MarketSnapshot, Swing

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def frame(
    n: int,
    *,
    base: float = 100.0,
    step: float = 0.0,
    wobble: float = 0.0,
    spread: float = 1.0,
    atr: float = 1.0,
    with_atr: bool = True,
    with_volume: bool = True,
    flat_high: bool = False,
) -> pd.DataFrame:
    """A V1-shaped H1 frame with controllable shape."""
    ts = pd.date_range("2026-01-01", periods=n, freq="h")
    closes = [base + step * i + (wobble if i % 2 else -wobble) for i in range(n)]
    highs = [base + spread] * n if flat_high else [c + spread for c in closes]
    data = {
        "timestamp": ts,
        "open": [c - step * 0.3 for c in closes],
        "high": highs,
        "low": [c - spread for c in closes],
        "close": closes,
    }
    if with_volume:
        data["v"] = [1000.0 + (i % 5) * 10.0 for i in range(n)]
    if with_atr:
        data["atr"] = [atr] * n
    return pd.DataFrame(data)


def candle(open_: float, high: float, low: float, close: float) -> pd.Series:
    return pd.Series({"open": open_, "high": high, "low": low, "close": close})


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
        symbol="STRUCT/USDT", d1=h1, h1=h1,
        h1_last_ts=float(last.timestamp()), h1_last_ts_dt=last, live_price=100.0,
    )


CORPUS = {
    "flat": frame(60),
    "uptrend": frame(60, step=1.5),
    "downtrend": frame(60, base=300.0, step=-1.5),
    "choppy": frame(60, wobble=3.0, spread=2.0),
    "choppy_trending": frame(60, step=0.8, wobble=2.0, spread=3.0),
    "tight": frame(60, spread=0.05, atr=3.0),
    "wide": frame(60, spread=8.0, atr=0.5),
    "plateau_highs": frame(60, wobble=2.0, flat_high=True),
    "no_atr": frame(60, with_atr=False),
    "tiny": frame(6),
    "window_edge": frame(5),
}


# --------------------------------------------------------------------------
# Swing detection vs V1
# --------------------------------------------------------------------------

class TestSwingDetectionMatchesV1(unittest.TestCase):
    def test_matches_v1_structural_parameters(self):
        v1 = SMCStrategy()
        for name, df in CORPUS.items():
            with self.subTest(frame=name):
                self.assertEqual(
                    identify_swings(df, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT),
                    v1.identify_swings(
                        df,
                        custom_window=STRUCTURE_SWING_WINDOW,
                        prominence_mult=STRUCTURE_PROMINENCE_MULT,
                    ),
                )

    def test_matches_v1_bias_parameters(self):
        # The other V1 call site (strategy.py:620) — the set analysis.htf uses.
        v1 = SMCStrategy()
        for name, df in CORPUS.items():
            with self.subTest(frame=name):
                self.assertEqual(
                    identify_swings(df, 3, 0.18),
                    v1.identify_swings(df, custom_window=3, prominence_mult=0.18),
                )

    def test_matches_v1_with_prominence_disabled(self):
        v1 = SMCStrategy()
        df = CORPUS["choppy"]
        self.assertEqual(
            identify_swings(df, 2, 0.0),
            v1.identify_swings(df, custom_window=2, prominence_mult=0.0),
        )

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        self.assertEqual(
            identify_swings(h1, STRUCTURE_SWING_WINDOW, STRUCTURE_PROMINENCE_MULT),
            SMCStrategy().identify_swings(h1, custom_window=2, prominence_mult=0.3),
        )

    def test_corpus_actually_produces_swings(self):
        counts = [
            len(identify_swings(df, 2, 0.0)[0]) for df in CORPUS.values()
        ]
        self.assertGreater(sum(counts), 0, "corpus produced no swing highs at all")

    def test_is_deterministic(self):
        df = CORPUS["choppy_trending"]
        self.assertEqual(identify_swings(df, 2, 0.3), identify_swings(df, 2, 0.3))


class TestSwingBoundaries(unittest.TestCase):
    def test_frame_shorter_than_window_yields_nothing(self):
        self.assertEqual(identify_swings(frame(4), 2, 0.3), ([], []))

    def test_window_edges_are_never_swings(self):
        # V1 scans range(window, len-window): the first/last `window` bars are
        # structurally ineligible.
        highs, lows = identify_swings(CORPUS["choppy"], 2, 0.0)
        n = len(CORPUS["choppy"])
        for s in highs + lows:
            self.assertGreaterEqual(s["index"], 2)
            self.assertLess(s["index"], n - 2)

    def test_plateau_dedup_quirk_preserved(self):
        # V1 skips a swing whose price AND index-1 match the previous entry;
        # equal-height neighbours therefore collapse. Compared against V1.
        df = CORPUS["plateau_highs"]
        self.assertEqual(
            identify_swings(df, 2, 0.0),
            SMCStrategy().identify_swings(df, custom_window=2, prominence_mult=0.0),
        )

    def test_missing_atr_disables_prominence_like_v1(self):
        df = CORPUS["no_atr"]
        self.assertEqual(
            identify_swings(df, 2, 0.3),
            SMCStrategy().identify_swings(df, custom_window=2, prominence_mult=0.3),
        )


class TestToSwings(unittest.TestCase):
    def test_translates_to_domain_objects(self):
        raw = [{"index": 3, "price": 101.5}, {"index": 7, "price": 99.0}]
        result = to_swings(raw, "high")
        self.assertEqual(
            result,
            (Swing(index=3, price=101.5, kind="high"),
             Swing(index=7, price=99.0, kind="high")),
        )

    def test_preserves_order_and_values(self):
        raw_highs, _ = identify_swings(CORPUS["choppy"], 2, 0.0)
        swings = to_swings(raw_highs, "high")
        self.assertEqual(len(swings), len(raw_highs))
        for swing, item in zip(swings, raw_highs):
            self.assertEqual(swing.index, int(item["index"]))
            self.assertEqual(swing.price, float(item["price"]))

    def test_empty_input(self):
        self.assertEqual(to_swings([], "low"), ())


# --------------------------------------------------------------------------
# Displacement (V1's structural break signal)
# --------------------------------------------------------------------------

class TestDisplacement(unittest.TestCase):
    def test_long_displacement(self):
        # Bullish body 6.0 >= 0.6*ATR(5) and body ratio 0.75 >= 0.5.
        c = candle(open_=100.0, high=104.0, low=96.0, close=106.0)
        self.assertTrue(is_displacement_candle(c, "LONG", 5.0))

    def test_short_displacement(self):
        c = candle(open_=106.0, high=108.0, low=98.0, close=100.0)
        self.assertTrue(is_displacement_candle(c, "SHORT", 5.0))

    def test_wrong_direction_candle_rejected(self):
        bullish = candle(open_=100.0, high=104.0, low=96.0, close=106.0)
        self.assertFalse(is_displacement_candle(bullish, "SHORT", 5.0))

    def test_body_below_atr_threshold_rejected(self):
        c = candle(open_=100.0, high=101.0, low=99.0, close=100.5)
        self.assertFalse(is_displacement_candle(c, "LONG", 5.0))

    def test_body_ratio_below_half_rejected(self):
        # Body 4 but range 20 -> ratio 0.2.
        c = candle(open_=100.0, high=114.0, low=94.0, close=104.0)
        self.assertFalse(is_displacement_candle(c, "LONG", 1.0))

    def test_zero_range_candle_is_not_displacement(self):
        c = candle(open_=100.0, high=100.0, low=100.0, close=100.0)
        self.assertFalse(is_displacement_candle(c, "LONG", 1.0))

    def test_none_direction_uses_short_branch_quirk(self):
        # V1's `else` applies the SHORT test to any non-LONG direction.
        bearish = candle(open_=106.0, high=108.0, low=98.0, close=100.0)
        self.assertTrue(is_displacement_candle(bearish, None, 5.0))
        bullish = candle(open_=100.0, high=104.0, low=96.0, close=106.0)
        self.assertFalse(is_displacement_candle(bullish, None, 5.0))

    def test_matches_v1_on_fixture_last_candle(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        htf = HTFStage.analyze(snapshot)
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        self.assertEqual(
            is_displacement_candle(h1.iloc[-1], htf.direction, last_atr(h1)),
            v1["h1_setup"]["displacement_valid"],
        )


# --------------------------------------------------------------------------
# ATR read + volume spike
# --------------------------------------------------------------------------

class TestLastAtr(unittest.TestCase):
    def test_reads_last_non_null(self):
        df = frame(30, atr=2.5)
        df.loc[df.index[-1], "atr"] = float("nan")
        self.assertEqual(last_atr(df), 2.5)

    def test_missing_column_yields_zero(self):
        self.assertEqual(last_atr(frame(30, with_atr=False)), 0.0)

    def test_all_nan_yields_zero(self):
        df = frame(30)
        df["atr"] = float("nan")
        self.assertEqual(last_atr(df), 0.0)

    def test_matches_v1_expression_on_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        expected = float(h1["atr"].dropna().iloc[-1])
        self.assertEqual(last_atr(h1), expected)


class TestVolumeSpike(unittest.TestCase):
    def test_spike_detected(self):
        df = frame(30)
        df.loc[df.index[-1], "v"] = 100000.0
        self.assertTrue(detect_volume_spike(df))

    def test_no_spike_on_steady_volume(self):
        df = frame(30)
        df["v"] = 1000.0
        self.assertFalse(detect_volume_spike(df))

    def test_missing_volume_column(self):
        self.assertFalse(detect_volume_spike(frame(30, with_volume=False)))

    def test_frame_shorter_than_sma_window(self):
        self.assertFalse(detect_volume_spike(frame(19)))

    def test_matches_v1_expression_on_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        self.assertEqual(detect_volume_spike(h1), v1["h1_setup"]["volume_spike"])


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestStructureStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.htf = HTFStage.analyze(self.snapshot)
        self.context = AnalysisContext.initial(self.snapshot).with_(htf=self.htf)

    def test_populates_structure_products(self):
        result = StructureStage().run(self.context)
        self.assertIsInstance(result.swing_highs, tuple)
        self.assertIsInstance(result.swing_lows, tuple)
        self.assertIsInstance(result.atr_last, float)
        self.assertIsInstance(result.displacement_valid, bool)
        self.assertIsInstance(result.volume_spike, bool)

    def test_swings_match_v1_structural_call(self):
        result = StructureStage().run(self.context)
        h1 = safe_frame(self.snapshot.h1, H1_MIN_ROWS)
        raw_highs, raw_lows = SMCStrategy().identify_swings(
            h1, custom_window=2, prominence_mult=0.3
        )
        self.assertEqual(result.swing_highs, to_swings(raw_highs, "high"))
        self.assertEqual(result.swing_lows, to_swings(raw_lows, "low"))

    def test_flags_match_v1_h1_setup(self):
        result = StructureStage().run(self.context)
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        self.assertEqual(result.displacement_valid, v1["h1_setup"]["displacement_valid"])
        self.assertEqual(result.volume_spike, v1["h1_setup"]["volume_spike"])
        self.assertEqual(result.atr_last, v1["h1_setup"]["atr_last"])

    def test_populates_only_structure_fields(self):
        result = StructureStage().run(self.context)
        for field in ("regime", "sweep", "order_blocks", "fvg", "entry",
                      "risk", "candidate", "signal"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_no_structure_without_htf(self):
        bare = AnalysisContext.initial(self.snapshot)
        result = StructureStage().run(bare)
        self.assertIsNone(result.swing_highs)
        self.assertIsNone(result.atr_last)

    def test_no_structure_when_h1_unusable(self):
        thin = AnalysisContext.initial(snapshot_with(frame(5))).with_(htf=htf_context())
        result = StructureStage().run(thin)
        self.assertIsNone(result.swing_highs)

    def test_does_not_mutate_input_context(self):
        StructureStage().run(self.context)
        self.assertIsNone(self.context.swing_highs)

    def test_is_deterministic(self):
        first = StructureStage().run(self.context)
        second = StructureStage().run(self.context)
        self.assertEqual(first, second)

    def test_stage_name(self):
        self.assertEqual(StructureStage().name, "structure")


# --------------------------------------------------------------------------
# Extraction neutrality + pipeline
# --------------------------------------------------------------------------

class TestExtractionChangedNothing(unittest.TestCase):
    def test_htf_still_matches_v1_after_swing_extraction(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        v1_map, v1_debug = v1["htf_map"], v1["debug"]["htf"]
        htf = HTFStage.analyze(snapshot)

        for field in ("weekly_bias", "d1_bias", "h4_bias", "location",
                      "pdh", "pdl", "daily_eq", "current_price",
                      "momentum_weakening"):
            with self.subTest(field=field):
                self.assertEqual(getattr(htf, field), v1_map[field])

        self.assertEqual(htf.direction, v1_debug["dir"])
        self.assertEqual(htf.direction_source, v1_debug["src"])
        self.assertEqual(htf.fallback_h1_bias, v1_debug["h1_fb"])
        self.assertEqual(round(htf.bull, 1), v1_debug["bull"])
        self.assertEqual(round(htf.bear, 1), v1_debug["bear"])

    def test_single_swing_implementation(self):
        # HTF must import the one implementation, never define its own.
        import v2.analysis.htf as htf_module
        import v2.analysis.structure as structure_module

        self.assertIs(htf_module.identify_swings, structure_module.identify_swings)
        self.assertFalse(hasattr(htf_module, "_identify_swings"))


class TestPipelineIntegration(unittest.TestCase):
    def test_htf_regime_structure_run_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        pipeline = AnalysisPipeline([HTFStage(), RegimeStage(), StructureStage()])
        result = pipeline.run(AnalysisContext.initial(snapshot))

        self.assertIsNotNone(result.htf)
        self.assertIsNotNone(result.regime)
        self.assertIsNotNone(result.swing_highs)
        self.assertIsNotNone(result.atr_last)
        self.assertIsNone(result.sweep)
        self.assertIsNone(result.signal)

    def test_structure_skipped_when_htf_bails(self):
        pipeline = AnalysisPipeline([HTFStage(), RegimeStage(), StructureStage()])
        result = pipeline.run(AnalysisContext.initial(snapshot_with(frame(5))))
        self.assertIsNone(result.htf)
        self.assertIsNone(result.swing_highs)


if __name__ == "__main__":
    unittest.main()
