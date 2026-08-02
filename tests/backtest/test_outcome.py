"""Tests for the Outcome Engine (Milestone M8).

Every scenario is built from **explicit OHLC candles**, so no assertion depends on
judgement: the fixture states exactly which levels each candle reaches, and the
expected state follows from one clause of §4. Each test names the clause it pins.

Hermetic: a synthetic repository in a temporary directory, no network, no clock.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from backtest.data.repository import KLINE_DTYPE, dataset_path
from backtest.lineage import LineageError, RunLineage, read_run_lineage
from backtest.outcome import (
    Cancellation, EXIT_REASONS, FILL_KINDS, OUTCOMES_FILENAME, OutcomeConfig,
    OutcomeConfigError, OutcomeDataError, OutcomeEngine, OutcomeError,
    OutcomeRecord, STATE_ACTIVE, STATE_EXPIRED, STATE_INVALIDATED_EXIT,
    STATE_INVALID_LEVELS, STATE_LOSS, STATE_NOT_FILLED, STATE_PENDING,
    STATE_WIN, TERMINAL_STATES, TradeIntent, TradeIntentError,
)
from tests.backtest.test_historical_adapter import write_repository

HOUR_MS = 3_600_000
M5_MS = 300_000

#: 2026-03-01T00:00:00Z — the instantiation boundary `T` for every trade below.
T = 1_772_323_200
T_MS = T * 1000
C_REF = T - 3600                       # open time of the reference candle

#: The frozen levels used throughout: a LONG risking 5 to make 10 (RR 2.0).
ENTRY, SL, TP = 100.0, 95.0, 110.0


def candle(open_ms: int, interval_ms: int, o: float, h: float, l: float,
           c: float, *, qv: float = 1000.0) -> tuple:
    return (open_ms, o, h, l, c, 10.0, open_ms + interval_ms - 1, qv, 100,
            5.0, 500.0, 0.0)


def h1(index: int, o: float, h: float, l: float, c: float) -> tuple:
    """The walk candle `W{index}`; `index = -1` is the reference candle."""
    return candle(T_MS + index * HOUR_MS, HOUR_MS, o, h, l, c)


def m5_of(h1_index: int, ranges) -> list[tuple]:
    """Twelve M5 candles covering `W{h1_index}` — the only complete set (C-4.3).

    `ranges` is 12 `(open, high, low, close)` tuples in ascending time order.
    """
    base = T_MS + h1_index * HOUR_MS
    assert len(ranges) == 12, "a complete M5 set is exactly 12 candles"
    return [candle(base + offset * M5_MS, M5_MS, *values)
            for offset, values in enumerate(ranges)]


def flat_m5(h1_index: int, o: float, h: float, l: float, c: float) -> list[tuple]:
    return m5_of(h1_index, [(o, h, l, c)] * 12)


class OutcomeCase(unittest.TestCase):
    """A repository built from explicit candles, and an engine over it."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def engine(self, h1_rows, m5_rows=(), *, config=None,
               name: str = "repo") -> OutcomeEngine:
        datasets = {("BTCUSDT", "1h"): list(h1_rows)}
        if m5_rows:
            datasets[("BTCUSDT", "5m")] = list(m5_rows)
        repo = write_repository(self.tmp / name, datasets)
        return OutcomeEngine(repo, config=config)

    def intent(self, **kw) -> TradeIntent:
        params = dict(
            run_id="replay-test", source="replay", symbol="BTC/USDT",
            direction="LONG", entry_mode="RETRACEMENT", boundary=T,
            h1_last_ts=C_REF, cycle_index=0, universe_rank=1,
            alert_tier="VALID", entry_price=ENTRY, sl=SL, tp=TP, score=12,
            signal_id="BTC/USDT|RETRACEMENT|1772319600.0",
        )
        params.update(kw)
        return TradeIntent(**params)

    def resolve(self, h1_rows, m5_rows=(), *, config=None,
                name: str = "repo", **intent_kw) -> OutcomeRecord:
        engine = self.engine(h1_rows, m5_rows, config=config, name=name)
        return engine.resolve(self.intent(**intent_kw))


#: A reference candle closing above the limit, so the limit is NOT pre-crossed
#: (C-4.1: LONG is pre-crossed when `entry_price >= close(C_ref)`).
REF = h1(-1, 104.0, 106.0, 103.0, 105.0)

#: `W0` reaching down to 99: the limit at 100 fills on a touch.
W0_TOUCH = h1(0, 104.0, 105.0, 99.0, 103.0)


# ==========================================================================
# TASK 2 — the state vocabulary is closed
# ==========================================================================

class TestStateVocabulary(unittest.TestCase):

    def test_only_the_contract_states_exist(self):
        from backtest.outcome import trade
        states = {value for name, value in vars(trade).items()
                  if name.startswith("STATE_")}
        self.assertEqual(states, {
            "PENDING", "ACTIVE", "WIN", "LOSS", "NOT_FILLED",
            "INVALIDATED_EXIT", "EXPIRED", "INVALID_LEVELS"})

    def test_open_and_breakeven_are_not_states(self):
        """C-3.1: `OPEN` is a reporting label and `BREAKEVEN` a derived bucket."""
        from backtest.outcome import trade
        source = Path("backtest/outcome/trade.py").read_text(encoding="utf-8")
        self.assertNotIn('STATE_OPEN', source)
        self.assertNotIn('STATE_BREAKEVEN', source)
        self.assertNotIn('STATE_PARTIAL', source)
        self.assertFalse(hasattr(trade, "STATE_OPEN"))

    def test_terminal_states_are_exactly_the_six(self):
        self.assertEqual(TERMINAL_STATES, {
            "WIN", "LOSS", "NOT_FILLED", "INVALIDATED_EXIT", "EXPIRED",
            "INVALID_LEVELS"})
        self.assertNotIn(STATE_PENDING, TERMINAL_STATES)
        self.assertNotIn(STATE_ACTIVE, TERMINAL_STATES)

    def test_the_exit_reason_vocabulary_is_fixed(self):
        self.assertEqual(EXIT_REASONS, {
            "TP", "SL", "GAP_TP", "GAP_SL", "SL_WORST_CASE_M5",
            "SL_WORST_CASE_NO_M5", "HTF_KILL", "MAX_HOLDING", "END_OF_DATA"})

    def test_the_fill_kind_vocabulary_is_fixed(self):
        self.assertEqual(FILL_KINDS, {
            "LIMIT_TOUCH", "LIMIT_GAP", "IMMEDIATE", "MARKET_WALK_ORIGIN"})

    def test_an_unknown_cancel_reason_is_refused(self):
        with self.assertRaises(TradeIntentError):
            Cancellation(boundary=T + 3600, reason="BECAUSE")

    def test_an_entry_mode_without_a_fill_rule_is_refused(self):
        with self.assertRaises(TradeIntentError):
            TradeIntent(run_id="r", source="replay", symbol="BTC/USDT",
                        direction="LONG", entry_mode="WATCH_ONLY",
                        boundary=T, h1_last_ts=C_REF)

    def test_h1_last_ts_must_precede_the_boundary(self):
        """C-5.3: it is the OPEN time of `C_ref`, never `T` itself."""
        with self.assertRaises(TradeIntentError):
            TradeIntent(run_id="r", source="replay", symbol="BTC/USDT",
                        direction="LONG", entry_mode="RETRACEMENT",
                        boundary=T, h1_last_ts=T)


