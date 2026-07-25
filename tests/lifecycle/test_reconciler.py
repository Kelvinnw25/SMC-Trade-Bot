"""lifecycle.reconciler — the memory-engine decisions, scenario by scenario.

These pin the individual V1 branches (kill / stale / cooldown / same-thesis /
insert / replace / cap / guard / decay / alert routing) with explicit inputs.
The exhaustive V1 equivalence is in test_lifecycle_equivalence.py.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Optional

from v2.domain import (
    ActiveSignal,
    EvictMutation,
    IncrementMissMutation,
    InsertMutation,
    KilledThesisRecord,
    PartialRefreshMutation,
    ReplaceMutation,
)
from v2.infra.clock import FakeClock
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.memory import MemoryView
from v2.lifecycle.reconciler import Reconciler

from tests.lifecycle.support import CycleSpec, make_signal

NOW = 1_000_000.0


def _clock(epoch: float = NOW) -> FakeClock:
    return FakeClock(datetime.fromtimestamp(epoch, tz=timezone.utc))


def _prior(**kw) -> ActiveSignal:
    base = dict(
        direction="LONG", score=10, decision="VALID", entry_price=100.0,
        sl=95.0, tp=115.0, regime="expansion_up", timestamp=NOW - 10.0,
        entry_mode="RETRACEMENT", alert_sent_tier="", alert_sent_ts=0.0,
        last_miss_h1_ts=0.0, miss_count=0,
    )
    base.update(kw)
    return ActiveSignal(**base)


def _reconcile(spec, *, active=None, killed=None, epoch=NOW, config=None):
    signal, htf = make_signal(spec)
    clock = _clock(epoch)
    rec = Reconciler(clock, config or LifecycleConfig())
    mem = MemoryView(active or {}, killed or {})
    decision = rec.reconcile(
        signal, mem, htf=htf, live_price=spec.live_price, h1_last_ts=spec.h1_last_ts
    )
    return decision


def _log_types(decision):
    return [le.event_type for le in decision.log_events]


def _first_mutation(decision, cls):
    for m in decision.mutations:
        if isinstance(m, cls):
            return m
    return None


def _valid_long(**kw) -> CycleSpec:
    base = dict(
        symbol="BTC", decision="VALID", direction="LONG", entry_mode="RETRACEMENT",
        regime="expansion_up", score=12, entry_ready=True, entry_price=100.0,
        sl=95.0, tp=115.0, entry_source="retracement_ob", tp_source="SWING_HIGH",
        d1_bias="BULLISH", h4_bias="BULLISH", weekly_bias="BULLISH",
        location="discount", atr_last=1.0, live_price=100.0, h1_last_ts=5000.0,
    )
    base.update(kw)
    return CycleSpec(**base)


class TestInsertAndAlert(unittest.TestCase):
    def test_fresh_watch_inserts_without_alert(self):
        d = _reconcile(_valid_long(decision="WATCH"))
        ins = _first_mutation(d, InsertMutation)
        self.assertIsNotNone(ins)
        self.assertEqual(ins.record.decision, "WATCH")
        self.assertEqual(ins.record.miss_count, 0)
        self.assertEqual(ins.record.alert_sent_tier, "")  # WATCH has no tier
        self.assertEqual(d.alerts, ())

    def test_fresh_valid_inserts_and_alerts_new(self):
        d = _reconcile(_valid_long(decision="VALID"))
        ins = _first_mutation(d, InsertMutation)
        self.assertEqual(ins.record.alert_sent_tier, "VALID")
        self.assertEqual(ins.record.alert_sent_ts, NOW)
        self.assertEqual(len(d.alerts), 1)
        self.assertEqual(d.alerts[0].kind, "new")
        self.assertEqual(d.alerts[0].dedup_key, "VALID")
        self.assertIn("ALERTED", _log_types(d))

    def test_record_carries_v1_payload_fields(self):
        d = _reconcile(_valid_long(decision="VALID", h1_last_ts=777.0))
        rec = _first_mutation(d, InsertMutation).record
        self.assertEqual(rec.entry_source, "retracement_ob")
        self.assertEqual(rec.signal_id, "BTC|RETRACEMENT|777.0")
        self.assertEqual(rec.last_miss_h1_ts, 777.0)
        self.assertEqual(rec.status, "WATCH")


class TestKill(unittest.TestCase):
    def test_price_breach_kills_and_records_thesis(self):
        prior = _prior(direction="LONG", sl=95.0, entry_mode="RETRACEMENT")
        # fresh SKIP cycle, live price nukes below sl (atr 0 -> buffer 0)
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction="LONG",
                         entry_mode="RETRACEMENT", has_htf=True, d1_bias="CHOPPY",
                         h4_bias="CHOPPY", live_price=90.0, h1_last_ts=6000.0)
        d = _reconcile(spec, active={"BTC": prior})
        self.assertIn("KILL", _log_types(d))
        ev = _first_mutation(d, EvictMutation)
        self.assertEqual(ev.reason, "kill")
        self.assertEqual(len(d.killed_theses), 1)
        self.assertEqual(d.killed_theses[0].thesis_key, "BTC|LONG|RETRACEMENT")
        self.assertEqual(d.killed_theses[0].killed_ts, NOW)

    def test_htf_flip_kills(self):
        prior = _prior(direction="LONG", sl=95.0)
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction=None,
                         has_htf=True, d1_bias="BEARISH", h4_bias="BEARISH",
                         live_price=999.0, h1_last_ts=6000.0)  # price fine, bias flipped
        d = _reconcile(spec, active={"BTC": prior})
        self.assertIn("KILL", _log_types(d))

    def test_kill_alerts_only_if_previously_alerted(self):
        prior = _prior(alert_sent_tier="VALID", sl=95.0)
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction="LONG",
                         has_htf=True, live_price=90.0, h1_last_ts=6000.0)
        d = _reconcile(spec, active={"BTC": prior})
        self.assertEqual(len(d.alerts), 1)
        self.assertEqual(d.alerts[0].kind, "kill")

    def test_kill_no_alert_when_never_alerted(self):
        prior = _prior(alert_sent_tier="", sl=95.0)
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction="LONG",
                         has_htf=True, live_price=90.0, h1_last_ts=6000.0)
        d = _reconcile(spec, active={"BTC": prior})
        self.assertEqual(d.alerts, ())


class TestStale(unittest.TestCase):
    def test_fresh_setup_invalidated_by_live_price(self):
        # scored LONG whose live price is already below its own sl
        spec = _valid_long(sl=95.0, live_price=90.0, atr_last=0.0)
        d = _reconcile(spec)
        self.assertIn("STALE", _log_types(d))
        self.assertEqual(d.mutations, ())

    def test_not_ready_signal_is_not_stale(self):
        # not-ready: sl is None in the result view -> is_poi_invalidated False
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction="SHORT",
                         entry_mode="RETRACEMENT", entry_ready=False,
                         has_htf=True, live_price=100.0, h1_last_ts=5000.0)
        d = _reconcile(spec)
        self.assertNotIn("STALE", _log_types(d))


class TestCooldown(unittest.TestCase):
    def test_matching_thesis_within_cooldown_blocks_insert(self):
        killed = {"BTC|LONG|RETRACEMENT": KilledThesisRecord("BTC|LONG|RETRACEMENT", NOW - 100.0)}
        d = _reconcile(_valid_long(decision="VALID"), killed=killed,
                       config=LifecycleConfig(kill_cooldown_seconds=3600.0))
        self.assertIn("KILL_COOLDOWN", _log_types(d))
        self.assertEqual(d.mutations, ())
        self.assertEqual(d.alerts, ())

    def test_expired_cooldown_allows_insert(self):
        killed = {"BTC|LONG|RETRACEMENT": KilledThesisRecord("BTC|LONG|RETRACEMENT", NOW - 5000.0)}
        d = _reconcile(_valid_long(decision="VALID"), killed=killed,
                       config=LifecycleConfig(kill_cooldown_seconds=3600.0))
        self.assertNotIn("KILL_COOLDOWN", _log_types(d))
        self.assertIsNotNone(_first_mutation(d, InsertMutation))


class TestSameThesis(unittest.TestCase):
    def test_partial_refresh_keeps_decay_state(self):
        prior = _prior(direction="LONG", entry_mode="RETRACEMENT",
                       alert_sent_tier="VALID", score=8, miss_count=2,
                       last_miss_h1_ts=42.0)
        # same thesis, current tier VALID <= existing VALID
        d = _reconcile(_valid_long(decision="VALID", score=15), active={"BTC": prior})
        self.assertIn("SAME_THESIS", _log_types(d))
        pr = _first_mutation(d, PartialRefreshMutation)
        self.assertEqual(pr.score, 15)  # max(15, 8)
        self.assertEqual(pr.decision, "VALID")  # never downgraded / changed here
        self.assertEqual(d.alerts, ())

    def test_higher_tier_takes_upgrade_path_not_same_thesis(self):
        prior = _prior(direction="LONG", entry_mode="RETRACEMENT",
                       alert_sent_tier="VALID", score=10)
        d = _reconcile(_valid_long(decision="A+", score=16), active={"BTC": prior})
        self.assertNotIn("SAME_THESIS", _log_types(d))
        self.assertEqual(len(d.alerts), 1)
        self.assertEqual(d.alerts[0].kind, "upgrade")
        self.assertEqual(d.alerts[0].new_decision, "A+")
        rec = _first_mutation(d, InsertMutation).record
        self.assertEqual(rec.alert_sent_tier, "A+")


class TestSlotManagement(unittest.TestCase):
    def _full(self):
        return {
            "AAA": _prior(score=5, direction="LONG", entry_mode="RETRACEMENT"),
            "BBB": _prior(score=7, direction="LONG", entry_mode="RETRACEMENT"),
        }

    def test_replace_weakest_when_stronger(self):
        cfg = LifecycleConfig(max_open_positions=2)
        d = _reconcile(_valid_long(symbol="CCC", decision="VALID", score=9),
                       active=self._full(), config=cfg)
        self.assertIn("REPLACE", _log_types(d))
        rep = _first_mutation(d, ReplaceMutation)
        self.assertEqual(rep.target_symbol, "AAA")  # weakest (score 5)
        self.assertEqual(rep.symbol, "CCC")

    def test_capped_when_not_stronger(self):
        cfg = LifecycleConfig(max_open_positions=2)
        d = _reconcile(_valid_long(symbol="CCC", decision="VALID", score=4),
                       active=self._full(), config=cfg)
        self.assertIn("CAPPED", _log_types(d))
        self.assertEqual(d.mutations, ())
        self.assertEqual(d.alerts, ())  # no insert -> no alert


class TestGuardAndDecay(unittest.TestCase):
    def test_guard_on_invalid_levels(self):
        spec = _valid_long(decision="VALID", sl=0.0)  # invalid levels
        d = _reconcile(spec)
        self.assertIn("GUARD", _log_types(d))

    def test_guard_decay_increments_and_evicts_at_limit(self):
        cfg = LifecycleConfig(miss_count_limit=3)
        prior = _prior(miss_count=2, last_miss_h1_ts=1000.0)
        spec = _valid_long(decision="VALID", sl=0.0, h1_last_ts=2000.0)  # new H1
        d = _reconcile(spec, active={"BTC": prior}, config=cfg)
        self.assertIsNotNone(_first_mutation(d, IncrementMissMutation))
        self.assertIn("DECAY", _log_types(d))
        ev = _first_mutation(d, EvictMutation)
        self.assertEqual(ev.reason, "decay")

    def test_skip_decay_increments(self):
        prior = _prior(miss_count=0, last_miss_h1_ts=1000.0)
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction="LONG",
                         entry_mode="RETRACEMENT", entry_ready=False,
                         has_htf=True, d1_bias="BULLISH", h4_bias="BULLISH",
                         live_price=100.0, h1_last_ts=2000.0, reason="low_score")
        d = _reconcile(spec, active={"BTC": prior})
        inc = _first_mutation(d, IncrementMissMutation)
        self.assertIsNotNone(inc)
        self.assertEqual(inc.h1_last_ts, 2000.0)

    def test_h1_gate_closed_no_miss(self):
        prior = _prior(miss_count=1, last_miss_h1_ts=5000.0)
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction="LONG",
                         entry_mode="RETRACEMENT", has_htf=True, d1_bias="BULLISH",
                         h4_bias="BULLISH", live_price=100.0, h1_last_ts=5000.0)
        d = _reconcile(spec, active={"BTC": prior})
        self.assertIsNone(_first_mutation(d, IncrementMissMutation))

    def test_decay_log_uses_retracement_default_for_no_direction(self):
        # no_direction fresh cycle hitting a memory signal: V1 entry_mode falls
        # back to RETRACEMENT (main.py:379); the DECAY log 'mode' must match.
        cfg = LifecycleConfig(miss_count_limit=1)
        prior = _prior(miss_count=0, last_miss_h1_ts=1000.0, decision="WATCH")
        spec = CycleSpec(symbol="BTC", decision="SKIP", direction=None,
                         has_htf=True, d1_bias="BULLISH", h4_bias="CHOPPY",
                         live_price=100.0, h1_last_ts=2000.0)
        d = _reconcile(spec, active={"BTC": prior}, config=cfg)
        decay = next(le for le in d.log_events if le.event_type == "DECAY")
        details = dict(decay.details)
        self.assertEqual(details["mode"], "RETRACEMENT")
        self.assertEqual(details["from_decision"], "WATCH")
        self.assertEqual(details["miss_count"], 1)


class TestSkipNotInMemoryIsNoOp(unittest.TestCase):
    def test_skip_unknown_symbol_produces_nothing(self):
        spec = CycleSpec(symbol="XYZ", decision="SKIP", has_htf=False,
                         live_price=100.0, h1_last_ts=1.0)
        d = _reconcile(spec)
        self.assertEqual(d.mutations, ())
        self.assertEqual(d.alerts, ())
        self.assertEqual(d.log_events, ())


if __name__ == "__main__":
    unittest.main()
