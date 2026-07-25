"""app.orchestrator — the thin scan-loop coordinator (Phase 5.1).

These tests verify coordination only: wiring, startup, the session flag computed
from the injected clock, the fetch→analyze→reconcile→apply→dispatch→persist
order, per-symbol error isolation, and that the orchestrator owns no decision
(everything comes from the injected components).
"""
from __future__ import annotations

import unittest

from v2.app.orchestrator import Orchestrator
from v2.domain import (
    ActiveSignal,
    EvictMutation,
    InsertMutation,
    KilledThesisRecord,
    LifecycleDecision,
)
from v2.infra.market_data import MarketDataError
from v2.infra.notifier import NotifierError
from v2.infra.persistence import PersistenceError
from v2.lifecycle.config import LifecycleConfig

from tests.app.support import (
    FakeAnalyzer,
    FakeMarketData,
    FakeNotifier,
    FakePersistence,
    FakeReconciler,
    make_snapshot,
    sniper_clock,
    strict_hour_clock,
    weekend_clock,
)
from tests.lifecycle.support import CycleSpec, make_signal


# -- signal builders --------------------------------------------------------

def valid_long(symbol: str, **kw):
    base = dict(
        symbol=symbol, decision="VALID", direction="LONG", entry_mode="RETRACEMENT",
        regime="expansion_up", score=12, entry_ready=True, entry_price=100.0,
        sl=95.0, tp=115.0, entry_source="retracement_ob", tp_source="SWING_HIGH",
        d1_bias="BULLISH", h4_bias="BULLISH", weekly_bias="BULLISH",
        location="discount", atr_last=1.0, live_price=100.0, h1_last_ts=5000.0,
    )
    base.update(kw)
    signal, _htf = make_signal(CycleSpec(**base))
    return signal


def skip_signal(symbol: str):
    signal, _ = make_signal(
        CycleSpec(symbol=symbol, decision="SKIP", has_htf=False, live_price=100.0)
    )
    return signal


def veto_short(symbol: str):
    signal, htf = make_signal(
        CycleSpec(
            symbol=symbol, decision="SKIP", veto=True, direction="SHORT",
            regime="expansion_up", d1_bias="BULLISH", h4_bias="BULLISH",
            weekly_bias="BULLISH", live_price=100.0, h1_last_ts=5000.0,
            veto_reason="weekly_bullish_expansion_up_short_veto",
        )
    )
    return signal, htf


def prior_short_alerted() -> ActiveSignal:
    return ActiveSignal(
        direction="SHORT", score=10, decision="VALID", entry_price=100.0,
        sl=105.0, tp=85.0, regime="expansion_up", timestamp=1000.0,
        entry_mode="RETRACEMENT", alert_sent_tier="VALID", alert_sent_ts=900.0,
    )


def make_orch(*, market, analyzer, clock=None, persistence=None, notifier=None,
              reconciler=None, config=None, top_coins_limit=30):
    return Orchestrator(
        clock=clock or sniper_clock(),
        market_data=market,
        persistence=persistence or FakePersistence(),
        notifier=notifier or FakeNotifier(),
        analyzer=analyzer,
        config=config or LifecycleConfig(),
        top_coins_limit=top_coins_limit,
        reconciler=reconciler,
    )


class TestStartup(unittest.TestCase):
    def test_loads_memory_into_store(self):
        prior = prior_short_alerted()
        killed = {"K": KilledThesisRecord("K", 500.0)}
        persistence = FakePersistence(active={"BTC/USDT": prior}, killed=killed)
        orch = make_orch(
            market=FakeMarketData([], {}), analyzer=FakeAnalyzer({}),
            persistence=persistence,
        )
        orch.startup()
        self.assertIn("BTC/USDT", orch.store.active)
        self.assertIn("K", orch.store.killed_theses)

    def test_passes_clock_timestamp_as_default(self):
        persistence = FakePersistence()
        clock = sniper_clock()
        orch = make_orch(
            market=FakeMarketData([], {}), analyzer=FakeAnalyzer({}),
            persistence=persistence, clock=clock,
        )
        orch.startup()
        self.assertEqual(persistence.default_timestamps[-1], clock.timestamp())

    def test_load_failure_starts_empty(self):
        persistence = FakePersistence(load_error=PersistenceError("corrupt"))
        orch = make_orch(
            market=FakeMarketData([], {}), analyzer=FakeAnalyzer({}),
            persistence=persistence,
        )
        orch.startup()
        self.assertEqual(dict(orch.store.active), {})
        self.assertEqual(dict(orch.store.killed_theses), {})