# ==========================================================================
# TASK 3 / TASK 5 — fills and terminal outcomes
# ==========================================================================

class TestWinAndLoss(OutcomeCase):

    def test_a_touch_fill_then_target_is_a_win(self):
        """C-4.1 touch fill, C-4.3 single hit, C-9.6 R arithmetic."""
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        self.assertEqual(record.state, STATE_WIN)
        self.assertEqual(record.exit_reason, "TP")
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_kind, "LIMIT_TOUCH")
        self.assertEqual(record.fill_price, ENTRY)
        self.assertEqual(record.fill_T, T)
        self.assertEqual(record.candles_to_fill, 0)
        self.assertEqual(record.exit_price, TP)
        self.assertEqual(record.exit_T, T + 3600)
        self.assertEqual(record.holding_candles, 2)
        self.assertEqual(record.realized_r, 2.0)
        self.assertEqual(record.structural_rr, 2.0)
        self.assertTrue(record.is_terminal)
        self.assertFalse(record.is_open)

    def test_a_touch_fill_then_stop_is_a_loss(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 104.0, 94.0, 94.5)])
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL")
        self.assertEqual(record.exit_price, SL)
        self.assertEqual(record.realized_r, -1.0)
        self.assertEqual(record.holding_candles, 2)

    def test_the_fill_candle_itself_can_reach_the_target(self):
        """C-4.4: exits are evaluated from the fill onward, on the fill candle."""
        record = self.resolve([REF, h1(0, 104.0, 111.0, 99.0, 110.0)])
        self.assertEqual(record.state, STATE_WIN)
        self.assertEqual(record.holding_candles, 1)

    def test_realized_r_uses_the_fill_price_not_the_structural_entry(self):
        """C-9.6: `R_unit = |fill_price - sl|`. A gap fill genuinely risks less."""
        record = self.resolve([REF, h1(0, 98.0, 99.0, 97.0, 98.5),
                               h1(1, 98.5, 111.0, 98.0, 110.5)])
        self.assertEqual(record.fill_kind, "LIMIT_GAP")
        self.assertEqual(record.fill_price, 98.0)
        self.assertEqual(record.realized_r, (TP - 98.0) / (98.0 - SL))
        self.assertEqual(record.structural_rr, 2.0)     # unchanged, frozen
        self.assertNotEqual(record.realized_r, record.structural_rr)

    def test_a_short_resolves_mirrored(self):
        ref = h1(-1, 96.0, 97.0, 94.0, 95.0)            # below the limit at 100
        record = self.resolve(
            [ref, h1(0, 96.0, 101.0, 95.0, 99.0), h1(1, 99.0, 100.0, 89.0, 90.0)],
            direction="SHORT", entry_price=100.0, sl=105.0, tp=90.0)
        self.assertEqual(record.state, STATE_WIN)
        self.assertEqual(record.fill_kind, "LIMIT_TOUCH")
        self.assertEqual(record.fill_price, 100.0)
        self.assertEqual(record.exit_price, 90.0)
        self.assertEqual(record.realized_r, 2.0)


class TestFillModel(OutcomeCase):

    def test_a_pre_crossed_limit_fills_immediately_at_the_origin_open(self):
        """C-4.1: an order already at or through market fills at once; modelling
        patience would fabricate a favourable entry."""
        ref = h1(-1, 99.0, 100.0, 98.0, 99.0)           # closes BELOW the limit
        record = self.resolve([ref, h1(0, 99.5, 111.0, 99.0, 110.0)])
        self.assertEqual(record.fill_kind, "IMMEDIATE")
        self.assertEqual(record.fill_price, 99.5)
        self.assertEqual(record.fill_T, T)

    def test_a_continuation_fills_at_the_walk_origin_open(self):
        """C-4.2: `open(W0)`, never `open(W1)` and never the pullback close."""
        record = self.resolve([REF, h1(0, 104.0, 111.0, 103.0, 110.0)],
                              entry_mode="CONTINUATION")
        self.assertEqual(record.fill_kind, "MARKET_WALK_ORIGIN")
        self.assertEqual(record.fill_price, 104.0)
        self.assertEqual(record.fill_T, T)
        self.assertEqual(record.candles_to_fill, 0)

    def test_the_pessimistic_continuation_model_fills_at_the_origin_close(self):
        record = self.resolve([REF, h1(0, 104.0, 111.0, 103.0, 110.0)],
                              config=OutcomeConfig(continuation_fill="w0_close"),
                              entry_mode="CONTINUATION")
        self.assertEqual(record.fill_price, 110.0)

    def test_a_continuation_ignores_the_pre_crossed_rule(self):
        """It is a market order: the reference close is irrelevant to it."""
        ref = h1(-1, 99.0, 100.0, 98.0, 99.0)
        record = self.resolve([ref, h1(0, 104.0, 111.0, 103.0, 110.0)],
                              entry_mode="CONTINUATION")
        self.assertEqual(record.fill_kind, "MARKET_WALK_ORIGIN")

    def test_a_limit_that_is_never_touched_never_fills(self):
        record = self.resolve([REF, h1(0, 104.0, 106.0, 101.0, 105.0),
                               h1(1, 105.0, 107.0, 102.0, 106.0)])
        self.assertFalse(record.filled)
        self.assertEqual(record.state, STATE_PENDING)
        self.assertEqual(record.exit_reason, "END_OF_DATA")

    def test_a_later_candle_can_fill_the_limit(self):
        record = self.resolve([REF, h1(0, 104.0, 106.0, 101.0, 105.0),
                               h1(1, 105.0, 106.0, 99.0, 104.0),
                               h1(2, 104.0, 111.0, 103.0, 110.5)])
        self.assertEqual(record.fill_T, T + 3600)
        self.assertEqual(record.candles_to_fill, 1)
        self.assertEqual(record.state, STATE_WIN)


