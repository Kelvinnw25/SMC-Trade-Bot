"""app.ledger — assemble_ledger_record from analysis outputs (Phase 7.1).

Covers a scored retracement (all fields populated from the candidate), a
no_direction SKIP (candidate None -> HTF from the pre-M5 context), and an
invalid-input SKIP (HTF None everywhere). Also verifies the orchestrator writes
one ledger record per analyzed symbol regardless of decision, and none on a
pre-analysis skip.
"""
from __future__ import annotations

import unittest

from v2.app.ledger import CONTRACT_VERSION, STRATEGY_VERSION, assemble_ledger_record
from v2.domain import LEDGER_SCHEMA_VERSION, SignalLedgerRecord
from v2.infra.market_data import SymbolVolume

from tests.app.support import (
    FakeAnalyzer,
    FakeMarketData,
    FakePersistence,
    make_snapshot,
    retracement_ob_signal,
)
from tests.app.test_orchestrator import make_orch, skip_signal, valid_long
from tests.lifecycle.support import CycleSpec, make_signal


class _Ctx:
    """Minimal stand-in for the pre-M5 AnalysisContext (only .htf is read)."""

    def __init__(self, htf) -> None:
        self.htf = htf


def _assemble(snapshot, ctx, signal, *, session_active=True, recorded_at=1000.0):
    return assemble_ledger_record(
        snapshot, ctx, signal, session_active=session_active, recorded_at=recorded_at
    )


class TestAssembleScored(unittest.TestCase):
    def setUp(self):
        self.signal = retracement_ob_signal("BTC/USDT", entry_price=100.0, sl=95.0, tp=115.0)
        self.snap = make_snapshot("BTC/USDT", live_price=101.0, h1_last_ts=5000.0)
        self.rec = _assemble(self.snap, _Ctx(None), self.signal)

    def test_provenance(self):
        r = self.rec
        self.assertEqual(r.schema_version, LEDGER_SCHEMA_VERSION)
        self.assertEqual(r.strategy_version, STRATEGY_VERSION)
        self.assertEqual(r.recorded_at, 1000.0)
        self.assertEqual(r.symbol, "BTC/USDT")
        self.assertEqual(r.h1_last_ts, 5000.0)

    def test_decision_and_context(self):
        r = self.rec
        self.assertEqual(r.decision, "VALID")
        self.assertEqual(r.direction, "LONG")
        self.assertEqual(r.entry_mode, "RETRACEMENT")
        self.assertEqual(r.regime, "expansion_up")
        self.assertTrue(r.session_active)
        self.assertEqual(r.live_price, 101.0)

    def test_levels_from_candidate(self):
        r = self.rec
        self.assertEqual((r.entry_price, r.sl, r.tp), (100.0, 95.0, 115.0))
        self.assertEqual(r.entry_source, "retracement_ob")
        self.assertTrue(r.entry_ready)

    def test_selected_ob_populated(self):
        r = self.rec
        self.assertEqual(r.selected_ob_top, 100.0)
        self.assertEqual(r.selected_ob_bottom, 98.0)
        self.assertEqual(r.ob_candidate_count, 1)
        self.assertTrue(r.fresh_ob)

    def test_confirmations_present(self):
        r = self.rec
        # candidate present -> booleans are real bools (not None)
        for v in (r.sweep, r.fvg, r.displacement, r.volume_spike, r.target_clear,
                  r.htf_context_clean):
            self.assertIsInstance(v, bool)

    def test_serializes(self):
        SignalLedgerRecord.from_jsonl_dict(self.rec.to_jsonl_dict())  # no raise