class TestSessionPropagation(unittest.TestCase):
    def _session_seen(self, clock) -> bool:
        analyzer = FakeAnalyzer({"BTC/USDT": skip_signal("BTC/USDT")})
        orch = make_orch(
            market=FakeMarketData(["BTC/USDT"], {"BTC/USDT": make_snapshot("BTC/USDT")}),
            analyzer=analyzer, clock=clock,
        )
        orch.run_cycle()
        return analyzer.calls[0][1]

    def test_sniper_hours_true(self):
        self.assertTrue(self._session_seen(sniper_clock()))

    def test_strict_hours_false(self):
        self.assertFalse(self._session_seen(strict_hour_clock()))

    def test_weekend_false(self):
        self.assertFalse(self._session_seen(weekend_clock()))


class TestScanCycleHappyPath(unittest.TestCase):
    def setUp(self):
        self.symbol = "BTC/USDT"
        self.market = FakeMarketData(
            [self.symbol], {self.symbol: make_snapshot(self.symbol)}
        )
        self.notifier = FakeNotifier()
        self.persistence = FakePersistence()
        self.analyzer = FakeAnalyzer({self.symbol: valid_long(self.symbol)})
        self.orch = make_orch(
            market=self.market, analyzer=self.analyzer,
            notifier=self.notifier, persistence=self.persistence,
        )

    def test_valid_signal_inserts_alerts_and_persists(self):
        self.orch.run_cycle()
        # lifecycle: inserted
        self.assertIn(self.symbol, self.orch.store.active)
        # delivery: a 'new' alert was dispatched
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0].kind, "new")
        # persistence: saved with the inserted signal
        self.assertIn(self.symbol, self.persistence.saved_active)

    def test_market_snapshot_requested_with_m5(self):
        self.orch.run_cycle()
        self.assertEqual(self.market.snapshot_calls, [(self.symbol, True)])


class TestLifecyclePassthrough(unittest.TestCase):
    def test_reconciler_receives_analyzer_signal_verbatim(self):
        symbol = "BTC/USDT"
        signal = valid_long(symbol)
        analyzer = FakeAnalyzer({symbol: signal})
        recon = FakeReconciler()
        snap = make_snapshot(symbol, live_price=101.5, h1_last_ts=4242.0)
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: snap}),
            analyzer=analyzer, reconciler=recon,
        )
        orch.run_cycle()
        self.assertEqual(len(recon.calls), 1)
        call = recon.calls[0]
        self.assertIs(call["signal"], signal)              # verbatim, not rebuilt
        self.assertIs(call["htf"], signal.candidate.htf)   # candidate's fresh HTF
        self.assertEqual(call["live_price"], 101.5)
        self.assertEqual(call["h1_last_ts"], 4242.0)

    def test_orchestrator_applies_the_decision_it_is_given(self):
        symbol = "BTC/USDT"
        rec = ActiveSignal(
            direction="LONG", score=9, decision="VALID", entry_price=100.0,
            sl=95.0, tp=115.0, regime="expansion_up", timestamp=9_999_999_999.0,
            entry_mode="RETRACEMENT",  # far-future ts so end-of-cycle TTL keeps it
        )
        canned = LifecycleDecision((InsertMutation(symbol, rec),))
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: valid_long(symbol)}),
            reconciler=FakeReconciler(canned),
        )
        orch.run_cycle()
        self.assertEqual(orch.store.active[symbol], rec)


