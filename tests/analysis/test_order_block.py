"""Phase 3.5: Order Block stage — behavioral equivalence with V1.

The candidate set is compared **field-for-field** against V1's own
`debug["ob_audit"]["ob_candidates"]` across a deterministic pseudo-random corpus
of frames that drives V1's real `build_signal_context`, in both directions, plus
`h1_setup["fresh_ob"]` on every corpus member (V1's only OB observable on the
paths where it returns before publishing the audit).

Controlled frames then exercise every branch and threshold directly: origin
candle selection, the scan window, first-match selection, mitigation, and each
quality-filter rejection.

Also asserts the prior stages are untouched: HTF, Regime, Structure and
Liquidity still produce V1's exact values.

No network, no Telegram, no filesystem writes.
"""
from __future__ import annotations

import random
import unittest
from pathlib import Path

import pandas as pd

from src.strategy import SMCStrategy
from tests.regression.snapshot import InputSnapshot
from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage, detect_sweep
from v2.analysis.order_block import (
    OB_MIN_DISTANCE_ATR_MULT,
    OB_MIN_SIZE_ATR_MULT,
    OB_NOT_LOST_ATR_MULT,
    OB_WINDOW_LOOKBACK,
    OrderBlockStage,
    candidate_window,
    detect_order_blocks,
    fresh_ob,
    last_close,
    passes_quality_filter,
    selected_block,
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
from v2.domain import HTFContext, MarketSnapshot, OrderBlock

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
    n: int = 30,
    close: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    atr: float | None = 1.0,
) -> pd.DataFrame:
    """A flat V1-shaped frame of doji bars; individual bars are overridden per
    test so only the intended candidates exist."""
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


def down_bar(df: pd.DataFrame, i: int, *, open_: float, low: float, close=None, high=None):
    """A bearish origin candle: the LONG (demand-zone) candidate shape."""
    return set_bar(
        df, i,
        open_=open_,
        low=low,
        close=open_ - 0.5 if close is None else close,
        high=open_ + 0.5 if high is None else high,
    )


def up_bar(df: pd.DataFrame, i: int, *, open_: float, high: float, close=None, low=None):
    """A bullish origin candle: the SHORT (supply-zone) candidate shape."""
    return set_bar(
        df, i,
        open_=open_,
        high=high,
        close=open_ + 0.5 if close is None else close,
        low=open_ - 0.5 if low is None else low,
    )


def random_frame(n: int, rng: random.Random, *, base: float, drift: float,
                 vol: float, atr: float, freq: str = "h") -> pd.DataFrame:
    """A deterministic pseudo-random V1-shaped frame (seeded; no wall clock)."""
    ts = pd.date_range("2026-01-01", periods=n, freq=freq)
    closes, price = [], base
    for _ in range(n):
        price = max(1.0, price + drift + rng.gauss(0, vol))
        closes.append(price)
    opens = [base] + closes[:-1]
    highs = [max(o, c) + abs(rng.gauss(0, vol)) * 0.6 for o, c in zip(opens, closes)]
    lows = [min(o, c) - abs(rng.gauss(0, vol)) * 0.6 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "timestamp": ts, "open": opens, "high": highs, "low": lows, "close": closes,
        "v": [1000.0 + rng.random() * 500 for _ in range(n)],
        "atr": [atr] * n,
    })


_DRIFTS = (-1.2, -0.4, -0.1, 0.1, 0.4, 1.2)
_VOLS = (0.3, 1.0, 2.5)
_ATRS = (0.5, 1.5, 3.0)


def corpus():
    """(name, d1, h1) triples driving V1's full `build_signal_context`.

    Deterministic: every frame comes from a fixed seed, so the corpus is
    identical on every run and on every machine.
    """
    cases = []
    for seed in range(30):
        rng = random.Random(seed)
        drift = rng.choice(_DRIFTS)
        vol = rng.choice(_VOLS)
        atr = rng.choice(_ATRS)
        d1 = random_frame(30, random.Random(seed + 9000), base=200.0,
                          drift=drift * 3, vol=vol * 2, atr=atr * 2, freq="D")
        h1 = random_frame(120, rng, base=200.0, drift=drift, vol=vol, atr=atr)
        cases.append((f"seed{seed}", d1, h1))
    return cases


