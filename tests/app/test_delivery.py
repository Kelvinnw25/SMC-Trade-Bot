"""Orchestrator delivery routing (Phase 5.2): terminal, notifier, OB-audit, cadence.

Verifies the orchestrator ROUTES analysis/lifecycle outputs to presentation and
sinks — it renders nothing itself beyond calling the pure `app.rendering` /
`app.audit` helpers, and owns no decision.
"""
from __future__ import annotations

import unittest

from v2.app.orchestrator import Orchestrator
from v2.infra.persistence import PersistenceError
from v2.lifecycle.config import LifecycleConfig

from tests.app.support import (
    CapturingWriter,
    FakeAnalyzer,
    FakeMarketData,
    FakePersistence,
    make_snapshot,
    retracement_ob_signal,
)
from tests.app.test_orchestrator import (
    make_orch,
    prior_short_alerted,
    skip_signal,
    valid_long,
    veto_short,
)


class TestTerminalRendering(unittest.TestCase):
    def test_scan_block_and_dashboard_written(self):
        symbol = "BTC/USDT"
        writer = CapturingWriter()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: valid_long(symbol)}),
            writer=writer,
        )
        orch.run_cycle()
        text = writer.joined()
        self.assertIn("[SCAN] BTC/USDT", text)
        self.assertIn("HTF  =>", text)
        self.assertIn("📋 RADAR SUMMARY", text)
        self.assertIn("➤ BTC/USDT", text)  # dashboard row

    def test_kill_skips_scan_and_writes_kill_log(self):
        symbol = "ETH/USDT"
        signal, htf = veto_short(symbol)
        writer = CapturingWriter()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: signal}, htf_by_symbol={symbol: htf}),
            persistence=FakePersistence(active={symbol: prior_short_alerted()}),
            writer=writer,
        )
        orch.run_cycle()
        text = writer.joined()
        self.assertIn("☠️ KILL", text)
        self.assertNotIn("[SCAN] ETH/USDT", text)  # killed -> no scan block

    def test_guard_log_written(self):
        symbol = "BTC/USDT"
        writer = CapturingWriter()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: valid_long(symbol, sl=0.0)}),  # invalid levels
            writer=writer,
        )
        orch.run_cycle()
        self.assertIn("⚠️ GUARD", writer.joined())


class TestOBAuditRouting(unittest.TestCase):
    def test_eligible_retracement_is_appended(self):
        symbol = "BTC/USDT"
        persistence = FakePersistence()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: retracement_ob_signal(symbol)}),
            persistence=persistence,
        )
        orch.run_cycle()
        self.assertEqual(len(persistence.ob_audits), 1)
        rec = persistence.ob_audits[0]
        self.assertEqual(rec.symbol, symbol)
        self.assertEqual(rec.entry_source, "retracement_ob")

    def test_dedup_across_cycles_same_h1_ts(self):
        symbol = "BTC/USDT"
        persistence = FakePersistence()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: retracement_ob_signal(symbol)}),
            persistence=persistence,
        )
        orch.run_cycle()
        orch.run_cycle()  # same h1_last_ts -> deduped
        self.assertEqual(len(persistence.ob_audits), 1)

    def test_stale_signal_is_not_audited(self):
        symbol = "BTC/USDT"
        persistence = FakePersistence()
        # LONG sl=95 with live price 90 -> STALE (fresh setup invalidated)
        signal = retracement_ob_signal(symbol, sl=95.0, live_price=90.0)
        orch = make_orch(
            market=FakeMarketData(
                [symbol], {symbol: make_snapshot(symbol, live_price=90.0)}
            ),
            analyzer=FakeAnalyzer({symbol: signal}),
            persistence=persistence,
        )
        orch.run_cycle()
        self.assertEqual(persistence.ob_audits, [])

    def test_append_failure_is_swallowed_but_deduped(self):
        symbol = "BTC/USDT"
        persistence = FakePersistence(append_error=PersistenceError("disk full"))
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: retracement_ob_signal(symbol)}),
            persistence=persistence,
        )
        orch.run_cycle()  # must not raise; V1 swallows append errors
        self.assertEqual(persistence.ob_audits, [])


class TestCadence(unittest.TestCase):
    def test_fetch_pause_per_symbol(self):
        symbols = ["A/USDT", "B/USDT"]
        snaps = {s: make_snapshot(s) for s in symbols}
        sleeps: list = []
        orch = make_orch(
            market=FakeMarketData(symbols, snaps),
            analyzer=FakeAnalyzer({s: skip_signal(s) for s in symbols}),
            sleeper=lambda s: sleeps.append(s),
        )
        orch.run_cycle()
        self.assertEqual(sleeps.count(0.20), 2)  # one inter-fetch pause per symbol

    def test_empty_universe_sleeps_thirty(self):
        sleeps: list = []
        orch = make_orch(
            market=FakeMarketData([], {}),
            analyzer=FakeAnalyzer({}),
            sleeper=lambda s: sleeps.append(s),
        )
        orch.run_forever(max_cycles=2)
        self.assertEqual(sleeps.count(30.0), 1)  # one empty-universe sleep between


if __name__ == "__main__":
    unittest.main()
