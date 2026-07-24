"""Phase 3.1: HTF stage — behavioral equivalence with V1.

The flagship test runs V1's own `build_signal_context` on the committed
regression fixture and compares every `HTFContext` field against V1's `htf_map`
and `debug["htf"]`. The pure direction/veto helpers are then exercised
exhaustively with synthetic inputs, which is the only practical way to cover
every bias/location/anchor combination.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from src.strategy import SMCStrategy
from tests.regression.oracle import V1Oracle
from tests.regression.snapshot import InputSnapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.htf import HTFStage, _select_direction, _veto
from v2.domain import HTFContext, MarketSnapshot

# The committed deterministic snapshot owned by the regression harness (read
# only — the harness itself is frozen).
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "regression"
    / "fixtures"
    / "synthetic_short.json"
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def ohlc_frame(
    n: int,
    *,
    start: str = "2026-01-01",
    freq: str = "D",
    base: float = 100.0,
    step: float = 1.0,
    with_atr: bool = True,
) -> pd.DataFrame:
    """A V1-shaped frame with a linear trend (monotonic → no swing points)."""
    ts = pd.date_range(start, periods=n, freq=freq)
    closes = [base + step * i for i in range(n)]
    data = {
        "timestamp": ts,
        "open": [c - step * 0.25 for c in closes],
        "high": [c + abs(step) * 0.5 + 0.5 for c in closes],
        "low": [c - abs(step) * 0.5 - 0.5 for c in closes],
        "close": closes,
        "v": [1000.0 + i for i in range(n)],
    }
    if with_atr:
        data["atr"] = [1.0] * n
    return pd.DataFrame(data)


def snapshot_from(d1: pd.DataFrame, h1: pd.DataFrame) -> MarketSnapshot:
    if len(h1):
        last = pd.to_datetime(h1["timestamp"].iloc[-1])
    else:
        last = pd.Timestamp("2026-01-01")
    return MarketSnapshot(
        symbol="EDGE/USDT",
        d1=d1,
        h1=h1,
        h1_last_ts=float(last.timestamp()),
        h1_last_ts_dt=last,
        live_price=1.0,
    )


def htf_map(weekly: str, d1: str, h4: str, location: str) -> dict:
    return {"weekly_bias": weekly, "d1_bias": d1, "h4_bias": h4, "location": location}


# --------------------------------------------------------------------------
# V1 equivalence on the committed fixture
# --------------------------------------------------------------------------

class TestV1Equivalence(unittest.TestCase):
    """Every HTFContext field must equal V1's value for the same input."""

    @classmethod
    def setUpClass(cls):
        cls.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(cls.snapshot.d1, cls.snapshot.h1)
        cls.v1_map = v1.get("htf_map") or {}
        cls.v1_debug = (v1.get("debug") or {}).get("htf", {})
        cls.v1_veto = (v1.get("debug") or {}).get("veto", {})
        cls.htf = HTFStage.analyze(cls.snapshot)

    def test_produces_an_htf_context(self):
        self.assertIsInstance(self.htf, HTFContext)
        self.assertTrue(self.v1_map, "V1 produced no htf_map for the fixture")

    def test_htf_map_levels_match_v1(self):
        for field in ("pdh", "pdl", "daily_eq", "pwh", "pwl", "weekly_eq", "current_price"):
            with self.subTest(field=field):
                self.assertEqual(getattr(self.htf, field), self.v1_map[field])

    def test_biases_and_location_match_v1(self):
        self.assertEqual(self.htf.weekly_bias, self.v1_map["weekly_bias"])
        self.assertEqual(self.htf.d1_bias, self.v1_map["d1_bias"])
        self.assertEqual(self.htf.h4_bias, self.v1_map["h4_bias"])
        self.assertEqual(self.htf.location, self.v1_map["location"])
        self.assertEqual(self.htf.momentum_weakening, self.v1_map["momentum_weakening"])

    def test_direction_block_matches_v1(self):
        self.assertEqual(self.htf.direction, self.v1_debug["dir"])
        self.assertEqual(self.htf.direction_source, self.v1_debug["src"])
        self.assertEqual(self.htf.fallback_h1_bias, self.v1_debug["h1_fb"])
        self.assertEqual(self.htf.anchor_override, self.v1_debug["anchor_override"])

    def test_bull_bear_match_v1_reported_values(self):
        # V1 reports round(x, 1); all reachable sums are exact multiples of 0.5,
        # so the rounded and raw values coincide.
        self.assertEqual(round(self.htf.bull, 1), self.v1_debug["bull"])
        self.assertEqual(round(self.htf.bear, 1), self.v1_debug["bear"])
        self.assertEqual(self.htf.bull, round(self.htf.bull, 1))
        self.assertEqual(self.htf.bear, round(self.htf.bear, 1))

    def test_veto_matches_v1(self):
        self.assertEqual(self.htf.veto_active, bool(self.v1_veto.get("active", False)))
        self.assertEqual(self.htf.veto_reason, self.v1_veto.get("reason"))

    def test_is_deterministic(self):
        self.assertEqual(HTFStage.analyze(self.snapshot), self.htf)