class TestGaps(OutcomeCase):

    def test_a_gap_through_the_entry_fills_at_the_better_open(self):
        """C-4.1: price-or-better on a gap."""
        record = self.resolve([REF, h1(0, 97.0, 98.0, 96.5, 97.5),
                               h1(1, 97.5, 111.0, 97.0, 110.5)])
        self.assertEqual(record.fill_kind, "LIMIT_GAP")
        self.assertEqual(record.fill_price, 97.0)

    def test_a_gap_through_the_stop_exits_at_the_open_and_can_be_worse_than_minus_one(self):
        """C-4.5: the exit price is the open, not the stop."""
        record = self.resolve([REF, W0_TOUCH, h1(1, 90.0, 91.0, 89.0, 90.5)])
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "GAP_SL")
        self.assertEqual(record.exit_price, 90.0)
        self.assertEqual(record.realized_r, (90.0 - ENTRY) / (ENTRY - SL))
        self.assertLess(record.realized_r, -1.0)

    def test_a_gap_through_the_target_exits_at_the_better_open(self):
        record = self.resolve([REF, W0_TOUCH, h1(1, 115.0, 116.0, 114.0, 115.5)])
        self.assertEqual(record.state, STATE_WIN)
        self.assertEqual(record.exit_reason, "GAP_TP")
        self.assertEqual(record.exit_price, 115.0)
        self.assertGreater(record.realized_r, 2.0)


class TestSameCandleResolution(OutcomeCase):
    """C-4.3 rules 2–4: both levels touched inside one H1 candle."""

    BOTH = h1(1, 103.0, 111.0, 94.0, 100.0)     # reaches TP=110 and SL=95

    def test_m5_orders_the_events_and_the_target_can_win(self):
        # Both walk candles get a complete M5 set, so the excursion is measured
        # entirely from M5 extremes and is not degraded (C-9.6).
        m5 = flat_m5(0, 104.0, 105.0, 99.0, 103.0) + \
             m5_of(1, [(103.0, 111.0, 102.0, 110.0)]          # TP in the 1st M5
                   + [(110.0, 111.0, 94.0, 95.0)] * 11)       # SL later
        record = self.resolve([REF, W0_TOUCH, self.BOTH], m5)
        self.assertEqual(record.state, STATE_WIN)
        self.assertEqual(record.exit_reason, "TP")
        self.assertFalse(record.excursion_degraded)

    def test_m5_orders_the_events_and_the_stop_can_win(self):
        m5 = m5_of(1, [(103.0, 104.0, 94.0, 95.0)]            # SL in the 1st M5
                   + [(95.0, 111.0, 94.0, 110.0)] * 11)
        record = self.resolve([REF, W0_TOUCH, self.BOTH], m5)
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL")

    def test_both_inside_one_m5_candle_is_the_worst_case(self):
        """C-4.3 rule 3: SL first."""
        m5 = m5_of(1, [(103.0, 111.0, 94.0, 100.0)] * 12)
        record = self.resolve([REF, W0_TOUCH, self.BOTH], m5)
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL_WORST_CASE_M5")
        self.assertEqual(record.realized_r, -1.0)

    def test_a_missing_m5_set_is_the_worst_case_and_is_counted(self):
        """C-4.3 rule 4: do not guess."""
        engine = self.engine([REF, W0_TOUCH, self.BOTH])
        report = engine.resolve_all([self.intent()])
        record = report.records[0]
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL_WORST_CASE_NO_M5")
        self.assertEqual(report.counters.m5_gap_resolutions, 1)
        self.assertTrue(record.excursion_degraded)

    def test_an_incomplete_m5_set_is_treated_as_missing(self):
        """C-4.3: complete means exactly 12 candles; any other count, including
        11, is incomplete."""
        eleven = m5_of(1, [(103.0, 111.0, 94.0, 100.0)] * 12)[:11]
        engine = self.engine([REF, W0_TOUCH, self.BOTH], eleven)
        report = engine.resolve_all([self.intent()])
        self.assertEqual(report.records[0].exit_reason, "SL_WORST_CASE_NO_M5")
        self.assertEqual(report.counters.m5_gap_resolutions, 1)

    def test_m5_that_contradicts_its_own_h1_candle_is_the_worst_case(self):
        """The timeframes disagree; the engine refuses to guess and counts it."""
        m5 = flat_m5(1, 103.0, 104.0, 102.0, 103.0)      # reaches neither level
        engine = self.engine([REF, W0_TOUCH, self.BOTH], m5)
        report = engine.resolve_all([self.intent()])
        self.assertEqual(report.records[0].exit_reason, "SL_WORST_CASE_NO_M5")
        self.assertEqual(report.counters.m5_inconsistent, 1)

    def test_a_limit_fill_candle_that_could_have_stopped_out_is_a_loss(self):
        """C-4.4: worst case unless M5 proves the target came first after the
        fill."""
        both_on_fill = h1(0, 104.0, 111.0, 94.0, 100.0)
        engine = self.engine([REF, both_on_fill])
        report = engine.resolve_all([self.intent()])
        record = report.records[0]
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL_WORST_CASE_NO_M5")
        self.assertEqual(record.fill_kind, "LIMIT_TOUCH")

    def test_m5_can_prove_the_target_came_first_after_a_limit_fill(self):
        both_on_fill = h1(0, 104.0, 111.0, 94.0, 100.0)
        m5 = m5_of(0, [(104.0, 105.0, 103.0, 104.0)]          # no fill yet
                   + [(104.0, 105.0, 99.0, 100.0)]            # fill at 100
                   + [(100.0, 111.0, 100.0, 110.0)]           # TP after the fill
                   + [(110.0, 111.0, 94.0, 95.0)] * 9)        # SL later still
        record = self.resolve([REF, both_on_fill], m5)
        self.assertEqual(record.state, STATE_WIN)
        self.assertEqual(record.exit_reason, "TP")

    def test_the_stop_wins_inside_the_fills_own_m5_candle(self):
        """The fill and the stop cannot be ordered within one M5 candle."""
        both_on_fill = h1(0, 104.0, 111.0, 94.0, 100.0)
        m5 = m5_of(0, [(104.0, 105.0, 103.0, 104.0)]
                   + [(104.0, 111.0, 94.0, 100.0)]            # fill AND both levels
                   + [(100.0, 101.0, 99.0, 100.0)] * 10)
        record = self.resolve([REF, both_on_fill], m5)
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL_WORST_CASE_M5")