def snapshot_of(d1: pd.DataFrame, h1: pd.DataFrame, symbol: str = "OB/USDT") -> MarketSnapshot:
    last = pd.to_datetime(h1["timestamp"].iloc[-1]) if len(h1) else pd.Timestamp("2026-01-01")
    return MarketSnapshot(
        symbol=symbol, d1=d1, h1=h1,
        h1_last_ts=float(last.timestamp()), h1_last_ts_dt=last, live_price=float(
            h1["close"].iloc[-1] if len(h1) else 100.0
        ),
    )


def snapshot_with(h1: pd.DataFrame) -> MarketSnapshot:
    return snapshot_of(h1, h1)


def htf_context(direction: str | None = "LONG") -> HTFContext:
    return HTFContext(
        pdh=110.0, pdl=90.0, daily_eq=100.0,
        weekly_bias="BULLISH", d1_bias="BULLISH", h4_bias="CHOPPY",
        location="discount", current_price=100.0, momentum_weakening=False,
        bull=8.5, bear=0.0, direction_source="weighted_bull",
        anchor_override=False, veto_active=False, direction=direction,
    )


def block_at(blocks, index: int) -> OrderBlock:
    """The candidate registered for bar `index` (fails loudly if absent)."""
    for block in blocks:
        if block.index == index:
            return block
    raise AssertionError(f"no candidate at index {index}: {[b.index for b in blocks]}")


def v2_blocks(d1: pd.DataFrame, h1: pd.DataFrame):
    """Run the real pipeline prefix and return the OB candidate tuple."""
    snapshot = snapshot_of(d1, h1)
    context = AnalysisContext.initial(snapshot)
    context = context.with_(htf=HTFStage.analyze(snapshot))
    context = StructureStage().run(context)
    return OrderBlockStage().run(context).order_blocks


# --------------------------------------------------------------------------
# V1 equivalence
# --------------------------------------------------------------------------