class TestAssembleNoCandidate(unittest.TestCase):
    def test_no_direction_uses_context_htf(self):
        signal, htf = make_signal(CycleSpec(
            symbol="ETH/USDT", direction=None, regime="neutral",
            d1_bias="BEARISH", h4_bias="BEARISH",
        ))
        self.assertIsNone(signal.candidate)          # no candidate on no_direction
        rec = _assemble(make_snapshot("ETH/USDT"), _Ctx(htf), signal)
        self.assertEqual(rec.decision, "SKIP")
        self.assertIsNone(rec.direction)
        # HTF fields come from the context's htf, not the (absent) candidate.
        self.assertEqual(rec.d1_bias, "BEARISH")
        self.assertEqual(rec.h4_bias, "BEARISH")
        # candidate-only fields are None.
        self.assertIsNone(rec.sweep)
        self.assertIsNone(rec.entry_price)
        self.assertIsNone(rec.selected_ob_top)
        self.assertIsNone(rec.atr_last)

    def test_invalid_input_htf_none(self):
        signal, _ = make_signal(CycleSpec(symbol="X/USDT", has_htf=False))
        self.assertIsNone(signal.candidate)
        rec = _assemble(make_snapshot("X/USDT"), _Ctx(None), signal)
        self.assertEqual(rec.decision, "SKIP")
        self.assertIsNone(rec.weekly_bias)
        self.assertIsNone(rec.d1_bias)
        self.assertIsNone(rec.entry_price)


class TestAssembleLineage(unittest.TestCase):
    """Milestone M1 — the schema-2 lineage keywords (C-16.1)."""

    def setUp(self):
        self.signal = retracement_ob_signal("BTC/USDT")
        self.snap = make_snapshot("BTC/USDT", d1_rows=119, h1_rows=499)

    def test_live_defaults(self):
        r = _assemble(self.snap, _Ctx(None), self.signal)
        self.assertEqual(r.source, "live")
        self.assertEqual(r.contract_version, CONTRACT_VERSION)
        self.assertIsNone(r.run_id)
        self.assertIsNone(r.cycle_index)
        self.assertIsNone(r.universe_rank)
        self.assertIsNone(r.data_snapshot_id)   # live has no kline snapshot

    def test_lineage_is_copied_verbatim(self):
        r = assemble_ledger_record(
            self.snap, _Ctx(None), self.signal,
            session_active=True, recorded_at=1000.0,
            source="replay", run_id="replay-abc", cycle_index=42, universe_rank=7,
            data_snapshot_id="snap-xyz",
        )
        self.assertEqual(
            (r.source, r.run_id, r.cycle_index, r.universe_rank, r.data_snapshot_id),
            ("replay", "replay-abc", 42, 7, "snap-xyz"),
        )
        self.assertEqual(r.contract_version, CONTRACT_VERSION)

    def test_window_shapes_are_the_served_row_counts(self):
        r = _assemble(self.snap, _Ctx(None), self.signal)
        self.assertEqual((r.d1_rows, r.h1_rows), (119, 499))

    def test_m5_rows_is_zero_when_no_m5_attached(self):
        # 0 (not None) so "no M5 attached" is distinguishable from "attached but
        # the score gate did not fire" (C-16.1).
        self.assertIsNone(self.snap.m5)
        self.assertEqual(_assemble(self.snap, _Ctx(None), self.signal).m5_rows, 0)

    def test_m5_rows_counts_attached_frame(self):
        import pandas as pd
        m5 = pd.DataFrame({"timestamp": range(199), "close": [1.0] * 199})
        snap = make_snapshot("BTC/USDT", m5=m5)
        self.assertEqual(_assemble(snap, _Ctx(None), self.signal).m5_rows, 199)


class TestOrchestratorWiring(unittest.TestCase):
    def test_one_record_per_analyzed_symbol_all_tiers(self):
        symbols = ["A/USDT", "B/USDT"]
        snaps = {s: make_snapshot(s) for s in symbols}
        persistence = FakePersistence()
        orch = make_orch(
            market=FakeMarketData(symbols, snaps),
            analyzer=FakeAnalyzer({
                "A/USDT": valid_long("A/USDT"),   # scored VALID
                "B/USDT": skip_signal("B/USDT"),     # SKIP
            }),
            persistence=persistence,
        )
        orch.run_cycle()
        self.assertEqual(len(persistence.ledger_records), 2)  # both tiers recorded
        decisions = {r.symbol: r.decision for r in persistence.ledger_records}
        self.assertEqual(decisions["A/USDT"], "VALID")
        self.assertEqual(decisions["B/USDT"], "SKIP")

    def test_no_record_on_pre_analysis_skip(self):
        # snapshot None -> analysis never runs -> no ledger record.
        persistence = FakePersistence()
        orch = make_orch(
            market=FakeMarketData(["A/USDT"], {"A/USDT": None}),
            analyzer=FakeAnalyzer({}),
            persistence=persistence,
        )
        orch.run_cycle()
        self.assertEqual(persistence.ledger_records, [])

    def test_ledger_write_failure_is_swallowed(self):
        from v2.infra.persistence import PersistenceError
        persistence = FakePersistence(append_error=PersistenceError("disk full"))
        orch = make_orch(
            market=FakeMarketData(["A/USDT"], {"A/USDT": make_snapshot("A/USDT")}),
            analyzer=FakeAnalyzer({"A/USDT": valid_long("A/USDT")}),
            persistence=persistence,
        )
        orch.run_cycle()  # must not raise
        self.assertEqual(persistence.ledger_records, [])