# ==========================================================================
# TASK 4 — trade lifetime
# ==========================================================================

class TestNotFilled(OutcomeCase):

    NEVER_TOUCHED = [REF, h1(0, 104.0, 106.0, 101.0, 105.0),
                     h1(1, 105.0, 107.0, 102.0, 106.0),
                     h1(2, 106.0, 108.0, 103.0, 107.0),
                     h1(3, 107.0, 109.0, 104.0, 108.0)]

    def test_every_cancel_reason_produces_not_filled(self):
        """C-4.8: the lifecycle IS the order-lifetime model."""
        for reason in ("KILL", "DECAY", "TTL", "REPLACED", "SUPERSEDED"):
            record = self.resolve(
                self.NEVER_TOUCHED, name=f"repo-{reason}",
                cancellation=Cancellation(boundary=T + 2 * 3600, reason=reason))
            self.assertEqual(record.state, STATE_NOT_FILLED, reason)
            self.assertEqual(record.cancel_reason, reason)
            self.assertFalse(record.filled)
            self.assertIsNone(record.realized_r)
            self.assertEqual(record.exit_T, T + 2 * 3600)

    def test_a_pending_replacement_cancels_the_order(self):
        record = self.resolve(
            self.NEVER_TOUCHED,
            cancellation=Cancellation(boundary=T + 3600, reason="REPLACED"))
        self.assertEqual((record.state, record.cancel_reason),
                         (STATE_NOT_FILLED, "REPLACED"))

    def test_a_pending_supersession_cancels_the_order(self):
        record = self.resolve(
            self.NEVER_TOUCHED,
            cancellation=Cancellation(boundary=T + 3600, reason="SUPERSEDED"))
        self.assertEqual((record.state, record.cancel_reason),
                         (STATE_NOT_FILLED, "SUPERSEDED"))

    def test_a_fill_beats_a_cancellation_produced_by_the_same_candles_scan(self):
        """C-4.13: intra-candle price events precede the close-scan events."""
        record = self.resolve(
            [REF, W0_TOUCH, h1(1, 103.0, 111.0, 102.0, 110.5)],
            cancellation=Cancellation(boundary=T + 3600, reason="DECAY"))
        self.assertEqual(record.state, STATE_WIN)
        self.assertIsNone(record.cancel_reason)

    def test_a_cancellation_at_the_instantiation_boundary_precedes_every_candle(self):
        record = self.resolve(
            [REF, W0_TOUCH], cancellation=Cancellation(boundary=T,
                                                       reason="REPLACED"))
        self.assertEqual(record.state, STATE_NOT_FILLED)
        self.assertIsNone(record.candles_to_fill)

    def test_a_fixed_window_shadow_expires_on_the_window(self):
        """C-2.2: `CAPPED` and `KILL_COOLDOWN` have no memory record to evict."""
        record = self.resolve(self.NEVER_TOUCHED, shadow_kind="CAPPED",
                              alert_tier=None)
        self.assertEqual(record.state, STATE_NOT_FILLED)
        self.assertEqual(record.cancel_reason, "SHADOW_WINDOW")
        self.assertEqual(record.exit_T, T + 3 * 3600)
        self.assertEqual(record.shadow_kind, "CAPPED")

    def test_a_lifecycle_event_cannot_cancel_a_fixed_window_shadow(self):
        record = self.resolve(
            self.NEVER_TOUCHED, shadow_kind="KILL_COOLDOWN", alert_tier=None,
            cancellation=Cancellation(boundary=T + 3600, reason="KILL"))
        self.assertEqual(record.cancel_reason, "SHADOW_WINDOW")
        self.assertEqual(record.exit_T, T + 3 * 3600)

    def test_an_unalerted_insert_shadow_uses_the_real_lifecycle(self):
        record = self.resolve(
            self.NEVER_TOUCHED, shadow_kind="UNALERTED_INSERT", alert_tier=None,
            cancellation=Cancellation(boundary=T + 2 * 3600, reason="DECAY"))
        self.assertEqual(record.cancel_reason, "DECAY")
        self.assertEqual(record.exit_T, T + 2 * 3600)


class TestActiveExits(OutcomeCase):

    QUIET = [h1(index, 103.0, 104.0, 102.0, 103.0) for index in range(1, 6)]

    def test_an_htf_flip_kill_while_active_closes_at_the_reference_close(self):
        """C-4.9: the kill alert tells the operator to exit; at H1 cadence that
        candle's close is the honest actionable price."""
        record = self.resolve([REF, W0_TOUCH] + self.QUIET,
                              htf_kill_boundary=T + 3 * 3600)
        self.assertEqual(record.state, STATE_INVALIDATED_EXIT)
        self.assertEqual(record.exit_reason, "HTF_KILL")
        self.assertEqual(record.exit_T, T + 2 * 3600)      # C_ref of that cycle
        self.assertEqual(record.exit_price, 103.0)
        self.assertEqual(record.holding_candles, 3)
        self.assertGreater(record.realized_r, 0.0)         # may be positive

    def test_a_price_exit_inside_the_kill_cycle_beats_the_kill(self):
        """C-4.13 again: the stop was struck before the scan that killed it."""
        record = self.resolve(
            [REF, W0_TOUCH, h1(1, 103.0, 104.0, 94.0, 95.0)],
            htf_kill_boundary=T + 2 * 3600)
        self.assertEqual(record.state, STATE_LOSS)
        self.assertEqual(record.exit_reason, "SL")

    def test_a_kill_before_the_fill_cannot_apply_to_an_active_trade(self):
        record = self.resolve([REF, W0_TOUCH] + self.QUIET,
                              htf_kill_boundary=T + 3600)
        self.assertEqual(record.state, STATE_INVALIDATED_EXIT)
        self.assertEqual(record.exit_T, T)                 # W0, the fill candle

    def test_the_holding_cap_expires_the_trade_at_that_candles_close(self):
        """C-4.10: `holding_candles` counts from the fill candle inclusive."""
        record = self.resolve([REF, W0_TOUCH] + self.QUIET,
                              config=OutcomeConfig(max_holding_h1=3))
        self.assertEqual(record.state, STATE_EXPIRED)
        self.assertEqual(record.exit_reason, "MAX_HOLDING")
        self.assertEqual(record.holding_candles, 3)
        self.assertEqual(record.exit_T, T + 2 * 3600)
        self.assertEqual(record.exit_price, 103.0)

    def test_a_one_candle_cap_expires_on_the_fill_candle(self):
        record = self.resolve([REF, W0_TOUCH] + self.QUIET,
                              config=OutcomeConfig(max_holding_h1=1))
        self.assertEqual(record.state, STATE_EXPIRED)
        self.assertEqual(record.holding_candles, 1)
        self.assertEqual(record.exit_T, T)


