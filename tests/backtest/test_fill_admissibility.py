"""Market-fill admissibility — C-4.1's crossed stop and C-4.14's tick floor.

Every case is built from explicit OHLC candles and an explicit price grid, so no
assertion depends on judgement: the fixture states exactly where the market
opened and how wide one tick is, and the expected verdict follows from one
clause.

The two refusals under test:

- **`STOP_REACHED_AT_FILL`** — a CONTINUATION fills at market at `open(W0)`,
  which is the decision instant. If the market has already reached the stop
  there was no trade to place. RETRACEMENT already had this protection through
  `_pre_crossed`, which these tests pin as unchanged.
- **`SUB_TICK_RISK`** — a stop nearer the fill than one increment of the
  exchange's price grid is unrepresentable, not merely small.

Neither is `INVALID_LEVELS`: the levels are internally sound in both cases, and
what fails is admissibility of the *fill*.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

from backtest.data.repository import KlineRepository
from backtest.outcome import OutcomeConfig, OutcomeEngine
from backtest.outcome.record import OUTCOME_SCHEMA_VERSION
from backtest.outcome.ticks import (
    TickReferenceError, TickTable, load_tick_table,
)
from backtest.outcome.trade import (
    CANCEL_REASONS, FILL_REJECT_REASONS, NOT_FILLED_REASONS,
    REJECT_STOP_REACHED_AT_FILL, REJECT_SUB_TICK_RISK, STATE_INVALID_LEVELS,
    STATE_LOSS, STATE_NOT_FILLED, STATE_WIN,
)
from tests.backtest.test_outcome import (
    C_REF, ENTRY, OutcomeCase, REF, SL, T, TP, W0_TOUCH, h1,
)

#: A LONG risking 5 to make 10 uses SL 95; the SHORT mirror puts SL above.
SHORT_ENTRY, SHORT_SL, SHORT_TP = 100.0, 105.0, 90.0


def exchange_info(ticks: dict[str, float]) -> bytes:
    """A minimal `exchangeInfo` payload carrying only what the floor reads."""
    payload = {"symbols": [
        {"symbol": name, "contractType": "PERPETUAL", "quoteAsset": "USDT",
         "filters": [{"filterType": "PRICE_FILTER", "tickSize": f"{tick}"}]}
        for name, tick in sorted(ticks.items())
    ]}
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class AdmissibilityCase(OutcomeCase):
    """`OutcomeCase` plus a committed, content-addressed price grid."""

    def reference(self, ticks: dict[str, float], *,
                  root: Path | None = None) -> str:
        """Write an `exchangeInfo` artifact and return its reference id."""
        payload = exchange_info(ticks)
        name = f"exchangeInfo-{hashlib.sha256(payload).hexdigest()[:16]}"
        base = (root if root is not None else self.tmp) / "reference"
        (base / "instruments").mkdir(parents=True, exist_ok=True)
        (base / "instruments" / f"{name}.json").write_bytes(payload)
        return name

    def config(self, ticks: dict[str, float] | None = None,
               **kw) -> OutcomeConfig:
        reference = None if ticks is None else self.reference(ticks)
        return OutcomeConfig(tick_reference=reference, **kw)

    def market(self, w0_open: float, *, ticks=None, direction="LONG",
               name="repo", **intent_kw):
        """One CONTINUATION resolved against a `W0` opening at `w0_open`."""
        levels = (dict(entry_price=ENTRY, sl=SL, tp=TP) if direction == "LONG"
                  else dict(entry_price=SHORT_ENTRY, sl=SHORT_SL,
                            tp=SHORT_TP))
        low, high = min(w0_open, 80.0), max(w0_open, 120.0)
        rows = [REF, h1(0, w0_open, high, low, w0_open),
                h1(1, w0_open, high, low, w0_open)]
        levels.update(intent_kw)                 # explicit levels win
        return self.resolve(
            rows, config=self.config(ticks), name=name,
            entry_mode="CONTINUATION", direction=direction, **levels)


# ==========================================================================
# The crossed-stop guard (C-4.1)
# ==========================================================================

class TestStopReachedAtFill(AdmissibilityCase):

    def test_long_fill_exactly_at_sl_is_inadmissible(self):
        record = self.market(SL)
        self.assertFalse(record.filled)
        self.assertEqual(record.state, STATE_NOT_FILLED)
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)

    def test_long_fill_beyond_sl_is_inadmissible(self):
        record = self.market(SL - 1.0)
        self.assertFalse(record.filled)
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)

    def test_short_fill_exactly_at_sl_is_inadmissible(self):
        record = self.market(SHORT_SL, direction="SHORT")
        self.assertFalse(record.filled)
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)

    def test_short_fill_beyond_sl_is_inadmissible(self):
        record = self.market(SHORT_SL + 1.0, direction="SHORT")
        self.assertFalse(record.filled)
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)

    def test_long_fill_on_the_safe_side_still_fills(self):
        record = self.market(SL + 5.0)
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_kind, "MARKET_WALK_ORIGIN")
        self.assertEqual(record.fill_price, SL + 5.0)

    def test_short_fill_on_the_safe_side_still_fills(self):
        record = self.market(SHORT_SL - 5.0, direction="SHORT")
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_kind, "MARKET_WALK_ORIGIN")

    def test_equality_is_deterministic_across_repeats(self):
        verdicts = {self.market(SL, name=f"r{i}").cancel_reason
                    for i in range(3)}
        self.assertEqual(verdicts, {REJECT_STOP_REACHED_AT_FILL})

    def test_rejection_is_not_invalid_levels(self):
        record = self.market(SL)
        self.assertNotEqual(record.state, STATE_INVALID_LEVELS)
        self.assertIsNone(record.realized_r)
        self.assertIsNone(record.fill_price)

    def test_rejection_outranks_a_lifecycle_cancellation(self):
        from backtest.outcome import Cancellation
        record = self.market(
            SL, cancellation=Cancellation(boundary=T + 3600, reason="TTL"))
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)
        self.assertEqual(record.exit_T, T)

    def test_a_cancellation_that_beat_the_fill_still_wins(self):
        """C-4.13: a deadline excluding `W0` means the fill was never evaluated,
        so the lifecycle reason — not a refusal — is what happened."""
        from backtest.outcome import Cancellation
        record = self.market(
            SL, cancellation=Cancellation(boundary=T, reason="REPLACED"))
        self.assertEqual(record.cancel_reason, "REPLACED")


# ==========================================================================
# The sub-tick floor (C-4.14)
# ==========================================================================

class TestSubTickFloor(AdmissibilityCase):

    def test_fill_exactly_one_tick_from_sl_is_admissible(self):
        record = self.market(SL + 0.5, ticks={"BTCUSDT": 0.5})
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_price, SL + 0.5)
        self.assertFalse(record.tick_size_unavailable)

    def test_fill_nine_tenths_of_a_tick_from_sl_is_inadmissible(self):
        record = self.market(SL + 0.45, ticks={"BTCUSDT": 0.5})
        self.assertFalse(record.filled)
        self.assertEqual(record.cancel_reason, REJECT_SUB_TICK_RISK)
        self.assertEqual(record.state, STATE_NOT_FILLED)

    def test_floor_is_exactly_one_tick_and_no_wider(self):
        """Two ticks of room must never be refused — the floor is 1, not 2."""
        record = self.market(SL + 1.0, ticks={"BTCUSDT": 0.5})
        self.assertTrue(record.filled)

    def test_crossed_stop_outranks_the_tick_floor(self):
        record = self.market(SL, ticks={"BTCUSDT": 0.5})
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)

    def test_short_side_floor_uses_the_same_distance(self):
        record = self.market(SHORT_SL - 0.45, direction="SHORT",
                             ticks={"BTCUSDT": 0.5})
        self.assertEqual(record.cancel_reason, REJECT_SUB_TICK_RISK)

    def test_no_reference_leaves_the_floor_unevaluated(self):
        record = self.market(SL + 0.001, ticks=None)
        self.assertTrue(record.filled)
        self.assertTrue(record.tick_size_unavailable)


# ==========================================================================
# Missing tick metadata
# ==========================================================================

class TestMissingTick(AdmissibilityCase):

    def test_absent_symbol_admits_and_flags(self):
        record = self.market(SL + 0.001, ticks={"ETHUSDT": 0.01})
        self.assertTrue(record.filled)
        self.assertTrue(record.tick_size_unavailable)
        self.assertIsNone(record.cancel_reason)

    def test_absent_symbol_still_gets_the_crossed_stop_guard(self):
        record = self.market(SL, ticks={"ETHUSDT": 0.01})
        self.assertFalse(record.filled)
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)

    def test_present_symbol_is_not_flagged(self):
        record = self.market(SL + 5.0, ticks={"BTCUSDT": 0.5})
        self.assertFalse(record.tick_size_unavailable)

    def test_no_fallback_tick_is_invented(self):
        """A grid holding only other symbols must not lend BTC a tick."""
        table = load_tick_table(
            self.reference({"ETHUSDT": 0.01}),
            roots=(self.tmp / "reference",))
        self.assertIsNone(table.tick_for("BTCUSDT"))
        self.assertEqual(table.tick_for("ETHUSDT"), 0.01)


# ==========================================================================
# The reference artifact itself
# ==========================================================================

class TestTickReference(unittest.TestCase):

    def test_config_hash_changes_with_tick_reference(self):
        base = OutcomeConfig()
        tagged = OutcomeConfig(tick_reference="exchangeInfo-deadbeefdeadbeef")
        self.assertNotEqual(base.config_hash, tagged.config_hash)

    def test_config_hash_is_stable_for_the_same_reference(self):
        first = OutcomeConfig(tick_reference="exchangeInfo-deadbeefdeadbeef")
        second = OutcomeConfig(tick_reference="exchangeInfo-deadbeefdeadbeef")
        self.assertEqual(first.config_hash, second.config_hash)

    def test_two_references_hash_differently(self):
        self.assertNotEqual(
            OutcomeConfig(tick_reference="exchangeInfo-1111111111111111"
                          ).config_hash,
            OutcomeConfig(tick_reference="exchangeInfo-2222222222222222"
                          ).config_hash)

    def test_empty_reference_is_refused(self):
        from backtest.outcome import OutcomeConfigError
        with self.assertRaises(OutcomeConfigError):
            OutcomeConfig(tick_reference="")

    def test_default_config_declares_no_reference(self):
        self.assertIsNone(OutcomeConfig().tick_reference)


class TestReferenceIntegrity(AdmissibilityCase):

    def test_missing_reference_is_refused_not_skipped(self):
        with self.assertRaises(TickReferenceError):
            load_tick_table("exchangeInfo-0000000000000000",
                            roots=(self.tmp / "reference",))

    def test_name_must_match_content(self):
        name = self.reference({"BTCUSDT": 0.5})
        path = self.tmp / "reference" / "instruments" / f"{name}.json"
        path.write_bytes(exchange_info({"BTCUSDT": 0.25}))
        with self.assertRaises(TickReferenceError):
            load_tick_table(name, roots=(self.tmp / "reference",))

    def test_payload_without_price_filter_is_refused(self):
        payload = json.dumps({"symbols": [{"symbol": "BTCUSDT",
                                           "filters": []}]}).encode("utf-8")
        name = f"exchangeInfo-{hashlib.sha256(payload).hexdigest()[:16]}"
        directory = self.tmp / "reference" / "instruments"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_bytes(payload)
        with self.assertRaises(TickReferenceError):
            load_tick_table(name, roots=(self.tmp / "reference",))

    def test_engine_exposes_the_loaded_grid(self):
        config = self.config({"BTCUSDT": 0.5})
        engine = self.engine([REF, W0_TOUCH], config=config)
        self.assertIsInstance(engine.ticks, TickTable)
        self.assertEqual(engine.ticks.tick_for("BTCUSDT"), 0.5)

    def test_engine_without_reference_has_no_grid(self):
        engine = self.engine([REF, W0_TOUCH], config=OutcomeConfig())
        self.assertIsNone(engine.ticks)


# ==========================================================================
# Nothing else moves
# ==========================================================================

class TestRetracementUnchanged(AdmissibilityCase):

    def test_limit_touch_is_unaffected_by_the_guard(self):
        """The crossed-stop guard is CONTINUATION-only; a RETRACEMENT limit
        resting below market still fills on a touch (C-4.1)."""
        record = self.resolve([REF, W0_TOUCH, h1(1, 103.0, 112.0, 102.0, 111.0)],
                              config=self.config({"BTCUSDT": 0.5}))
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_kind, "LIMIT_TOUCH")
        self.assertEqual(record.fill_price, ENTRY)

    def test_pre_crossed_immediate_is_unaffected(self):
        """C-4.1: a limit already at or through `close(C_ref)` fills at once."""
        reference = h1(-1, 96.0, 99.0, 95.5, 99.0)      # close below the limit
        record = self.resolve([reference, h1(0, 99.0, 112.0, 98.0, 111.0)],
                              config=self.config({"BTCUSDT": 0.5}))
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_kind, "IMMEDIATE")

    def test_retracement_outcome_identical_with_and_without_a_grid(self):
        rows = [REF, W0_TOUCH, h1(1, 103.0, 112.0, 102.0, 111.0)]
        without = self.resolve(rows, config=OutcomeConfig(), name="a")
        with_grid = self.resolve(rows, config=self.config({"BTCUSDT": 0.5}),
                                 name="b")
        for field in ("filled", "fill_kind", "fill_price", "state",
                      "exit_reason", "realized_r", "mae_r", "mfe_r"):
            self.assertEqual(getattr(without, field),
                             getattr(with_grid, field), field)


class TestAdmissibleFillsUnchanged(AdmissibilityCase):

    def test_continuation_win_is_untouched(self):
        rows = [REF, h1(0, 104.0, 106.0, 103.0, 105.0),
                h1(1, 105.0, 112.0, 104.0, 111.0)]
        without = self.resolve(rows, config=OutcomeConfig(), name="a",
                               entry_mode="CONTINUATION")
        with_grid = self.resolve(rows, config=self.config({"BTCUSDT": 0.5}),
                                 name="b", entry_mode="CONTINUATION")
        self.assertEqual(without.state, STATE_WIN)
        self.assertEqual(with_grid.state, STATE_WIN)
        self.assertEqual(without.realized_r, with_grid.realized_r)
        self.assertEqual(without.fill_price, with_grid.fill_price)

    def test_r_unit_zero_is_still_invalid_levels(self):
        """C-9.6 owns a fill sitting exactly on the stop when no guard applies —
        here the tick floor is unevaluated and the fill is a RETRACEMENT limit."""
        record = self.resolve(
            [REF, h1(0, 104.0, 106.0, 95.0, 96.0)], config=OutcomeConfig(),
            entry_price=SL, sl=SL - 0.0, tp=TP)
        self.assertEqual(record.state, STATE_INVALID_LEVELS)

    def test_invalid_geometry_still_precedes_admissibility(self):
        record = self.market(SL, ticks={"BTCUSDT": 0.5}, sl=ENTRY)
        self.assertEqual(record.state, STATE_INVALID_LEVELS)
        self.assertIsNone(record.cancel_reason)


class TestVocabularyAndClosure(AdmissibilityCase):

    def test_reject_reasons_are_disjoint_from_cancel_reasons(self):
        self.assertFalse(FILL_REJECT_REASONS & CANCEL_REASONS)
        self.assertEqual(NOT_FILLED_REASONS,
                         CANCEL_REASONS | FILL_REJECT_REASONS)

    def test_reject_reasons_are_not_valid_cancellations(self):
        from backtest.outcome import Cancellation, TradeIntentError
        with self.assertRaises(TradeIntentError):
            Cancellation(boundary=T, reason=REJECT_SUB_TICK_RISK)

    def test_population_closure_over_a_mixed_book(self):
        config = self.config({"BTCUSDT": 0.5})
        engine = self.engine(
            [REF, h1(0, 96.0, 112.0, 94.0, 111.0),
             h1(1, 111.0, 112.0, 104.0, 111.0)], config=config)
        intents = [
            self.intent(entry_mode="CONTINUATION"),                  # admitted
            self.intent(entry_mode="CONTINUATION", sl=96.0, tp=TP),  # crossed
            self.intent(entry_mode="RETRACEMENT"),                   # limit
        ]
        report = engine.resolve_all(intents)
        filled = [r for r in report.records if r.filled]
        unfilled = [r for r in report.records if not r.filled]
        self.assertEqual(len(filled) + len(unfilled), len(intents))
        for record in unfilled:
            if record.state == STATE_NOT_FILLED:
                self.assertIn(record.cancel_reason, NOT_FILLED_REASONS)

    def test_schema_version_records_the_new_field(self):
        self.assertGreaterEqual(OUTCOME_SCHEMA_VERSION, 3)
        record = self.market(SL + 5.0, ticks={"BTCUSDT": 0.5})
        self.assertIn("tick_size_unavailable", record.to_dict())

    def test_rejected_record_round_trips_through_the_reader(self):
        from backtest.dataset import read_outcomes
        config = self.config({"BTCUSDT": 0.5})
        engine = self.engine(
            [REF, h1(0, SL, 120.0, 80.0, SL), h1(1, SL, 120.0, 80.0, SL)],
            config=config)
        run = self.tmp / "run"
        run.mkdir()
        engine.write([self.intent(entry_mode="CONTINUATION")], run)
        (record,) = read_outcomes(run / "outcomes.jsonl")
        self.assertEqual(record.cancel_reason, REJECT_STOP_REACHED_AT_FILL)
        self.assertFalse(record.filled)

    def test_output_is_byte_deterministic(self):
        config = self.config({"BTCUSDT": 0.5})
        payloads = []
        for index in range(2):
            engine = self.engine(
                [REF, h1(0, SL, 120.0, 80.0, SL), h1(1, SL, 120.0, 80.0, SL)],
                config=config, name=f"repo{index}")
            run = self.tmp / f"run{index}"
            run.mkdir()
            engine.write([self.intent(entry_mode="CONTINUATION")], run)
            payloads.append((run / "outcomes.jsonl").read_bytes())
        self.assertEqual(payloads[0], payloads[1])


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