class TestOrchestratorLineage(unittest.TestCase):
    """Milestone M1 — the orchestrator threads lineage into every record
    (C-16.3.1 items 3-5). Nothing reads these values; they are carried only."""

    def _run(self, *, symbols, cycles=1, **orch_kw):
        snaps = {s: make_snapshot(s) for s in symbols}
        persistence = FakePersistence()
        orch = make_orch(
            market=FakeMarketData(symbols, snaps),
            analyzer=FakeAnalyzer({s: valid_long(s) for s in symbols}),
            persistence=persistence,
            **orch_kw,
        )
        for _ in range(cycles):
            orch.run_cycle()
        return persistence.ledger_records

    def test_injected_lineage_reaches_every_record(self):
        records = self._run(
            symbols=["A/USDT", "B/USDT"],
            source="replay", run_id="replay-deadbeef", data_snapshot_id="snap-1",
        )
        self.assertEqual(len(records), 2)
        for r in records:
            self.assertEqual(r.source, "replay")
            self.assertEqual(r.run_id, "replay-deadbeef")
            self.assertEqual(r.data_snapshot_id, "snap-1")
            self.assertEqual(r.contract_version, CONTRACT_VERSION)
            self.assertEqual(r.schema_version, LEDGER_SCHEMA_VERSION)

    def test_universe_rank_is_the_1_based_processing_order(self):
        records = self._run(symbols=["A/USDT", "B/USDT", "C/USDT"])
        ranks = {r.symbol: r.universe_rank for r in records}
        self.assertEqual(ranks["A/USDT"], 1)
        self.assertEqual(ranks["B/USDT"], 2)   # the second symbol -> rank 2
        self.assertEqual(ranks["C/USDT"], 3)

    def test_cycle_index_starts_at_zero_and_increments(self):
        records = self._run(symbols=["A/USDT"], cycles=3)
        self.assertEqual([r.cycle_index for r in records], [0, 1, 2])

    def test_empty_universe_cycle_still_consumes_an_index(self):
        # C-16.3.1 item 4: an empty cycle is still a cycle, so the next real
        # cycle's index reflects it — otherwise cycle_index would not align with
        # the replay driver's boundary index.
        class TwoPhaseMarket:
            """Empty universe on the first call, one symbol afterwards."""

            def __init__(self, symbol):
                self.symbol = symbol
                self.calls = 0

            def get_top_volume_symbols(self, limit):
                self.calls += 1
                if self.calls == 1:
                    return ()
                return (SymbolVolume(symbol=self.symbol, volume=1.0),)

            def get_market_snapshot(self, symbol, *, include_m5=False):
                return make_snapshot(symbol)

        persistence = FakePersistence()
        orch = make_orch(
            market=TwoPhaseMarket("A/USDT"),
            analyzer=FakeAnalyzer({"A/USDT": valid_long("A/USDT")}),
            persistence=persistence,
        )
        self.assertFalse(orch.run_cycle())          # cycle 0: empty universe
        self.assertTrue(orch.run_cycle())           # cycle 1: one symbol
        self.assertEqual([r.cycle_index for r in persistence.ledger_records], [1])

    def test_live_defaults_when_nothing_is_injected(self):
        records = self._run(symbols=["A/USDT"])
        r = records[0]
        self.assertEqual(r.source, "live")
        self.assertIsNone(r.run_id)
        self.assertIsNone(r.data_snapshot_id)

    def test_window_shapes_recorded_from_the_snapshot(self):
        # make_snapshot's defaults: 12 D1 rows, 60 H1 rows, no M5.
        r = self._run(symbols=["A/USDT"])[0]
        self.assertEqual((r.d1_rows, r.h1_rows, r.m5_rows), (12, 60, 0))


if __name__ == "__main__":
    unittest.main()