class TestOpenLabel(OutcomeCase):
    """C-3.1 / C-4.10: `OPEN` is derived, never stored as a state."""

    def test_a_pending_trade_at_the_end_of_data_is_open_and_unfilled(self):
        record = self.resolve([REF, h1(0, 104.0, 106.0, 101.0, 105.0)])
        self.assertEqual(record.state, STATE_PENDING)
        self.assertTrue(record.is_open)
        self.assertFalse(record.is_terminal)
        self.assertFalse(record.filled)
        self.assertEqual(record.exit_reason, "END_OF_DATA")

    def test_an_active_trade_at_the_end_of_data_is_open_and_marked_to_market(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 104.0, 102.0, 103.5)])
        self.assertEqual(record.state, STATE_ACTIVE)
        self.assertTrue(record.is_open)
        self.assertTrue(record.filled)
        self.assertEqual(record.exit_reason, "END_OF_DATA")
        self.assertEqual(record.exit_price, 103.5)
        self.assertEqual(record.realized_r, (103.5 - ENTRY) / (ENTRY - SL))

    def test_a_continuation_with_no_walk_origin_never_fills(self):
        record = self.resolve([REF], entry_mode="CONTINUATION")
        self.assertFalse(record.filled)
        self.assertEqual(record.state, STATE_PENDING)


class TestInvalidLevels(OutcomeCase):
    """C-4.14: rejected before any walk, counted, excluded from performance."""

    def test_inverted_long_levels_are_terminal(self):
        record = self.resolve([REF, W0_TOUCH], sl=110.0, tp=95.0)
        self.assertEqual(record.state, STATE_INVALID_LEVELS)
        self.assertFalse(record.filled)
        self.assertIsNone(record.realized_r)
        self.assertIn(record.state, TERMINAL_STATES)

    def test_inverted_short_levels_are_terminal(self):
        record = self.resolve([REF, W0_TOUCH], direction="SHORT",
                              sl=90.0, tp=105.0)
        self.assertEqual(record.state, STATE_INVALID_LEVELS)

    def test_a_non_positive_level_is_terminal(self):
        record = self.resolve([REF, W0_TOUCH], sl=0.0)
        self.assertEqual(record.state, STATE_INVALID_LEVELS)

    def test_a_non_finite_level_is_terminal(self):
        record = self.resolve([REF, W0_TOUCH], tp=float("inf"))
        self.assertEqual(record.state, STATE_INVALID_LEVELS)

    def test_a_fill_on_the_stop_has_no_risk_unit(self):
        """C-9.6: `R_unit == 0` is terminal `INVALID_LEVELS`."""
        ref = h1(-1, 99.0, 100.0, 98.0, 99.0)         # pre-crossed -> open fill
        record = self.resolve([ref, h1(0, 95.0, 111.0, 94.0, 110.0)])
        self.assertEqual(record.state, STATE_INVALID_LEVELS)
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_price, SL)


# ==========================================================================
# TASK 6 — excursions and the record
# ==========================================================================

class TestExcursions(OutcomeCase):

    def test_excursions_are_signed_and_measured_from_m5(self):
        m5 = flat_m5(0, 104.0, 105.0, 99.0, 103.0) + \
             m5_of(1, [(103.0, 111.0, 97.0, 110.0)] * 12)
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 97.0, 110.5)], m5)
        self.assertEqual(record.state, STATE_WIN)
        self.assertFalse(record.excursion_degraded)
        self.assertLessEqual(record.mae_r, 0.0)
        self.assertGreaterEqual(record.mfe_r, 0.0)
        self.assertEqual(record.mae_r, (97.0 - ENTRY) / (ENTRY - SL))

    def test_a_missing_m5_set_degrades_the_excursion_and_is_counted(self):
        engine = self.engine([REF, W0_TOUCH,
                              h1(1, 103.0, 111.0, 102.0, 110.5)])
        report = engine.resolve_all([self.intent()])
        self.assertTrue(report.records[0].excursion_degraded)
        self.assertEqual(report.counters.excursion_degraded, 1)

    def test_excursion_signs_hold_even_when_the_fill_is_an_extreme(self):
        record = self.resolve([REF, h1(0, 104.0, 111.0, 100.0, 110.5)])
        self.assertLessEqual(record.mae_r, 0.0)
        self.assertGreaterEqual(record.mfe_r, 0.0)