class TestRegressionHarnessCompatibility(unittest.TestCase):
    """Every HTF field the harness reports must be derivable from HTFContext."""

    def test_matches_v1_oracle_observables(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        observed = V1Oracle().analyze(snapshot)
        htf = HTFStage.analyze(snapshot)

        self.assertEqual(htf.weekly_bias, observed.weekly_bias)
        self.assertEqual(htf.d1_bias, observed.d1_bias)
        self.assertEqual(htf.h4_bias, observed.h4_bias)
        self.assertEqual(htf.location, observed.location)
        self.assertEqual(htf.direction, observed.direction)
        self.assertEqual(htf.direction_source, observed.direction_source)
        self.assertEqual(htf.bull, observed.bull)
        self.assertEqual(htf.bear, observed.bear)
        self.assertEqual(htf.anchor_override, observed.anchor_override)
        self.assertEqual(htf.veto_reason, observed.veto_reason)


# --------------------------------------------------------------------------
# Early exits (V1 invalid_input / invalid_htf_map)
# --------------------------------------------------------------------------

class TestEarlyExits(unittest.TestCase):
    def test_insufficient_d1_rows(self):
        snap = snapshot_from(ohlc_frame(4), ohlc_frame(40, freq="h"))
        self.assertIsNone(HTFStage.analyze(snap))

    def test_insufficient_h1_rows(self):
        snap = snapshot_from(ohlc_frame(40), ohlc_frame(19, freq="h"))
        self.assertIsNone(HTFStage.analyze(snap))

    def test_empty_frames(self):
        empty = ohlc_frame(0)
        self.assertIsNone(HTFStage.analyze(snapshot_from(empty, ohlc_frame(40, freq="h"))))
        self.assertIsNone(HTFStage.analyze(snapshot_from(ohlc_frame(40), empty)))

    def test_nan_rows_drop_below_minimum(self):
        d1 = ohlc_frame(10)
        d1.loc[2:, "close"] = float("nan")  # only 2 usable rows remain
        self.assertIsNone(HTFStage.analyze(snapshot_from(d1, ohlc_frame(40, freq="h"))))

    def test_single_calendar_day_yields_no_htf_map(self):
        # 8 D1-shaped rows all inside one date -> fewer than 2 daily groups.
        d1 = ohlc_frame(8, start="2026-01-01 00:00", freq="h")
        self.assertIsNone(HTFStage.analyze(snapshot_from(d1, ohlc_frame(40, freq="h"))))


# --------------------------------------------------------------------------
# Weighted direction selection (pure)
# --------------------------------------------------------------------------

class TestDirectionSelection(unittest.TestCase):
    def select(self, weekly, d1, h4, location, *, h1=None, regime="neutral"):
        return _select_direction(htf_map(weekly, d1, h4, location), h1, regime)

    def test_full_bullish_alignment(self):
        bull, bear, direction, source, fb, anchor = self.select(
            "BULLISH", "BULLISH", "BULLISH", "discount"
        )
        self.assertEqual((bull, bear), (8.5, 0.0))  # 4 + 2 + 1 + 1.5
        self.assertEqual(direction, "LONG")
        self.assertEqual(source, "weighted_bull")
        self.assertIsNone(fb)
        self.assertFalse(anchor)

    def test_full_bearish_alignment(self):
        bull, bear, direction, source, _, anchor = self.select(
            "BEARISH", "BEARISH", "BEARISH", "premium"
        )
        self.assertEqual((bull, bear), (0.0, 8.5))
        self.assertEqual(direction, "SHORT")
        self.assertEqual(source, "weighted_bear")
        self.assertFalse(anchor)

    def test_discount_with_bearish_weekly_credits_bull(self):
        # V1 quirk preserved: in discount, a BEARISH weekly still adds +0.5 BULL.
        bull, bear, _, _, _, _ = self.select("BEARISH", "CHOPPY", "CHOPPY", "discount")
        self.assertEqual((bull, bear), (0.5, 4.0))

    def test_premium_with_bullish_weekly_credits_bear(self):
        # Mirror quirk: in premium, a BULLISH weekly still adds +0.5 BEAR.
        bull, bear, _, _, _, _ = self.select("BULLISH", "CHOPPY", "CHOPPY", "premium")
        self.assertEqual((bull, bear), (4.0, 0.5))

    def test_equilibrium_adds_nothing(self):
        bull, bear, _, _, _, _ = self.select("BULLISH", "CHOPPY", "CHOPPY", "equilibrium")
        self.assertEqual((bull, bear), (4.0, 0.0))

    def test_no_direction_when_gap_too_small(self):
        # bull 4.0 vs bear 3.5 -> passes the floor but fails the 1.0 gap.
        bull, bear, direction, source, _, anchor = self.select(
            "BULLISH", "BEARISH", "BEARISH", "premium"
        )
        self.assertEqual((bull, bear), (4.0, 3.5))
        self.assertIsNone(direction)
        self.assertEqual(source, "none")
        self.assertFalse(anchor)

    def test_fallback_consulted_only_below_floor(self):
        # All-CHOPPY -> both weights 0 -> fallback runs; None frame -> "CHOPPY".
        bull, bear, direction, source, fb, _ = self.select(
            "CHOPPY", "CHOPPY", "CHOPPY", "equilibrium"
        )
        self.assertEqual((bull, bear), (0.0, 0.0))
        self.assertEqual(fb, "CHOPPY")
        self.assertEqual(source, "none")
        self.assertIsNone(direction)

    def test_fallback_bullish_credits_bull_and_sets_source(self):
        uptrend = ohlc_frame(30, freq="h", step=1.0)
        bull, bear, direction, source, fb, _ = self.select(
            "CHOPPY", "CHOPPY", "CHOPPY", "equilibrium", h1=uptrend
        )
        self.assertEqual(fb, "BULLISH")
        self.assertEqual((bull, bear), (1.0, 0.0))
        self.assertEqual(source, "fallback_h1")
        self.assertIsNone(direction)  # 1.0 is still below the 4.0 floor

    def test_fallback_bearish_credits_bear_and_sets_source(self):
        downtrend = ohlc_frame(30, freq="h", base=200.0, step=-1.0)
        bull, bear, _, source, fb, _ = self.select(
            "CHOPPY", "CHOPPY", "CHOPPY", "equilibrium", h1=downtrend
        )
        self.assertEqual(fb, "BEARISH")
        self.assertEqual((bull, bear), (0.0, 1.0))
        self.assertEqual(source, "fallback_h1")

    def test_fallback_source_survives_a_weighted_direction(self):
        # When the fallback fires and then pushes past the floor, V1 keeps
        # "fallback_h1" as the source rather than overwriting it.
        uptrend = ohlc_frame(30, freq="h", step=1.0)
        bull, _, direction, source, _, _ = self.select(
            "CHOPPY", "BULLISH", "BULLISH", "equilibrium", h1=uptrend
        )
        self.assertEqual(bull, 4.0)  # 2 + 1 + 1 fallback
        self.assertEqual(direction, "LONG")
        self.assertEqual(source, "fallback_h1")


class TestAnchorOverride(unittest.TestCase):
    def select(self, weekly, d1, h4, location, regime):
        return _select_direction(htf_map(weekly, d1, h4, location), None, regime)

    def test_rotational_bullish_anchor(self):
        bull, bear, direction, source, _, anchor = self.select(
            "BULLISH", "BEARISH", "BEARISH", "premium", "rotational"
        )
        self.assertEqual((bull, bear), (4.0, 3.5))
        self.assertEqual(direction, "LONG")
        self.assertEqual(source, "weekly_anchor_rotational")
        self.assertTrue(anchor)

    def test_rotational_bearish_anchor(self):
        bull, bear, direction, source, _, anchor = self.select(
            "BEARISH", "BULLISH", "BULLISH", "discount", "rotational"
        )
        self.assertEqual((bull, bear), (3.5, 4.0))
        self.assertEqual(direction, "SHORT")
        self.assertEqual(source, "weekly_anchor_rotational")
        self.assertTrue(anchor)

    def test_anchor_requires_rotational_regime(self):
        _, _, direction, source, _, anchor = self.select(
            "BULLISH", "BEARISH", "BEARISH", "premium", "neutral"
        )
        self.assertIsNone(direction)
        self.assertEqual(source, "none")
        self.assertFalse(anchor)

    def test_anchor_not_applied_when_direction_already_set(self):
        _, _, direction, source, _, anchor = self.select(
            "BULLISH", "BULLISH", "BULLISH", "discount", "rotational"
        )
        self.assertEqual(direction, "LONG")
        self.assertEqual(source, "weighted_bull")
        self.assertFalse(anchor)

    def test_anchor_needs_weekly_to_lead(self):
        # Rotational, no direction, but weekly is CHOPPY -> no anchor.
        _, _, direction, _, _, anchor = self.select(
            "CHOPPY", "CHOPPY", "CHOPPY", "equilibrium", "rotational"
        )
        self.assertIsNone(direction)
        self.assertFalse(anchor)


# --------------------------------------------------------------------------
# Veto (pure)
# --------------------------------------------------------------------------

class TestVeto(unittest.TestCase):
    def test_short_vetoed_in_bullish_expansion_up(self):
        self.assertEqual(
            _veto("SHORT", "BULLISH", "expansion_up"),
            (True, "weekly_bullish_expansion_up_short_veto"),
        )

    def test_long_vetoed_in_bearish_expansion_down(self):
        self.assertEqual(
            _veto("LONG", "BEARISH", "expansion_down"),
            (True, "weekly_bearish_expansion_down_long_veto"),
        )

    def test_aligned_directions_are_not_vetoed(self):
        self.assertEqual(_veto("LONG", "BULLISH", "expansion_up"), (False, None))
        self.assertEqual(_veto("SHORT", "BEARISH", "expansion_down"), (False, None))

    def test_veto_requires_the_matching_regime(self):
        self.assertEqual(_veto("SHORT", "BULLISH", "rotational"), (False, None))
        self.assertEqual(_veto("LONG", "BEARISH", "neutral"), (False, None))

    def test_no_direction_is_never_vetoed(self):
        self.assertEqual(_veto(None, "BULLISH", "expansion_up"), (False, None))


# --------------------------------------------------------------------------
# Stage wiring
# --------------------------------------------------------------------------

class TestStageWiring(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        self.context = AnalysisContext.initial(self.snapshot)

    def test_populates_only_htf(self):
        result = HTFStage().run(self.context)
        self.assertIsInstance(result.htf, HTFContext)
        for field in (
            "regime", "swing_highs", "swing_lows", "atr_last",
            "displacement_valid", "volume_spike", "sweep", "order_blocks",
            "fvg", "entry", "risk", "candidate", "signal",
        ):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_does_not_mutate_the_input_context(self):
        HTFStage().run(self.context)
        self.assertIsNone(self.context.htf)

    def test_stage_name(self):
        self.assertEqual(HTFStage().name, "htf")

    def test_sets_none_when_v1_would_bail(self):
        thin = AnalysisContext.initial(
            snapshot_from(ohlc_frame(4), ohlc_frame(40, freq="h"))
        )
        self.assertIsNone(HTFStage().run(thin).htf)


if __name__ == "__main__":
    unittest.main()
