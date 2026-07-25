"""lifecycle.store — the single-writer memory: mutation application, TTL, prune."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from v2.domain import (
    ActiveSignal,
    EvictMutation,
    IncrementMissMutation,
    InsertMutation,
    KilledThesisRecord,
    LifecycleDecision,
    PartialRefreshMutation,
    ReplaceMutation,
)
from v2.infra.clock import FakeClock
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.store import SignalStore


def _signal(**kw) -> ActiveSignal:
    base = dict(
        direction="LONG", score=10, decision="VALID", entry_price=100.0,
        sl=95.0, tp=115.0, regime="expansion_up", timestamp=1000.0,
        entry_mode="RETRACEMENT",
    )
    base.update(kw)
    return ActiveSignal(**base)


def _clock(epoch: float) -> FakeClock:
    return FakeClock(datetime.fromtimestamp(epoch, tz=timezone.utc))


class TestApplyMutations(unittest.TestCase):
    def test_insert(self):
        store = SignalStore()
        rec = _signal()
        store.apply(LifecycleDecision((InsertMutation("BTC", rec),)))
        self.assertEqual(store.active["BTC"], rec)

    def test_replace_drops_target_and_inserts(self):
        store = SignalStore(active={"OLD": _signal(score=5)})
        rec = _signal(score=20)
        store.apply(LifecycleDecision((ReplaceMutation("OLD", "NEW", rec),)))
        self.assertNotIn("OLD", store.active)
        self.assertEqual(store.active["NEW"], rec)

    def test_partial_refresh_keeps_miss_and_last_miss(self):
        store = SignalStore(active={"BTC": _signal(miss_count=2, last_miss_h1_ts=50.0, score=8)})
        store.apply(LifecycleDecision((
            PartialRefreshMutation("BTC", score=12, decision="VALID",
                                   entry_price=101.0, sl=96.0, tp=120.0,
                                   regime="rotational", timestamp=2000.0),
        )))
        got = store.active["BTC"]
        self.assertEqual(got.score, 12)
        self.assertEqual(got.entry_price, 101.0)
        self.assertEqual(got.timestamp, 2000.0)
        # unchanged (Decision #010: same-thesis keeps decay state)
        self.assertEqual(got.miss_count, 2)
        self.assertEqual(got.last_miss_h1_ts, 50.0)

    def test_increment_miss(self):
        store = SignalStore(active={"BTC": _signal(miss_count=1, last_miss_h1_ts=10.0)})
        store.apply(LifecycleDecision((IncrementMissMutation("BTC", 99.0),)))
        got = store.active["BTC"]
        self.assertEqual(got.miss_count, 2)
        self.assertEqual(got.last_miss_h1_ts, 99.0)

    def test_evict(self):
        store = SignalStore(active={"BTC": _signal()})
        store.apply(LifecycleDecision((EvictMutation("BTC", "kill"),)))
        self.assertNotIn("BTC", store.active)

    def test_killed_theses_recorded(self):
        store = SignalStore()
        rec = KilledThesisRecord("BTC|LONG|RETRACEMENT", 500.0)
        store.apply(LifecycleDecision((), (), (), (rec,)))
        self.assertEqual(store.killed_theses["BTC|LONG|RETRACEMENT"].killed_ts, 500.0)

    def test_increment_then_evict_in_order(self):
        store = SignalStore(active={"BTC": _signal(miss_count=2, last_miss_h1_ts=10.0)})
        store.apply(LifecycleDecision((
            IncrementMissMutation("BTC", 99.0),
            EvictMutation("BTC", "decay"),
        )))
        self.assertNotIn("BTC", store.active)


class TestViewIsolation(unittest.TestCase):
    def test_view_is_a_snapshot(self):
        store = SignalStore(active={"BTC": _signal()})
        view = store.view()
        store.apply(LifecycleDecision((EvictMutation("BTC", "kill"),)))
        # the previously-handed-out view is unaffected
        self.assertIn("BTC", view)
        self.assertNotIn("BTC", store.active)

    def test_active_property_is_a_copy(self):
        store = SignalStore(active={"BTC": _signal()})
        snapshot = store.active
        snapshot["ETH"] = _signal()
        self.assertNotIn("ETH", store.active)


class TestTTLSweep(unittest.TestCase):
    def test_untapped_expiry(self):
        cfg = LifecycleConfig(untapped_ttl_seconds=100.0)
        store = SignalStore(active={"BTC": _signal(timestamp=0.0, miss_count=0)}, config=cfg)
        decision = store.ttl_sweep(_clock(150.0))
        self.assertEqual(len(decision.mutations), 1)
        self.assertIsInstance(decision.mutations[0], EvictMutation)
        self.assertEqual(decision.mutations[0].reason, "ttl")
        self.assertEqual(decision.log_events[0].event_type, "TTL")

    def test_tapped_uses_shorter_ttl(self):
        cfg = LifecycleConfig(untapped_ttl_seconds=1000.0, tapped_ttl_seconds=100.0)
        store = SignalStore(active={"BTC": _signal(timestamp=0.0, miss_count=1)}, config=cfg)
        # age 150 > tapped 100 but < untapped 1000 -> expires only because tapped
        decision = store.ttl_sweep(_clock(150.0))
        self.assertEqual(len(decision.mutations), 1)

    def test_not_expired_when_young(self):
        cfg = LifecycleConfig(untapped_ttl_seconds=1000.0)
        store = SignalStore(active={"BTC": _signal(timestamp=0.0)}, config=cfg)
        decision = store.ttl_sweep(_clock(150.0))
        self.assertEqual(decision.mutations, ())

    def test_sweep_is_pure_until_applied(self):
        cfg = LifecycleConfig(untapped_ttl_seconds=100.0)
        store = SignalStore(active={"BTC": _signal(timestamp=0.0)}, config=cfg)
        decision = store.ttl_sweep(_clock(150.0))
        self.assertIn("BTC", store.active)  # not yet mutated
        store.apply(decision)
        self.assertNotIn("BTC", store.active)


class TestPruneKilled(unittest.TestCase):
    def test_prune_drops_expired_and_keeps_fresh(self):
        cfg = LifecycleConfig(kill_cooldown_seconds=100.0)
        store = SignalStore(
            killed_theses={
                "OLD": KilledThesisRecord("OLD", 0.0),
                "NEW": KilledThesisRecord("NEW", 120.0),
            },
            config=cfg,
        )
        store.prune_killed_theses(_clock(150.0))  # now-old=150>=100 drop; now-new=30<100 keep
        self.assertNotIn("OLD", store.killed_theses)
        self.assertIn("NEW", store.killed_theses)


if __name__ == "__main__":
    unittest.main()