class TestEmptyUniverse(unittest.TestCase):
    def test_empty_universe_does_nothing(self):
        market = FakeMarketData([], {})
        persistence = FakePersistence()
        orch = make_orch(market=market, analyzer=FakeAnalyzer({}), persistence=persistence)
        orch.run_cycle()
        self.assertEqual(market.snapshot_calls, [])
        self.assertEqual(persistence.save_calls, 0)

    def test_universe_error_is_swallowed(self):
        market = FakeMarketData(
            ["X"], {}, universe_error=MarketDataError("network down")
        )
        persistence = FakePersistence()
        orch = make_orch(market=market, analyzer=FakeAnalyzer({}), persistence=persistence)
        orch.run_cycle()  # must not raise
        self.assertEqual(market.snapshot_calls, [])
        self.assertEqual(persistence.save_calls, 0)


class TestPerSymbolIsolation(unittest.TestCase):
    def test_one_symbol_error_does_not_stop_the_scan(self):
        good, bad = "GOOD/USDT", "BAD/USDT"
        market = FakeMarketData(
            [bad, good],
            {bad: make_snapshot(bad), good: make_snapshot(good)},
        )
        analyzer = FakeAnalyzer(
            {bad: RuntimeError("boom"), good: valid_long(good)}
        )
        persistence = FakePersistence()
        orch = make_orch(market=market, analyzer=analyzer, persistence=persistence)
        orch.run_cycle()
        # the good symbol still processed and persisted; the cycle completed
        self.assertIn(good, orch.store.active)
        self.assertNotIn(bad, orch.store.active)
        self.assertEqual(persistence.save_calls, 1)

    def test_none_snapshot_skips_symbol(self):
        symbol = "BTC/USDT"
        market = FakeMarketData([symbol], {symbol: None})
        analyzer = FakeAnalyzer({symbol: valid_long(symbol)})
        orch = make_orch(market=market, analyzer=analyzer)
        orch.run_cycle()
        self.assertEqual(analyzer.calls, [])  # analysis never ran
        self.assertEqual(dict(orch.store.active), {})

    def test_insufficient_data_skips_symbol(self):
        symbol = "BTC/USDT"
        market = FakeMarketData(
            [symbol], {symbol: make_snapshot(symbol, d1_rows=5, h1_rows=60)}
        )
        analyzer = FakeAnalyzer({symbol: valid_long(symbol)})
        orch = make_orch(market=market, analyzer=analyzer)
        orch.run_cycle()
        self.assertEqual(analyzer.calls, [])

    def test_market_error_on_snapshot_skips_symbol(self):
        good, bad = "GOOD/USDT", "BAD/USDT"
        market = FakeMarketData(
            [bad, good],
            {bad: MarketDataError("fetch failed"), good: make_snapshot(good)},
        )
        analyzer = FakeAnalyzer({good: valid_long(good)})
        orch = make_orch(market=market, analyzer=analyzer)
        orch.run_cycle()
        self.assertIn(good, orch.store.active)


class TestNotifierResilience(unittest.TestCase):
    def test_notifier_failure_is_swallowed_and_state_kept(self):
        symbol = "BTC/USDT"
        notifier = FakeNotifier(error=NotifierError("telegram down"))
        persistence = FakePersistence()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: valid_long(symbol)}),
            notifier=notifier, persistence=persistence,
        )
        orch.run_cycle()  # must not raise
        self.assertEqual(len(notifier.sent), 1)              # attempted
        self.assertIn(symbol, orch.store.active)             # state kept
        # V1 marks alerted regardless of send success
        self.assertEqual(orch.store.active[symbol].alert_sent_tier, "VALID")
        self.assertEqual(persistence.save_calls, 1)


class TestPersistenceResilience(unittest.TestCase):
    def test_save_failure_is_swallowed(self):
        symbol = "BTC/USDT"
        persistence = FakePersistence(save_error=PersistenceError("disk full"))
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: valid_long(symbol)}),
            persistence=persistence,
        )
        orch.run_cycle()  # must not raise
        self.assertIn(symbol, orch.store.active)


