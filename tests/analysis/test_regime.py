"""Phase 3.2: Regime stage — behavioral equivalence with V1 after extraction.

The classifier is compared directly against V1's `SMCStrategy.classify_regime`
across a corpus of synthetic frames (so many regime labels are exercised), and
the execution-mode mapping is compared against V1's `h1_setup["entry_mode"]` on
the committed fixture plus unit-tested branch by branch.

Also asserts the extraction was behavior-neutral: HTF still produces V1's exact
values, and the label HTF consumes is the label RegimeStage publishes.

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
from v2.analysis.regime import (
    RegimeStage,
    classify_regime_label,
    derive_entry_mode,
)
from v2.domain import HTFContext, MarketSnapshot, Regime

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
    atr_tail: float | None = None,
    with_atr: bool = True,
) -> pd.DataFrame:
    """A V1-shaped H1 frame with controllable trend, oscillation and ATR."""
    ts = pd.date_range("2026-01-01", periods=n, freq="h")
    closes = [
        base + step * i + (wobble if i % 2 else -wobble) for i in range(n)
    ]
    data = {
        "timestamp": ts,
        "open": [c - step * 0.3 for c in closes],
        "high": [c + spread for c in closes],
        "low": [c - spread for c in closes],
        "close": closes,
        "v": [1000.0 + i for i in range(n)],
    }
    if with_atr:
        values = [atr] * n
        if atr_tail is not None:
            values[-1] = atr_tail
        data["atr"] = values
    return pd.DataFrame(data)


def htf_context(
    *,
    direction: str | None = "LONG",
    weekly: str = "BULLISH",
    d1: str = "BULLISH",
    location: str = "discount",
    veto: bool = False,
) -> HTFContext:
    return HTFContext(
        pdh=110.0, pdl=90.0, daily_eq=100.0,
        weekly_bias=weekly, d1_bias=d1, h4_bias="CHOPPY",
        location=location, current_price=100.0, momentum_weakening=False,
        bull=8.5, bear=0.0, direction_source="weighted_bull",
        anchor_override=False, veto_active=veto, direction=direction,
    )


def snapshot_with(h1: pd.DataFrame) -> MarketSnapshot:
    last = pd.to_datetime(h1["timestamp"].iloc[-1]) if len(h1) else pd.Timestamp("2026-01-01")
    return MarketSnapshot(
        symbol="REGIME/USDT",
        d1=h1,
        h1=h1,
        h1_last_ts=float(last.timestamp()),
        h1_last_ts_dt=last,
        live_price=100.0,
    )


# A corpus spanning quiet, trending, volatile and degenerate frames.
CORPUS = {
    "flat": frame(60),
    "tight_range": frame(60, spread=0.2, atr=2.0),
    "wide_range": frame(60, spread=6.0, atr=1.0),
    "strong_uptrend": frame(60, step=1.5, spread=2.0),
    "strong_downtrend": frame(60, base=300.0, step=-1.5, spread=2.0),
    "slow_uptrend": frame(60, step=0.05, spread=1.0),
    "choppy": frame(60, wobble=3.0, spread=2.0),
    "choppy_trending": frame(60, step=0.8, wobble=2.0, spread=3.0),
    "atr_spike_last": frame(60, spread=2.0, atr=1.0, atr_tail=5.0),
    "atr_collapse_last": frame(60, spread=2.0, atr=4.0, atr_tail=0.5),
    "short_frame": frame(29),
    "no_atr_column": frame(60, with_atr=False),
    "zero_atr": frame(60, atr=0.0),
}


# --------------------------------------------------------------------------
# Classifier equivalence with V1
# --------------------------------------------------------------------------

class TestClassifierMatchesV1(unittest.TestCase):
    def test_matches_v1_across_the_corpus(self):
        v1 = SMCStrategy()
        for name, df in CORPUS.items():
            with self.subTest(frame=name):
                self.assertEqual(classify_regime_label(df), v1.classify_regime(df))

    def test_corpus_exercises_several_regimes(self):
        labels = {classify_regime_label(df) for df in CORPUS.values()}
        self.assertGreaterEqual(
            len(labels), 3, f"corpus is too narrow to be meaningful: {labels}"
        )

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        self.assertEqual(
            classify_regime_label(h1), SMCStrategy().classify_regime(h1)
        )

    def test_is_deterministic(self):
        df = CORPUS["choppy_trending"]
        self.assertEqual(classify_regime_label(df), classify_regime_label(df))


class TestClassifierUnknownPaths(unittest.TestCase):
    """Every route V1 takes to "unknown"."""

    def test_none_and_empty(self):
        self.assertEqual(classify_regime_label(None), "unknown")
        self.assertEqual(classify_regime_label(frame(0)), "unknown")

    def test_below_thirty_rows(self):
        self.assertEqual(classify_regime_label(frame(29)), "unknown")

    def test_missing_atr_column(self):
        self.assertEqual(classify_regime_label(frame(60, with_atr=False)), "unknown")

    def test_all_nan_atr(self):
        df = frame(60)
        df["atr"] = float("nan")
        self.assertEqual(classify_regime_label(df), "unknown")

    def test_nan_latest_atr(self):
        df = frame(60)
        df.loc[df.index[-1], "atr"] = float("nan")
        self.assertEqual(classify_regime_label(df), "unknown")

    def test_non_positive_latest_atr(self):
        self.assertEqual(classify_regime_label(frame(60, atr=1.0, atr_tail=0.0)), "unknown")

    def test_unknown_paths_match_v1(self):
        v1 = SMCStrategy()
        nan_atr = frame(60)
        nan_atr["atr"] = float("nan")
        for name, df in (
            ("short", frame(29)),
            ("no_atr", frame(60, with_atr=False)),
            ("nan_atr", nan_atr),
            ("zero_last_atr", frame(60, atr=1.0, atr_tail=0.0)),
        ):
            with self.subTest(frame=name):
                self.assertEqual(classify_regime_label(df), v1.classify_regime(df))


# --------------------------------------------------------------------------
# Execution-mode mapping (V1 strategy.py:1308-1342)
# --------------------------------------------------------------------------

class TestDeriveEntryMode(unittest.TestCase):
    def test_expansion_maps_to_continuation(self):
        for label in ("expansion_up", "expansion_down"):
            with self.subTest(label=label):
                self.assertEqual(derive_entry_mode(label, htf_context()), "CONTINUATION")

    def test_ranging_regimes_map_to_retracement(self):
        for label in ("accumulation", "distribution", "rotational"):
            with self.subTest(label=label):
                self.assertEqual(derive_entry_mode(label, htf_context()), "RETRACEMENT")

    def test_compression_long_aligned_is_watch_only(self):
        htf = htf_context(direction="LONG", weekly="BULLISH", d1="BULLISH", location="discount")
        self.assertEqual(derive_entry_mode("compression", htf), "WATCH_ONLY")

    def test_compression_short_aligned_is_watch_only(self):
        htf = htf_context(direction="SHORT", weekly="BEARISH", d1="BEARISH", location="premium")
        self.assertEqual(derive_entry_mode("compression", htf), "WATCH_ONLY")

    def test_compression_requires_full_alignment(self):
        misaligned = (
            htf_context(direction="LONG", weekly="BULLISH", d1="BULLISH", location="premium"),
            htf_context(direction="LONG", weekly="BULLISH", d1="BEARISH", location="discount"),
            htf_context(direction="LONG", weekly="BEARISH", d1="BULLISH", location="discount"),
            htf_context(direction="SHORT", weekly="BEARISH", d1="BEARISH", location="discount"),
        )
        for htf in misaligned:
            with self.subTest(location=htf.location, weekly=htf.weekly_bias, d1=htf.d1_bias):
                self.assertEqual(derive_entry_mode("compression", htf), "NONE")

    def test_neutral_and_unknown_map_to_none(self):
        for label in ("neutral", "unknown"):
            with self.subTest(label=label):
                self.assertEqual(derive_entry_mode(label, htf_context()), "NONE")

    def test_label_is_normalized_like_v1(self):
        # V1 lowercases/strips before matching (regime_str).
        self.assertEqual(derive_entry_mode("  EXPANSION_UP ", htf_context()), "CONTINUATION")

    def test_no_direction_yields_none(self):
        # V1 exits at no_direction before the mapping runs.
        self.assertEqual(derive_entry_mode("expansion_up", htf_context(direction=None)), "NONE")

    def test_missing_htf_yields_none(self):
        self.assertEqual(derive_entry_mode("expansion_up", None), "NONE")

    def test_veto_yields_none(self):
        # V1's veto branch sets entry_mode="NONE" explicitly.
        self.assertEqual(
            derive_entry_mode("expansion_up", htf_context(veto=True)), "NONE"
        )


# --------------------------------------------------------------------------
# Stage behavior + V1 end-to-end
# --------------------------------------------------------------------------

class TestRegimeStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.htf = HTFStage.analyze(self.snapshot)

    def test_produces_a_regime(self):
        regime = RegimeStage.analyze(self.snapshot, self.htf)
        self.assertIsInstance(regime, Regime)

    def test_label_and_entry_mode_match_v1_on_fixture(self):
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        regime = RegimeStage.analyze(self.snapshot, self.htf)
        self.assertEqual(regime.label, v1["debug"]["htf"]["regime"])
        self.assertEqual(regime.entry_mode, v1["h1_setup"]["entry_mode"])

    def test_none_when_htf_absent(self):
        # V1 classifies only after build_htf_map succeeds.
        self.assertIsNone(RegimeStage.analyze(self.snapshot, None))

    def test_none_when_h1_unusable(self):
        thin = snapshot_with(frame(5))
        self.assertIsNone(RegimeStage.analyze(thin, self.htf))

    def test_is_deterministic(self):
        first = RegimeStage.analyze(self.snapshot, self.htf)
        self.assertEqual(first, RegimeStage.analyze(self.snapshot, self.htf))

    def test_stage_name(self):
        self.assertEqual(RegimeStage().name, "regime")

    def test_populates_only_regime(self):
        context = AnalysisContext.initial(self.snapshot).with_(htf=self.htf)
        result = RegimeStage().run(context)
        self.assertIsInstance(result.regime, Regime)
        for field in (
            "swing_highs", "swing_lows", "atr_last", "displacement_valid",
            "volume_spike", "sweep", "order_blocks",
            "fvg", "entry", "risk", "candidate", "signal",
        ):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_does_not_mutate_input_context(self):
        context = AnalysisContext.initial(self.snapshot).with_(htf=self.htf)
        RegimeStage().run(context)
        self.assertIsNone(context.regime)


# --------------------------------------------------------------------------
# Extraction is behavior-neutral
# --------------------------------------------------------------------------

class TestExtractionChangedNothing(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()

    def test_htf_still_matches_v1_after_extraction(self):
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        v1_map, v1_debug = v1["htf_map"], v1["debug"]["htf"]
        htf = HTFStage.analyze(self.snapshot)

        for field in ("pdh", "pdl", "daily_eq", "pwh", "pwl", "weekly_eq",
                      "current_price", "weekly_bias", "d1_bias", "h4_bias",
                      "location", "momentum_weakening"):
            with self.subTest(field=field):
                self.assertEqual(getattr(htf, field), v1_map[field])

        self.assertEqual(htf.direction, v1_debug["dir"])
        self.assertEqual(htf.direction_source, v1_debug["src"])
        self.assertEqual(htf.anchor_override, v1_debug["anchor_override"])
        self.assertEqual(round(htf.bull, 1), v1_debug["bull"])
        self.assertEqual(round(htf.bear, 1), v1_debug["bear"])

    def test_htf_consumes_the_label_regime_publishes(self):
        # One classifier, one result: the label HTF used for its anchor/veto
        # decision is the same label RegimeStage publishes.
        htf = HTFStage.analyze(self.snapshot)
        regime = RegimeStage.analyze(self.snapshot, htf)
        v1_label = SMCStrategy().build_signal_context(
            self.snapshot.d1, self.snapshot.h1
        )["debug"]["htf"]["regime"]
        self.assertEqual(regime.label, v1_label)


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def test_htf_then_regime_populate_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        pipeline = AnalysisPipeline([HTFStage(), RegimeStage()])
        result = pipeline.run(AnalysisContext.initial(snapshot))

        self.assertIsInstance(result.htf, HTFContext)
        self.assertIsInstance(result.regime, Regime)
        self.assertIsNone(result.signal)

    def test_regime_none_when_htf_bails(self):
        pipeline = AnalysisPipeline([HTFStage(), RegimeStage()])
        result = pipeline.run(AnalysisContext.initial(snapshot_with(frame(5))))
        self.assertIsNone(result.htf)
        self.assertIsNone(result.regime)


if __name__ == "__main__":
    unittest.main()