class TestOrderBlocksMatchV1(unittest.TestCase):
    """V1 publishes the full candidate set only on the paths that reach
    `debug["ob_audit"]` (entry-ready or WATCH_ONLY); `h1_setup["fresh_ob"]` is
    published on every non-early-exit path. Both are compared."""

    @classmethod
    def setUpClass(cls):
        strategy = SMCStrategy()
        cls.cases = []
        for name, d1, h1 in corpus():
            cls.cases.append((name, d1, h1, strategy.build_signal_context(d1, h1)))

    def test_candidate_set_matches_v1_field_for_field(self):
        compared = 0
        for name, d1, h1, v1 in self.cases:
            audit = (v1.get("debug") or {}).get("ob_audit")
            if not audit:
                continue
            compared += 1
            with self.subTest(frame=name):
                actual = [b.to_v1_dict() for b in v2_blocks(d1, h1)]
                self.assertEqual(actual, audit["ob_candidates"])
        self.assertGreater(compared, 5, "corpus must exercise the audit path")

    def test_selection_matches_v1_field_for_field(self):
        compared = 0
        for name, d1, h1, v1 in self.cases:
            audit = (v1.get("debug") or {}).get("ob_audit")
            if not audit:
                continue
            compared += 1
            with self.subTest(frame=name):
                blocks = v2_blocks(d1, h1)
                block = selected_block(blocks)
                if block is None:
                    self.assertIsNone(audit["ob_index"])
                    self.assertIsNone(audit["ob_top"])
                    self.assertIsNone(audit["ob_bottom"])
                    self.assertIsNone(audit["ob_size_atr"])
                else:
                    self.assertEqual(block.index, audit["ob_index"])
                    self.assertEqual(block.top, audit["ob_top"])
                    self.assertEqual(block.bottom, audit["ob_bottom"])
                    self.assertEqual(block.size_atr, audit["ob_size_atr"])
                self.assertEqual(fresh_ob(blocks), audit["fresh_ob"])
        self.assertGreater(compared, 5)

    def test_fresh_ob_matches_v1_h1_setup_across_corpus(self):
        compared = 0
        for name, d1, h1, v1 in self.cases:
            h1_setup = v1.get("h1_setup")
            if not h1_setup:
                continue
            compared += 1
            with self.subTest(frame=name):
                self.assertEqual(fresh_ob(v2_blocks(d1, h1)), h1_setup["fresh_ob"])
        self.assertGreater(compared, 20)

    def test_corpus_covers_both_directions(self):
        directions = {
            (c[3].get("h1_setup") or {}).get("direction") for c in self.cases
        }
        self.assertIn("LONG", directions)
        self.assertIn("SHORT", directions)

    def test_corpus_covers_selected_rejected_and_empty(self):
        selected, rejected, empty = False, False, False
        for _, d1, h1, _v1 in self.cases:
            blocks = v2_blocks(d1, h1) or ()
            selected = selected or selected_block(blocks) is not None
            rejected = rejected or any(not b.passed_quality_filter for b in blocks)
            empty = empty or not blocks
        self.assertTrue(selected, "no corpus frame selected an OB")
        self.assertTrue(rejected, "no corpus frame rejected a candidate")
        self.assertTrue(empty, "no corpus frame produced an empty candidate set")

    def test_matches_v1_on_the_fixture(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        v1 = SMCStrategy().build_signal_context(snapshot.d1, snapshot.h1)
        blocks = v2_blocks(snapshot.d1, snapshot.h1)
        self.assertEqual(fresh_ob(blocks), v1["h1_setup"]["fresh_ob"])
        self.assertEqual(fresh_ob(blocks), v1["debug"]["conf"]["ob"])

    def test_is_deterministic(self):
        _, d1, h1 = corpus()[0]
        self.assertEqual(v2_blocks(d1, h1), v2_blocks(d1, h1))

    def test_v1_thresholds_preserved(self):
        self.assertEqual(
            (OB_WINDOW_LOOKBACK, OB_MIN_SIZE_ATR_MULT,
             OB_NOT_LOST_ATR_MULT, OB_MIN_DISTANCE_ATR_MULT),
            (10, 0.25, 0.20, 0.15),
        )


# --------------------------------------------------------------------------
# Bullish order blocks (LONG — demand zones)
# --------------------------------------------------------------------------

class TestBullishOrderBlocks(unittest.TestCase):
    def test_down_candle_becomes_a_demand_zone(self):
        df = down_bar(build_frame(), 25, open_=100.0, low=97.0)
        blocks = detect_order_blocks("LONG", df, 98.0, 1.0)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual((block.index, block.top, block.bottom), (25, 100.0, 97.0))
        self.assertEqual(block.size, 3.0)
        self.assertEqual(block.size_atr, 3.0)
        self.assertEqual(block.rank_from_last, 1)

    def test_up_candles_are_not_candidates(self):
        df = up_bar(build_frame(), 25, open_=97.0, high=100.0)
        self.assertEqual(detect_order_blocks("LONG", df, 98.0, 1.0), ())

    def test_doji_is_not_a_candidate(self):
        # close == open is neither < nor >, so V1 registers nothing.
        self.assertEqual(detect_order_blocks("LONG", build_frame(), 100.0, 1.0), ())

    def test_ranks_newest_first(self):
        df = build_frame()
        down_bar(df, 22, open_=100.0, low=97.0)
        down_bar(df, 26, open_=100.0, low=97.0)
        blocks = detect_order_blocks("LONG", df, 98.0, 1.0)
        self.assertEqual([b.index for b in blocks], [26, 22])
        self.assertEqual([b.rank_from_last for b in blocks], [1, 2])

    def test_first_passing_candidate_is_selected(self):
        df = build_frame()
        down_bar(df, 22, open_=100.0, low=97.0)
        down_bar(df, 26, open_=100.0, low=97.0)
        blocks = detect_order_blocks("LONG", df, 98.0, 1.0)
        self.assertEqual([b.is_selected for b in blocks], [True, False])
        self.assertEqual(selected_block(blocks).index, 26)

    def test_selection_skips_candidates_that_fail_the_filter(self):
        df = build_frame()
        down_bar(df, 22, open_=100.0, low=97.0)          # passes
        down_bar(df, 26, open_=100.0, low=99.9)          # size 0.1 < 0.25*atr
        blocks = detect_order_blocks("LONG", df, 98.0, 1.0)
        self.assertEqual([b.passed_quality_filter for b in blocks], [False, True])
        self.assertEqual(selected_block(blocks).index, 22)

    def test_mitigated_zone_is_not_fresh(self):
        df = build_frame()
        down_bar(df, 22, open_=100.0, low=97.0)
        # A later *up* bar (so it is not itself a candidate) closing below the
        # zone bottom mitigates it.
        up_bar(df, 24, open_=95.0, high=96.5, close=96.0, low=95.0)
        self.assertFalse(block_at(detect_order_blocks("LONG", df, 98.0, 1.0), 22).fresh_status)

    def test_close_exactly_at_bottom_stays_fresh(self):
        # V1 tests strict `<`, so a close equal to the bottom does not mitigate.
        df = build_frame()
        down_bar(df, 22, open_=100.0, low=97.0)
        up_bar(df, 24, open_=96.0, high=97.5, close=97.0, low=95.5)
        self.assertTrue(block_at(detect_order_blocks("LONG", df, 98.0, 1.0), 22).fresh_status)

    def test_stale_block_still_wins_the_slot(self):
        # V1 selects on the quality filter alone; freshness is read afterwards,
        # so a stale block takes the slot ahead of an older but fresh one.
        df = build_frame()
        down_bar(df, 22, open_=100.0, low=90.0)          # deep zone, stays fresh
        down_bar(df, 26, open_=100.0, low=97.0)          # shallow zone, mitigated
        up_bar(df, 27, open_=95.0, high=96.5, close=96.0, low=95.0)
        blocks = detect_order_blocks("LONG", df, 98.0, 1.0)
        self.assertTrue(block_at(blocks, 22).fresh_status)
        self.assertFalse(block_at(blocks, 26).fresh_status)
        self.assertEqual(selected_block(blocks).index, 26)
        self.assertFalse(fresh_ob(blocks))

    def test_price_deeply_below_zone_is_lost(self):
        # current_price < bottom - 0.20*atr
        df = down_bar(build_frame(), 25, open_=100.0, low=97.0)
        self.assertFalse(
            detect_order_blocks("LONG", df, 96.5, 1.0)[0].passed_quality_filter
        )
        self.assertTrue(
            detect_order_blocks("LONG", df, 96.85, 1.0)[0].passed_quality_filter
        )

    def test_zone_too_close_to_price_is_rejected(self):
        # |price - mid| < 0.15*atr, with mid = 98.5
        df = down_bar(build_frame(), 25, open_=100.0, low=97.0)
        self.assertFalse(
            detect_order_blocks("LONG", df, 98.4, 1.0)[0].passed_quality_filter
        )
        self.assertTrue(
            detect_order_blocks("LONG", df, 98.35, 1.0)[0].passed_quality_filter
        )

    def test_metadata_is_atr_relative(self):
        df = down_bar(build_frame(), 25, open_=100.0, low=97.0)
        block = detect_order_blocks("LONG", df, 94.0, 2.0)[0]
        self.assertEqual(block.size_atr, 1.5)               # 3.0 / 2.0
        self.assertEqual(block.depth_from_price, 2.25)      # |94 - 98.5| / 2.0


# --------------------------------------------------------------------------
# Bearish order blocks (SHORT — supply zones)
# --------------------------------------------------------------------------

class TestBearishOrderBlocks(unittest.TestCase):
    def test_up_candle_becomes_a_supply_zone(self):
        df = up_bar(build_frame(), 25, open_=97.0, high=100.0)
        blocks = detect_order_blocks("SHORT", df, 99.0, 1.0)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual((block.index, block.top, block.bottom), (25, 100.0, 97.0))
        self.assertEqual(block.size, 3.0)

    def test_down_candles_are_not_candidates(self):
        df = down_bar(build_frame(), 25, open_=100.0, low=97.0)
        self.assertEqual(detect_order_blocks("SHORT", df, 98.0, 1.0), ())

    def test_mitigated_zone_is_not_fresh(self):
        df = build_frame()
        up_bar(df, 22, open_=97.0, high=100.0)
        # A later *down* bar (so it is not itself a candidate) closing above the
        # zone top mitigates it.
        down_bar(df, 24, open_=102.0, low=100.5, close=101.0, high=102.5)
        self.assertFalse(block_at(detect_order_blocks("SHORT", df, 99.0, 1.0), 22).fresh_status)

    def test_close_exactly_at_top_stays_fresh(self):
        df = build_frame()
        up_bar(df, 22, open_=97.0, high=100.0)
        down_bar(df, 24, open_=101.0, low=99.5, close=100.0, high=101.5)
        self.assertTrue(block_at(detect_order_blocks("SHORT", df, 99.0, 1.0), 22).fresh_status)

    def test_price_deeply_above_zone_is_lost(self):
        # current_price > top + 0.20*atr
        df = up_bar(build_frame(), 25, open_=97.0, high=100.0)
        self.assertFalse(
            detect_order_blocks("SHORT", df, 100.5, 1.0)[0].passed_quality_filter
        )
        self.assertTrue(
            detect_order_blocks("SHORT", df, 100.15, 1.0)[0].passed_quality_filter
        )

    def test_zone_too_close_to_price_is_rejected(self):
        df = up_bar(build_frame(), 25, open_=97.0, high=100.0)
        self.assertFalse(
            detect_order_blocks("SHORT", df, 98.6, 1.0)[0].passed_quality_filter
        )
        self.assertTrue(
            detect_order_blocks("SHORT", df, 98.65, 1.0)[0].passed_quality_filter
        )

    def test_first_passing_candidate_is_selected(self):
        df = build_frame()
        up_bar(df, 22, open_=97.0, high=100.0)
        up_bar(df, 26, open_=97.0, high=100.0)
        blocks = detect_order_blocks("SHORT", df, 99.0, 1.0)
        self.assertEqual([b.index for b in blocks], [26, 22])
        self.assertEqual(selected_block(blocks).index, 26)


# --------------------------------------------------------------------------
# The scan window
# --------------------------------------------------------------------------

class TestCandidateWindow(unittest.TestCase):
    def test_bounds_match_v1(self):
        self.assertEqual(candidate_window(29), (19, 28))
        self.assertEqual(candidate_window(11), (1, 10))

    def test_never_reaches_index_zero(self):
        self.assertEqual(candidate_window(5), (1, 4))
        self.assertEqual(candidate_window(2), (1, 1))

    def test_last_bar_is_excluded(self):
        df = down_bar(build_frame(), 29, open_=100.0, low=97.0)
        self.assertEqual(detect_order_blocks("LONG", df, 98.0, 1.0), ())

    def test_bar_older_than_the_window_is_excluded(self):
        df = down_bar(build_frame(), 18, open_=100.0, low=97.0)   # window starts at 19
        self.assertEqual(detect_order_blocks("LONG", df, 98.0, 1.0), ())
        df = down_bar(build_frame(), 19, open_=100.0, low=97.0)
        self.assertEqual(len(detect_order_blocks("LONG", df, 98.0, 1.0)), 1)

    def test_window_holds_at_most_ten_bars(self):
        df = build_frame()
        for i in range(15, 29):
            down_bar(df, i, open_=100.0, low=97.0)
        blocks = detect_order_blocks("LONG", df, 98.0, 1.0)
        self.assertEqual(len(blocks), 10)
        self.assertEqual([b.index for b in blocks], list(range(28, 18, -1)))

    def test_last_close_is_the_closed_bar(self):
        df = set_bar(build_frame(), 29, close=123.0)
        self.assertEqual(last_close(df), 123.0)


# --------------------------------------------------------------------------
# Quality filter
# --------------------------------------------------------------------------

class TestQualityFilter(unittest.TestCase):
    def test_size_floor(self):
        # size 0.20 < 0.25*atr rejects; 0.25 exactly passes (V1 uses `>=`).
        self.assertFalse(passes_quality_filter("LONG", 100.0, 99.8, 99.6, 1.0))
        self.assertTrue(passes_quality_filter("LONG", 100.0, 99.75, 99.6, 1.0))

    def test_non_positive_atr_rejects_everything(self):
        for atr in (0.0, -1.0):
            with self.subTest(atr=atr):
                self.assertFalse(passes_quality_filter("LONG", 100.0, 97.0, 98.0, atr))

    def test_nan_atr_rejects(self):
        self.assertFalse(
            passes_quality_filter("LONG", 100.0, 97.0, 98.0, float("nan"))
        )

    def test_any_non_long_direction_uses_the_supply_test(self):
        # V1's `else` branch is "not LONG", not "SHORT".
        for direction in ("SHORT", None, "", "NONE", "long"):
            with self.subTest(direction=direction):
                self.assertEqual(
                    passes_quality_filter(direction, 100.0, 97.0, 100.5, 1.0),
                    passes_quality_filter("SHORT", 100.0, 97.0, 100.5, 1.0),
                )

    def test_long_and_short_disagree_on_the_same_zone(self):
        # Price far above the zone: lost for a SHORT, still valid for a LONG.
        self.assertTrue(passes_quality_filter("LONG", 100.0, 97.0, 105.0, 1.0))
        self.assertFalse(passes_quality_filter("SHORT", 100.0, 97.0, 105.0, 1.0))


# --------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------

class TestDegenerateInputs(unittest.TestCase):
    def test_none_frame(self):
        self.assertEqual(detect_order_blocks("LONG", None, 100.0, 1.0), ())

    def test_empty_frame(self):
        self.assertEqual(detect_order_blocks("LONG", build_frame(n=0), 100.0, 1.0), ())

    def test_single_bar_frame(self):
        self.assertEqual(detect_order_blocks("LONG", build_frame(n=1), 100.0, 1.0), ())

    def test_two_bar_frame_scans_nothing(self):
        df = down_bar(build_frame(n=2), 0, open_=100.0, low=97.0)
        self.assertEqual(detect_order_blocks("LONG", df, 98.0, 1.0), ())

    def test_non_long_direction_takes_the_supply_scan(self):
        df = build_frame()
        up_bar(df, 25, open_=97.0, high=100.0)
        for direction in (None, "", "NONE", "long"):
            with self.subTest(direction=direction):
                blocks = detect_order_blocks(direction, df, 99.0, 1.0)
                self.assertEqual(len(blocks), 1)
                self.assertEqual(blocks[0].top, 100.0)

    def test_zero_atr_registers_candidates_without_metadata(self):
        df = down_bar(build_frame(), 25, open_=100.0, low=97.0)
        block = detect_order_blocks("LONG", df, 98.0, 0.0)[0]
        self.assertIsNone(block.size_atr)
        self.assertIsNone(block.depth_from_price)
        self.assertFalse(block.passed_quality_filter)
        self.assertFalse(block.is_selected)
        self.assertEqual(block.size, 3.0)

    def test_zero_size_zone(self):
        # open == low on a down candle: a zero-height zone, still registered.
        df = set_bar(build_frame(), 25, open_=100.0, low=100.0, close=99.0, high=100.5)
        block = detect_order_blocks("LONG", df, 98.0, 1.0)[0]
        self.assertEqual(block.size, 0.0)
        self.assertFalse(block.passed_quality_filter)

    def test_selection_helpers_tolerate_empty_input(self):
        for blocks in (None, ()):
            with self.subTest(blocks=blocks):
                self.assertIsNone(selected_block(blocks))
                self.assertFalse(fresh_ob(blocks))


# --------------------------------------------------------------------------
# Stage behavior
# --------------------------------------------------------------------------

class TestOrderBlockStage(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        base = AnalysisContext.initial(self.snapshot)
        self.context = StructureStage().run(
            base.with_(htf=HTFStage.analyze(self.snapshot))
        )

    def test_populates_order_blocks(self):
        result = OrderBlockStage().run(self.context)
        self.assertIsInstance(result.order_blocks, tuple)
        for block in result.order_blocks:
            self.assertIsInstance(block, OrderBlock)

    def test_matches_v1_fresh_ob(self):
        result = OrderBlockStage().run(self.context)
        v1 = SMCStrategy().build_signal_context(self.snapshot.d1, self.snapshot.h1)
        self.assertEqual(fresh_ob(result.order_blocks), v1["h1_setup"]["fresh_ob"])

    def test_uses_the_last_closed_close_not_live_price(self):
        h1 = safe_frame(self.snapshot.h1, H1_MIN_ROWS)
        expected = detect_order_blocks(
            HTFStage.analyze(self.snapshot).direction,
            h1,
            last_close(h1),
            self.context.atr_last,
        )
        self.assertEqual(OrderBlockStage().run(self.context).order_blocks, expected)

    def test_populates_only_order_blocks(self):
        result = OrderBlockStage().run(self.context)
        for field in ("fvg", "entry", "risk", "candidate", "signal"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(result, field))

    def test_no_blocks_without_htf(self):
        bare = AnalysisContext.initial(self.snapshot)
        self.assertIsNone(OrderBlockStage().run(bare).order_blocks)

    def test_no_blocks_without_structure(self):
        no_structure = AnalysisContext.initial(self.snapshot).with_(
            htf=HTFStage.analyze(self.snapshot)
        )
        self.assertIsNone(OrderBlockStage().run(no_structure).order_blocks)

    def test_no_blocks_when_h1_unusable(self):
        thin = (
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
            .with_(htf=htf_context(), atr_last=1.0)
        )
        self.assertIsNone(OrderBlockStage().run(thin).order_blocks)

    def test_does_not_mutate_input_context(self):
        OrderBlockStage().run(self.context)
        self.assertIsNone(self.context.order_blocks)

    def test_is_deterministic(self):
        self.assertEqual(
            OrderBlockStage().run(self.context), OrderBlockStage().run(self.context)
        )

    def test_stage_name(self):
        self.assertEqual(OrderBlockStage().name, "order_block")


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


class TestModuleBoundary(unittest.TestCase):
    def test_order_block_owns_no_later_stage_behavior(self):
        import v2.analysis.order_block as ob_module

        for name in (
            "detect_fvg", "fair_value_gap", "fvg_valid",
            "entry_price", "build_entry", "find_liquidity_target",
            "score_signal", "build_signal",
        ):
            with self.subTest(symbol=name):
                self.assertFalse(hasattr(ob_module, name))

    def test_order_block_detection_has_a_single_owner(self):
        import v2.analysis.htf as htf_module
        import v2.analysis.liquidity as liquidity_module
        import v2.analysis.regime as regime_module
        import v2.analysis.structure as structure_module

        for module in (htf_module, regime_module, structure_module, liquidity_module):
            for name in ("detect_order_blocks", "passes_quality_filter",
                         "selected_block", "fresh_ob"):
                with self.subTest(module=module.__name__, symbol=name):
                    self.assertFalse(hasattr(module, name))


# --------------------------------------------------------------------------
# Pipeline integration
# --------------------------------------------------------------------------

class TestPipelineIntegration(unittest.TestCase):
    def stages(self):
        return [HTFStage(), RegimeStage(), StructureStage(), LiquidityStage(),
                OrderBlockStage()]

    def test_first_five_stages_run_in_order(self):
        snapshot = InputSnapshot.load(FIXTURE).to_market_snapshot()
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot)
        )
        self.assertIsNotNone(result.htf)
        self.assertIsNotNone(result.regime)
        self.assertIsNotNone(result.swing_highs)
        self.assertIsNotNone(result.sweep)
        self.assertIsInstance(result.order_blocks, tuple)
        self.assertIsNone(result.fvg)
        self.assertIsNone(result.signal)

    def test_order_block_skipped_when_htf_bails(self):
        result = AnalysisPipeline(self.stages()).run(
            AnalysisContext.initial(snapshot_with(build_frame(n=5)))
        )
        self.assertIsNone(result.htf)
        self.assertIsNone(result.order_blocks)


if __name__ == "__main__":
    unittest.main()