class TestVetoKillUsesFreshHtf(unittest.TestCase):
    def test_veto_cycle_recovers_htf_and_kills_prior(self):
        symbol = "ETH/USDT"
        signal, htf = veto_short(symbol)  # candidate None, direction SHORT
        analyzer = FakeAnalyzer(
            {symbol: signal}, htf_by_symbol={symbol: htf}  # pipeline provides htf
        )
        persistence = FakePersistence(active={symbol: prior_short_alerted()})
        notifier = FakeNotifier()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=analyzer, persistence=persistence, notifier=notifier,
        )
        orch.run_cycle()
        # the fresh HTF (D1&H4 BULLISH) flips against the prior SHORT -> KILL
        self.assertNotIn(symbol, orch.store.active)
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0].kind, "kill")
        self.assertEqual(analyzer.pipeline.runs, [symbol])  # recovered via pipeline
        # the killed thesis was recorded and persisted
        self.assertIn(
            "ETH/USDT|SHORT|RETRACEMENT", dict(orch.store.killed_theses)
        )


class TestEndOfCycleMaintenance(unittest.TestCase):
    def test_ttl_sweep_evicts_and_prune_runs(self):
        symbol = "OLD/USDT"
        cfg = LifecycleConfig(untapped_ttl_seconds=100.0, kill_cooldown_seconds=100.0)
        # prior aged well beyond TTL; the clock is far ahead of its timestamp
        prior = ActiveSignal(
            direction="LONG", score=5, decision="WATCH", entry_price=100.0,
            sl=95.0, tp=115.0, regime="expansion_up", timestamp=0.0,
            entry_mode="RETRACEMENT",
        )
        killed_old = {"K|x|y": KilledThesisRecord("K|x|y", 0.0)}
        persistence = FakePersistence(active={symbol: prior}, killed=killed_old)
        # SKIP this cycle so nothing re-inserts; TTL should evict the aged prior
        analyzer = FakeAnalyzer({symbol: skip_signal(symbol)})
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=analyzer, persistence=persistence, config=cfg,
        )
        orch.run_cycle()
        self.assertEqual(dict(orch.store.active), {})            # TTL evicted
        self.assertEqual(persistence.saved_active, {})            # saved empty
        self.assertEqual(persistence.saved_killed, {})            # pruned killed


class TestRunForever(unittest.TestCase):
    def test_bounded_loop_runs_cycles_and_sleeps_between(self):
        symbol = "BTC/USDT"
        market = FakeMarketData([symbol], {symbol: make_snapshot(symbol)})
        persistence = FakePersistence()
        orch = make_orch(
            market=market, analyzer=FakeAnalyzer({symbol: skip_signal(symbol)}),
            persistence=persistence,
        )
        sleeps: list = []
        orch.run_forever(sleeper=lambda s: sleeps.append(s), max_cycles=3)
        self.assertEqual(persistence.save_calls, 3)   # 3 cycles
        self.assertEqual(len(sleeps), 2)              # sleeps between, not after last
        self.assertEqual(persistence.load_calls, 1)   # startup once


class TestOwnsNoBusinessLogic(unittest.TestCase):
    def test_skip_signal_produces_no_insert(self):
        # The orchestrator must not fabricate a decision: a SKIP analyzer result
        # (with an empty store) yields no memory change and no alert.
        symbol = "BTC/USDT"
        notifier = FakeNotifier()
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: skip_signal(symbol)}),
            notifier=notifier,
        )
        orch.run_cycle()
        self.assertEqual(dict(orch.store.active), {})
        self.assertEqual(notifier.sent, [])

    def test_decision_is_the_reconcilers_not_the_orchestrators(self):
        # With a spy reconciler returning an EMPTY decision even for a VALID
        # signal, the store stays empty — the orchestrator adds nothing of its own.
        symbol = "BTC/USDT"
        orch = make_orch(
            market=FakeMarketData([symbol], {symbol: make_snapshot(symbol)}),
            analyzer=FakeAnalyzer({symbol: valid_long(symbol)}),
            reconciler=FakeReconciler(LifecycleDecision()),
        )
        orch.run_cycle()
        self.assertEqual(dict(orch.store.active), {})


if __name__ == "__main__":
    unittest.main()