class TestRecord(OutcomeCase):

    def test_the_record_is_immutable(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        with self.assertRaises(Exception):
            record.state = STATE_LOSS               # type: ignore[misc]

    def test_the_record_carries_every_contract_group(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        data = record.to_dict()
        for field in (
            # identity
            "run_id", "source", "thesis_key", "symbol", "direction",
            "entry_mode", "boundary", "h1_last_ts", "cycle_index",
            "universe_rank", "alert_tier", "shadow_kind", "in_burn_in",
            # frozen levels
            "entry_price", "sl", "tp", "structural_rr", "tp_source",
            "entry_source", "score",
            # fill
            "filled", "fill_kind", "fill_T", "fill_price", "candles_to_fill",
            # exit
            "state", "cancel_reason", "exit_reason", "exit_T", "exit_price",
            "realized_r", "realized_r_net", "holding_candles",
            # path
            "mae_r", "mfe_r", "excursion_degraded",
            # lineage
            "contract_version", "strategy_version", "ledger_schema_version",
            "outcome_schema_version", "outcome_config_hash",
            "data_snapshot_id", "env",
        ):
            self.assertIn(field, data, field)

    def test_the_record_carries_no_derived_metric(self):
        """Expectancy, win rate and drawdown belong to the metrics engine."""
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        data = record.to_dict()
        for forbidden in ("expectancy", "win_rate", "drawdown", "equity",
                          "pnl", "profit", "sharpe", "is_open", "is_breakeven"):
            self.assertNotIn(forbidden, data)

    def test_open_and_breakeven_are_derived_properties(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        self.assertFalse(record.is_open)
        self.assertFalse(record.is_breakeven)
        self.assertTrue(replace(record, realized_r=0.05).is_breakeven)

    def test_lineage_identifies_the_producing_environment(self):
        engine = self.engine([REF, W0_TOUCH,
                              h1(1, 103.0, 111.0, 102.0, 110.5)])
        record = engine.resolve(self.intent())
        self.assertEqual(record.contract_version, "1.1")
        self.assertEqual(record.ledger_schema_version, 2)
        self.assertEqual(record.outcome_schema_version, 2)
        self.assertEqual(record.outcome_config_hash, engine.config_hash)
        self.assertIn("pandas_ta", record.env)
        self.assertTrue(record.data_snapshot_id)

    def test_the_signal_id_joins_back_to_the_ledger_row(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        self.assertEqual(record.signal_id,
                         "BTC/USDT|RETRACEMENT|1772319600.0")

    def test_the_net_r_applies_the_fee_when_enabled(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)],
                              config=OutcomeConfig(taker_fee_rate=0.0005))
        gross = 2.0
        self.assertEqual(record.realized_r, gross)
        self.assertEqual(record.realized_r_net,
                         gross - 2 * 0.0005 * ENTRY / (ENTRY - SL))

    def test_gross_equals_net_in_the_zero_cost_baseline(self):
        record = self.resolve([REF, W0_TOUCH,
                               h1(1, 103.0, 111.0, 102.0, 110.5)])
        self.assertEqual(record.realized_r, record.realized_r_net)


# ==========================================================================
# TASK 7 / TASK 10 — output and determinism
# ==========================================================================

class TestOutput(OutcomeCase):

    def intents(self):
        return [self.intent(),
                self.intent(symbol="ETH/USDT", cycle_index=1, universe_rank=2)]

    def setUp(self) -> None:
        super().setUp()
        datasets = {
            ("BTCUSDT", "1h"): [REF, W0_TOUCH,
                                h1(1, 103.0, 111.0, 102.0, 110.5)],
            ("ETHUSDT", "1h"): [REF, W0_TOUCH,
                                h1(1, 103.0, 104.0, 94.0, 94.5)],
        }
        self.repo = write_repository(self.tmp / "repo", datasets)
        self.eng = OutcomeEngine(self.repo)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def test_one_line_per_trade(self):
        report = self.eng.write(self.intents(), self.run_dir)
        lines = [line for line in
                 (self.run_dir / OUTCOMES_FILENAME).read_text(
                     encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(report), 2)
        self.assertEqual(report.states, {STATE_WIN: 1, STATE_LOSS: 1})

    def test_the_engine_writes_only_outcomes_jsonl(self):
        """Replay stays immutable: the engine adds one artifact and no more."""
        (self.run_dir / "signal_ledger.jsonl").write_text("untouched\n",
                                                          encoding="utf-8")
        (self.run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        self.eng.write(self.intents(), self.run_dir)
        after = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        self.assertEqual(set(after) - set(before), {OUTCOMES_FILENAME})
        for name, payload in before.items():
            self.assertEqual(after[name], payload, name)

    def test_an_existing_outcomes_file_is_refused(self):
        self.eng.write(self.intents(), self.run_dir)
        with self.assertRaises(OutcomeError):
            self.eng.write(self.intents(), self.run_dir)

    def test_overwriting_replaces_rather_than_appends(self):
        first = self.eng.write(self.intents(), self.run_dir)
        payload = (self.run_dir / OUTCOMES_FILENAME).read_bytes()
        self.eng.write(self.intents(), self.run_dir, overwrite=True)
        self.assertEqual((self.run_dir / OUTCOMES_FILENAME).read_bytes(),
                         payload)
        self.assertEqual(len(first), 2)

    def test_a_missing_run_directory_is_refused(self):
        with self.assertRaises(OutcomeError):
            self.eng.write(self.intents(), self.tmp / "nowhere")

    def test_records_are_written_in_a_fixed_order(self):
        report = self.eng.write(
            [self.intent(symbol="ETH/USDT", cycle_index=1, universe_rank=2),
             self.intent()], self.run_dir)
        self.assertEqual([r.symbol for r in report.records],
                         ["BTC/USDT", "ETH/USDT"])

    def test_two_runs_are_byte_identical(self):
        """Task 10: given the same ledger and prices, the same outcomes."""
        first_dir = self.tmp / "a"
        second_dir = self.tmp / "b"
        first_dir.mkdir()
        second_dir.mkdir()
        self.eng.write(self.intents(), first_dir)
        OutcomeEngine(self.repo).write(self.intents(), second_dir)
        self.assertEqual((first_dir / OUTCOMES_FILENAME).read_bytes(),
                         (second_dir / OUTCOMES_FILENAME).read_bytes())

    def test_resolution_is_repeatable_in_process(self):
        intent = self.intent()
        self.assertEqual(self.eng.resolve(intent).to_dict(),
                         self.eng.resolve(intent).to_dict())

    def test_a_different_config_is_a_different_hash_and_is_stamped(self):
        other = OutcomeEngine(self.repo,
                              config=OutcomeConfig(max_holding_h1=24))
        self.assertNotEqual(other.config_hash, self.eng.config_hash)
        self.assertEqual(other.resolve(self.intent()).outcome_config_hash,
                         other.config_hash)


class TestConfig(unittest.TestCase):

    def test_the_field_set_is_the_contract_list(self):
        self.assertEqual(set(OutcomeConfig().to_dict()), {
            "max_holding_h1", "continuation_fill", "limit_fill_rule",
            "tie_break", "no_m5_policy", "taker_fee_rate", "slippage_rate",
            "funding_rate_model", "shadow_pending_h1", "burn_in_h1",
            "equity_convention"})

    def test_the_defaults_are_the_contract_defaults(self):
        config = OutcomeConfig()
        self.assertEqual(config.max_holding_h1, 168)
        self.assertEqual(config.continuation_fill, "w0_open")
        self.assertEqual(config.limit_fill_rule, "touch")
        self.assertEqual(config.tie_break, "sl_first")
        self.assertEqual(config.no_m5_policy, "worst_case")
        self.assertEqual(config.taker_fee_rate, 0.0)
        self.assertEqual(config.slippage_rate, 0.0)
        self.assertEqual(config.funding_rate_model, "none")
        self.assertEqual(config.shadow_pending_h1, 3)
        self.assertEqual(config.burn_in_h1, 336)
        self.assertEqual(config.equity_convention, "1R")

    def test_the_hash_is_stable_and_sixteen_hex_characters(self):
        self.assertEqual(OutcomeConfig().config_hash,
                         OutcomeConfig().config_hash)
        self.assertEqual(len(OutcomeConfig().config_hash), 16)

    def test_every_unimplemented_variant_is_refused_not_ignored(self):
        for field, value in (("limit_fill_rule", "close"),
                             ("tie_break", "tp_first"),
                             ("no_m5_policy", "best_case"),
                             ("funding_rate_model", "linear"),
                             ("equity_convention", "usd"),
                             ("continuation_fill", "w1_open"),
                             ("slippage_rate", 0.0005)):
            with self.assertRaises(OutcomeConfigError, msg=field):
                OutcomeConfig(**{field: value})


class TestDataFailures(OutcomeCase):

    def test_a_symbol_with_no_prices_cannot_be_resolved(self):
        engine = self.engine([REF, W0_TOUCH])
        with self.assertRaises(OutcomeDataError):
            engine.resolve(self.intent(symbol="GHOST/USDT"))

    def test_a_missing_reference_candle_is_an_error_not_a_guess(self):
        """C-4.1 needs `close(C_ref)` to decide whether the limit was crossed."""
        engine = self.engine([W0_TOUCH])          # no candle at h1_last_ts
        with self.assertRaises(OutcomeDataError):
            engine.resolve(self.intent())

    def test_a_corrupt_dataset_surfaces_as_a_data_error(self):
        engine = self.engine([REF, W0_TOUCH])
        dataset_path(self.tmp / "repo", "BTCUSDT", "1h").write_bytes(b"nope")
        from backtest.data.repository import KlineRepository
        broken = OutcomeEngine(KlineRepository(self.tmp / "repo"))
        with self.assertRaises(OutcomeDataError):
            broken.resolve(self.intent())


# ==========================================================================
# TASK 9 — boundaries
# ==========================================================================

class TestBoundaries(unittest.TestCase):
    OUTCOME = Path("backtest") / "outcome"

    def test_production_never_imports_the_outcome_engine(self):
        """C-9.1: nothing in `v2/` knows it exists."""
        from tests.backtest.test_historical_adapter import imported_modules
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"),
                                 f"{path} imports {module}")

    def test_the_engine_never_reads_through_the_historical_adapter(self):
        """C-9.5: the repository directly, by time range."""
        from tests.backtest.test_historical_adapter import imported_modules
        for path in self.OUTCOME.glob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.replay"),
                                 f"{path.name} imports {module}")

    def test_the_engine_depends_only_on_data_and_v2_domain(self):
        """§10: `outcome, metrics -> data + v2.domain`."""
        from tests.backtest.test_historical_adapter import imported_modules
        allowed = {
            "__future__", "dataclasses", "typing", "json", "math", "pathlib",
            "hashlib", "numpy",
            "backtest.data.repository", "backtest.lineage",
            # The capture wire format (C-15.3), in a leaf that imports neither
            # side: the builder reads what the capture layer writes, and §10
            # forbids `outcome` from importing `replay`. Sharing the schema is
            # what makes that separation possible rather than duplicated.
            "backtest.capture_schema",
            "backtest.outcome.builder",
            "backtest.outcome.config", "backtest.outcome.errors",
            "backtest.outcome.record", "backtest.outcome.trade",
            "backtest.outcome.walker", "backtest.outcome.engine",
            "v2.app.ledger", "v2.domain.signal_ledger",
        }
        for path in self.OUTCOME.glob("*.py"):
            for module in imported_modules(path):
                self.assertIn(module, allowed, f"{path.name}: {module}")

    def test_the_engine_holds_no_analysis_or_lifecycle_dependency(self):
        from tests.backtest.test_historical_adapter import imported_modules
        for path in self.OUTCOME.glob("*.py"):
            for module in imported_modules(path):
                for forbidden in ("v2.analysis", "v2.lifecycle", "v2.app.orch",
                                  "v2.infra"):
                    self.assertFalse(module.startswith(forbidden),
                                     f"{path.name}: {module}")

    def test_the_engine_computes_no_aggregate(self):
        """Statistics, expectancy and reports belong to the metrics engine."""
        for path in self.OUTCOME.glob("*.py"):
            code = path.read_text(encoding="utf-8").lower()
            for forbidden in ("expectancy", "win_rate", "sharpe", "drawdown",
                              "profit_factor"):
                self.assertNotIn(f"def {forbidden}", code)

    def test_no_outcome_field_is_written_into_the_ledger(self):
        """C-9.5: one-way derivation."""
        from v2.domain.signal_ledger import SignalLedgerRecord
        fields = set(SignalLedgerRecord.__dataclass_fields__)
        for outcome_only in ("state", "realized_r", "exit_reason", "fill_price",
                             "mae_r", "mfe_r", "holding_candles"):
            self.assertNotIn(outcome_only, fields, outcome_only)


# ==========================================================================
# M11 TASK 2 — N2: the record carries its own comparison key (C-5.7.2)
# ==========================================================================

class TestRunLineage(OutcomeCase):
    """`burn_in_h1` and `universe_mode` become properties of the data."""

    WALK = (REF, W0_TOUCH, h1(1, 103.0, 111.0, 102.0, 110.5))

    def manifest(self, directory: Path, **core) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": "replay-abc",
            "core": {
                "burn_in_h1": 336, "universe_mode": "volume",
                "T_0": 1_772_323_200, "T_n": 1_772_409_600,
                "data_snapshot_id": "snap0123456789ab",
                **core,
            },
        }
        path = directory / "manifest.json"
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    # -- the fields exist and are lineage ---------------------------------

    def test_the_record_carries_all_five_comparison_preconditions(self):
        """C-5.7.2 names five; a record must now answer all of them alone."""
        engine = OutcomeEngine(
            write_repository(self.tmp / "r", {("BTCUSDT", "1h"): list(self.WALK)}),
            lineage=RunLineage(burn_in_h1=336, universe_mode="volume"))
        record = engine.resolve(self.intent())
        self.assertEqual(record.burn_in_h1, 336)
        self.assertEqual(record.universe_mode, "volume")
        self.assertTrue(record.data_snapshot_id)
        self.assertEqual(record.boundary, T)

    def test_undeclared_lineage_is_none_and_never_a_guessed_default(self):
        record = self.resolve(self.WALK)
        self.assertIsNone(record.burn_in_h1)
        self.assertIsNone(record.universe_mode)

    def test_the_lineage_is_read_from_the_run_manifest(self):
        run_dir = self.tmp / "run"
        self.manifest(run_dir, burn_in_h1=48, universe_mode="static")
        repo = write_repository(self.tmp / "r",
                                {("BTCUSDT", "1h"): list(self.WALK)})
        engine = OutcomeEngine.for_run(repo, run_dir)
        self.assertEqual(engine.lineage.burn_in_h1, 48)
        self.assertEqual(engine.lineage.universe_mode, "static")
        record = engine.resolve(self.intent())
        self.assertEqual(record.burn_in_h1, 48)
        self.assertEqual(record.universe_mode, "static")

    def test_the_manifest_reader_also_recovers_the_window(self):
        run_dir = self.tmp / "run"
        self.manifest(run_dir)
        lineage = read_run_lineage(run_dir)
        self.assertEqual(lineage.window_start, 1_772_323_200)
        self.assertEqual(lineage.window_end, 1_772_409_600)
        self.assertEqual(lineage.data_snapshot_id, "snap0123456789ab")
        self.assertTrue(lineage.declared)

    def test_a_missing_manifest_is_an_error_not_an_empty_lineage(self):
        with self.assertRaises(LineageError):
            read_run_lineage(self.tmp / "nowhere")

    def test_a_manifest_without_a_core_block_is_refused(self):
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text('{"run_id": "x"}',
                                               encoding="utf-8")
        with self.assertRaises(LineageError):
            read_run_lineage(run_dir)

    def test_a_manifest_missing_the_keys_yields_an_undeclared_lineage(self):
        """Absent is undeclared. `freeze()` refuses it; nothing guesses."""
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text('{"core": {"T_0": 1}}',
                                               encoding="utf-8")
        lineage = read_run_lineage(run_dir)
        self.assertIsNone(lineage.burn_in_h1)
        self.assertIsNone(lineage.universe_mode)
        self.assertFalse(lineage.declared)

    # -- proof: metadata only ---------------------------------------------

    def test_lineage_changes_no_other_field_of_the_record(self):
        """The N2 proof, at record level: only the two new keys differ."""
        rows = list(self.WALK)
        bare = OutcomeEngine(
            write_repository(self.tmp / "a", {("BTCUSDT", "1h"): rows})
        ).resolve(self.intent())
        stamped = OutcomeEngine(
            write_repository(self.tmp / "b", {("BTCUSDT", "1h"): rows}),
            lineage=RunLineage(burn_in_h1=336, universe_mode="volume"),
        ).resolve(self.intent())
        before, after = bare.to_dict(), stamped.to_dict()
        differing = {k for k in before if before[k] != after[k]}
        self.assertEqual(differing, {"burn_in_h1", "universe_mode"})

    def test_the_lineage_is_read_by_no_computation(self):
        """A walk result cannot depend on how a run was configured."""
        rows = list(self.WALK)
        results = []
        for burn_in, mode in ((0, "volume"), (999, "static"), (None, None)):
            engine = OutcomeEngine(
                write_repository(self.tmp / f"r{burn_in}",
                                 {("BTCUSDT", "1h"): rows}),
                lineage=RunLineage(burn_in_h1=burn_in, universe_mode=mode))
            record = engine.resolve(self.intent())
            payload = record.to_dict()
            del payload["burn_in_h1"], payload["universe_mode"]
            results.append(json.dumps(payload, sort_keys=True))
        self.assertEqual(len(set(results)), 1)

    # -- compatibility -----------------------------------------------------

    def test_a_schema_1_line_still_parses_with_undeclared_lineage(self):
        """Backward compatibility: an old file is still valid evidence."""
        from backtest.dataset import read_outcomes
        record = self.resolve(self.WALK)
        row = record.to_dict()
        del row["burn_in_h1"], row["universe_mode"]
        row["outcome_schema_version"] = 1
        path = self.tmp / "outcomes.jsonl"
        path.write_text(json.dumps(row, sort_keys=True) + "\n",
                        encoding="utf-8")
        parsed = read_outcomes(path)
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0].burn_in_h1)
        self.assertIsNone(parsed[0].universe_mode)

    def test_a_stamped_record_round_trips_through_the_reader(self):
        from backtest.dataset import read_outcomes
        engine = OutcomeEngine(
            write_repository(self.tmp / "r", {("BTCUSDT", "1h"): list(self.WALK)}),
            lineage=RunLineage(burn_in_h1=336, universe_mode="volume"))
        run_dir = self.tmp / "out"
        run_dir.mkdir()
        engine.write([self.intent()], run_dir)
        parsed = read_outcomes(run_dir / OUTCOMES_FILENAME)
        self.assertEqual(parsed[0].burn_in_h1, 336)
        self.assertEqual(parsed[0].universe_mode, "volume")

    def test_the_schema_version_records_that_the_field_set_changed(self):
        record = self.resolve(self.WALK)
        self.assertEqual(record.outcome_schema_version, 2)


if __name__ == "__main__":
    unittest.main()
