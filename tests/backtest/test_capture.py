"""M9 — §15 lifecycle capture, and the C-9.2 trade builder.

The scenarios below drive the **production** `Reconciler` and `SignalStore`
through a `RecordingReconciler`, in the orchestrator's own call order, so what is
captured is what the real lifecycle actually emitted rather than a decision some
test hand-assembled. `_Cycle` is that harness and nothing more: it reproduces the
invocation order of `_process_symbol` / `_end_of_cycle` and owns no rule.

Coverage follows the milestone's task list — insert, upgrade, replace, kill,
decay, ttl, supersede, refresh, multiple symbols, multiple runs, empty replay,
duplicate protection, `TradeIntent` reconstruction, Outcome Engine integration —
plus §17.8's completeness assertion: every memory removal is attributed, and the
C-15.5 residue equals the TTL log count.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backtest.capture_schema import (
    CAPTURE_SCHEMA_VERSION,
    CapturedEvent,
    FIELD_ORDER,
    KIND_ALERT,
    KIND_KILLED_THESIS,
    KIND_LOG,
    KIND_MUTATION,
    LIFECYCLE_EVENTS_FILENAME,
    MEMORY_SNAPSHOTS_FILENAME,
    MemorySnapshot,
    read_events,
    read_snapshots,
)
from backtest.outcome import OutcomeConfig, OutcomeEngine, TradeIntentBuilder
from backtest.outcome.builder import TradeSourceError, build_trade_intents
from backtest.replay.capture import (
    CaptureError,
    CaptureRecorder,
    RecordingReconciler,
    TTL_RENDERED_MARKER,
)
from backtest.replay.sinks import RecordingNotifier, ReplayPersistence
from tests.lifecycle.support import CycleSpec, make_signal
from v2.app.rendering import render_log_event
from v2.infra.clock import FakeClock
from v2.lifecycle import LifecycleConfig, Reconciler, SignalStore

HOUR = 3600
T0 = 1699999200          # on the hour; T0 % 3600 == 0


def valid(symbol: str, *, score: int = 12, direction: str = "LONG",
          entry_mode: str = "RETRACEMENT", decision: str = "VALID",
          entry: float = 100.0, sl: float = 95.0, tp: float = 115.0,
          live: float = 101.0, h1_last_ts: float = 0.0,
          d1_bias: str = "BULLISH", h4_bias: str = "BULLISH") -> CycleSpec:
    """An actionable, alert-bearing setup with sane levels."""
    return CycleSpec(
        symbol=symbol, decision=decision, direction=direction,
        entry_mode=entry_mode, regime="expansion_up", score=score,
        entry_ready=True, entry_price=entry, sl=sl, tp=tp,
        entry_source="retracement_ob", tp_source="EQH_cluster",
        d1_bias=d1_bias, h4_bias=h4_bias, atr_last=1.0,
        live_price=live, h1_last_ts=h1_last_ts,
    )


def skip(symbol: str, *, live: float = 101.0, h1_last_ts: float = 0.0,
         d1_bias: str = "BULLISH", h4_bias: str = "BULLISH") -> CycleSpec:
    """A SKIP that leaves an existing record alone except for decay."""
    return CycleSpec(
        symbol=symbol, decision="SKIP", direction="LONG",
        entry_mode="RETRACEMENT", regime="rotational", score=4,
        entry_ready=False, d1_bias=d1_bias, h4_bias=h4_bias, atr_last=1.0,
        live_price=live, h1_last_ts=h1_last_ts,
    )


class _Cycle:
    """A faithful miniature of the orchestrator's cycle, for capture tests.

    Reproduces exactly the order of `_process_symbol` and `_end_of_cycle`
    ([orchestrator.py:249-403](v2/app/orchestrator.py#L249)): reconcile, apply,
    render, dispatch — then TTL sweep, apply, TTL logs, prune, persist. It holds
    no lifecycle rule of its own; every decision comes from the production
    `Reconciler`.
    """

    def __init__(self, run_dir: Path, *, config: LifecycleConfig | None = None,
                 run_id: str = "replay-test", capture_console: bool = True):
        self.config = config or LifecycleConfig()
        self.clock = FakeClock(
            datetime.fromtimestamp(T0 + 1, tz=timezone.utc))
        self.recorder = CaptureRecorder(
            run_dir, run_id=run_id, source="replay",
            capture_console=capture_console)
        self.persistence = ReplayPersistence(run_dir, recorder=self.recorder)
        self.notifier = RecordingNotifier(run_dir, recorder=self.recorder)
        self.store = SignalStore(config=self.config)
        self.reconciler = RecordingReconciler(
            Reconciler(self.clock, self.config), self.recorder)
        self.index = -1

    @property
    def boundary(self) -> int:
        return T0 + self.index * HOUR

    def run(self, specs, *, universe=None, processed: bool = True):
        """One cycle over `specs`, in universe order."""
        self.index += 1
        if self.index > 0:
            self.clock.advance(float(HOUR))
        T = self.boundary
        self.recorder.begin_cycle(self.index, T)
        symbols = tuple(spec.symbol for spec in specs)
        for spec in specs:
            spec = CycleSpec(**{**spec.__dict__, "h1_last_ts": float(T - HOUR)})
            signal, htf = make_signal(spec)
            kill_htf = htf if signal.direction is not None else None
            decision = self.reconciler.reconcile(
                signal, self.store.view(), htf=kill_htf,
                live_price=spec.live_price, h1_last_ts=spec.h1_last_ts)
            self.store.apply(decision)
            for event in decision.log_events:
                self.recorder.record_writer_line(render_log_event(event))
            for alert in decision.alerts:
                self.notifier.send(alert)
        if processed:
            ttl = self.store.ttl_sweep(self.clock)
            self.store.apply(ttl)
            for event in ttl.log_events:
                self.recorder.record_writer_line(render_log_event(event))
            self.store.prune_killed_theses(self.clock)
            self.persistence.save_active_signals(self.store.active)
            self.persistence.save_killed_theses(self.store.killed_theses)
        return self.recorder.end_cycle(
            processed, universe=universe if universe is not None else symbols)


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.run_dir = Path(self._tmp.name) / "runs" / "replay-test"
        self.run_dir.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def events(self):
        return read_events(self.run_dir / LIFECYCLE_EVENTS_FILENAME)

    def snapshots(self):
        return read_snapshots(self.run_dir / MEMORY_SNAPSHOTS_FILENAME)

    def write_manifest(self, burn_in_until: int = 0):
        (self.run_dir / "manifest.json").write_text(
            json.dumps({"burn_in_until": burn_in_until}), encoding="utf-8")

    def build(self, **kwargs):
        self.write_manifest(kwargs.pop("burn_in_until", 0))
        return build_trade_intents(self.run_dir, **kwargs)


# ==========================================================================
# The wire format (C-15.3 / C-15.4)
# ==========================================================================

class TestSchema(unittest.TestCase):

    def test_the_field_list_is_exactly_the_contracts(self):
        """C-15.3 names 21 fields. The set is closed: adding one is a schema
        change and must bump `schema_version` (C-16.5)."""
        self.assertEqual(FIELD_ORDER, (
            "run_id", "source", "cycle_index", "scan_T", "h1_last_ts", "seq",
            "symbol", "universe_rank", "phase", "kind", "mutation_type",
            "mutation_payload", "evict_reason", "target_symbol",
            "log_event_type", "log_details", "alert_kind", "alert_tier",
            "alert_payload", "attribution", "schema_version",
        ))
        self.assertEqual(CAPTURE_SCHEMA_VERSION, 1)

    def test_an_event_round_trips_through_json(self):
        event = CapturedEvent(
            run_id="r", source="replay", cycle_index=3, scan_T=T0,
            h1_last_ts=float(T0 - HOUR), seq=7, symbol="BTC/USDT",
            universe_rank=1, phase="symbol", kind=KIND_MUTATION,
            mutation_type="EvictMutation",
            mutation_payload={"symbol": "BTC/USDT", "reason": "kill"},
            evict_reason="kill")
        again = CapturedEvent.from_dict(json.loads(event.to_json_line()))
        self.assertEqual(again, event)

    def test_an_unknown_key_is_ignored_and_a_missing_one_defaults(self):
        """C-16.5 forward/backward compatibility, on this file too."""
        event = CapturedEvent.from_dict({
            "run_id": "r", "source": "replay", "cycle_index": 0, "scan_T": T0,
            "h1_last_ts": 0.0, "seq": 0, "symbol": None,
            "universe_rank": None, "phase": "end_of_cycle", "kind": KIND_LOG,
            "a_field_from_the_future": 1,
        })
        self.assertEqual(event.attribution, "reconciler")
        self.assertEqual(event.schema_version, CAPTURE_SCHEMA_VERSION)

    def test_lines_are_byte_stable_regardless_of_construction_order(self):
        left = CapturedEvent(
            run_id="r", source="replay", cycle_index=0, scan_T=T0,
            h1_last_ts=0.0, seq=0, symbol="X", universe_rank=None,
            phase="symbol", kind=KIND_LOG, log_event_type="CAPPED",
            log_details={"score": 9, "weakest_score": 11})
        right = CapturedEvent(
            run_id="r", source="replay", cycle_index=0, scan_T=T0,
            h1_last_ts=0.0, seq=0, symbol="X", universe_rank=None,
            phase="symbol", kind=KIND_LOG,
            log_details={"weakest_score": 11, "score": 9},
            log_event_type="CAPPED")
        self.assertEqual(left.to_json_line(), right.to_json_line())

    def test_a_snapshot_round_trips(self):
        snapshot = MemorySnapshot(
            run_id="r", source="replay", cycle_index=1, scan_T=T0,
            active={"BTC/USDT": {"direction": "LONG"}},
            killed={"BTC/USDT|LONG|RETRACEMENT": 1.5})
        again = MemorySnapshot.from_dict(json.loads(snapshot.to_json_line()))
        self.assertEqual(again, snapshot)

    def test_reading_an_absent_file_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_events(Path(tmp) / "nope.jsonl"), ())
            self.assertEqual(read_snapshots(Path(tmp) / "nope.jsonl"), ())


# ==========================================================================
# The observer changes nothing (C-15.2)
# ==========================================================================

class TestRecordingReconciler(CaptureTestCase):

    def test_it_returns_the_inner_decision_object_unchanged(self):
        """Not a copy — the same instance. A copy would be indistinguishable
        today and a place for drift to hide tomorrow."""
        recorder = CaptureRecorder(self.run_dir, run_id="r")
        recorder.begin_cycle(0, T0)
        inner = Reconciler(FakeClock(datetime.fromtimestamp(T0 + 1, tz=timezone.utc)))
        wrapper = RecordingReconciler(inner, recorder)
        signal, htf = make_signal(valid("BTC/USDT", h1_last_ts=float(T0 - HOUR)))
        store = SignalStore()

        expected = inner.reconcile(
            signal, store.view(), htf=htf, live_price=101.0,
            h1_last_ts=float(T0 - HOUR))
        actual = wrapper.reconcile(
            signal, store.view(), htf=htf, live_price=101.0,
            h1_last_ts=float(T0 - HOUR))
        self.assertEqual(actual, expected)

    def test_its_signature_matches_the_production_reconcilers(self):
        import inspect
        self.assertEqual(
            inspect.signature(RecordingReconciler.reconcile).parameters.keys(),
            inspect.signature(Reconciler.reconcile).parameters.keys())

    def test_it_records_before_returning(self):
        """C-15.2: everything after `reconcile` can raise and be swallowed, so a
        decision must be captured before control leaves the wrapper."""
        seen = []

        class _Spy(CaptureRecorder):
            def record_decision(self, symbol, h1_last_ts, decision, *,
                                memory_size=0):
                seen.append(symbol)
                super().record_decision(symbol, h1_last_ts, decision,
                                        memory_size=memory_size)

        recorder = _Spy(self.run_dir, run_id="r")
        recorder.begin_cycle(0, T0)
        clock = FakeClock(datetime.fromtimestamp(T0 + 1, tz=timezone.utc))
        wrapper = RecordingReconciler(Reconciler(clock), recorder)
        signal, htf = make_signal(valid("BTC/USDT", h1_last_ts=float(T0 - HOUR)))
        wrapper.reconcile(signal, SignalStore().view(), htf=htf,
                          live_price=101.0, h1_last_ts=float(T0 - HOUR))
        self.assertEqual(seen, ["BTC/USDT"])

    def test_recording_outside_a_cycle_bracket_is_refused(self):
        recorder = CaptureRecorder(self.run_dir, run_id="r")
        clock = FakeClock(datetime.fromtimestamp(T0 + 1, tz=timezone.utc))
        wrapper = RecordingReconciler(Reconciler(clock), recorder)
        signal, htf = make_signal(valid("BTC/USDT", h1_last_ts=float(T0 - HOUR)))
        with self.assertRaises(CaptureError):
            wrapper.reconcile(signal, SignalStore().view(), htf=htf,
                              live_price=101.0, h1_last_ts=float(T0 - HOUR))

    def test_end_cycle_without_begin_cycle_is_refused(self):
        recorder = CaptureRecorder(self.run_dir, run_id="r")
        with self.assertRaises(CaptureError):
            recorder.end_cycle(True)


# ==========================================================================
# One decision, one line per emission
# ==========================================================================

class TestExpansion(CaptureTestCase):

    def test_an_alerted_insert_captures_mutation_alert_and_logs(self):
        cycle = _Cycle(self.run_dir)
        capture = cycle.run([valid("BTC/USDT")])

        events = self.events()
        self.assertEqual(capture.decisions, 1)
        kinds = [event.kind for event in events]
        self.assertEqual(kinds, [KIND_MUTATION, KIND_ALERT, KIND_LOG])
        insert, alert, log = events
        self.assertEqual(insert.mutation_type, "InsertMutation")
        self.assertEqual(insert.mutation_payload["record"]["direction"], "LONG")
        self.assertEqual(insert.mutation_payload["record"]["entry_price"], 100.0)
        self.assertEqual(alert.alert_kind, "new")
        self.assertEqual(alert.alert_tier, "VALID")
        self.assertEqual(alert.alert_payload["tp_source"], "EQH_cluster")
        self.assertEqual(log.log_event_type, "ALERTED")
        self.assertEqual(log.log_details, {"tier": "VALID"})

    def test_the_tuple_order_of_the_decision_is_the_line_order(self):
        """C-15.3 states the order twice and the two statements disagree; the
        cross-reference to `LifecycleDecision`'s tuple order is the verifiable
        one, so mutations precede alerts precede logs. Pinned here rather than
        left to whoever reads the clause next — see the M9 findings."""
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        kinds = [event.kind for event in self.events()]
        self.assertLess(kinds.index(KIND_MUTATION), kinds.index(KIND_ALERT))
        self.assertLess(kinds.index(KIND_ALERT), kinds.index(KIND_LOG))

    def test_a_decision_with_no_content_still_produces_one_line(self):
        """C-15.3: "reconcile ran and decided nothing" must stay distinguishable
        from "reconcile never ran" — C-15.8's `decisions` count depends on it."""
        cycle = _Cycle(self.run_dir)
        capture = cycle.run([skip("BTC/USDT")])
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, KIND_LOG)
        self.assertIsNone(events[0].log_event_type)
        self.assertIsNone(events[0].mutation_type)
        self.assertEqual(capture.decisions, 1)

    def test_seq_is_zero_based_and_monotonic_across_symbols(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT"), valid("ETH/USDT", score=11)])
        seqs = [event.seq for event in self.events()]
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_seq_restarts_each_cycle(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("ETH/USDT")])
        first = [e.seq for e in self.events() if e.cycle_index == 0]
        second = [e.seq for e in self.events() if e.cycle_index == 1]
        self.assertEqual(min(first), 0)
        self.assertEqual(min(second), 0)

    def test_universe_rank_comes_from_the_universe_not_the_call_order(self):
        """C-15.2.1. `SOL/USDT` is served but never reconciled — a symbol the
        adapter skipped — so call order would rank `ETH/USDT` second."""
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT"), valid("ETH/USDT", score=11)],
                  universe=("BTC/USDT", "SOL/USDT", "ETH/USDT"))
        ranks = {event.symbol: event.universe_rank for event in self.events()}
        self.assertEqual(ranks["BTC/USDT"], 1)
        self.assertEqual(ranks["ETH/USDT"], 3)

    def test_a_symbol_outside_the_universe_has_a_null_rank(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")], universe=("ETH/USDT",))
        self.assertIsNone(self.events()[0].universe_rank)

    def test_the_killed_thesis_record_is_its_own_line(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        # kill it: price beyond SL plus the ATR buffer, and both biases flipped.
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        killed = [e for e in self.events() if e.kind == KIND_KILLED_THESIS]
        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0].mutation_payload["thesis_key"],
                         "BTC/USDT|LONG|RETRACEMENT")

    def test_every_event_carries_the_run_identity(self):
        cycle = _Cycle(self.run_dir, run_id="replay-abc123")
        cycle.run([valid("BTC/USDT")])
        for event in self.events():
            self.assertEqual(event.run_id, "replay-abc123")
            self.assertEqual(event.source, "replay")
            self.assertEqual(event.schema_version, CAPTURE_SCHEMA_VERSION)


# ==========================================================================
# Each lifecycle transition (§17.8)
# ==========================================================================

class TestTransitions(CaptureTestCase):

    def log_types(self):
        return [e.log_event_type for e in self.events()
                if e.log_event_type is not None]

    def test_upgrade_captures_an_upgrade_alert(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", decision="VALID", score=10)])
        cycle.run([valid("BTC/USDT", decision="A+", score=15)])
        alerts = [e for e in self.events() if e.kind == KIND_ALERT]
        self.assertEqual([a.alert_kind for a in alerts], ["new", "upgrade"])
        self.assertEqual(alerts[1].alert_tier, "A+")

    def test_same_thesis_refresh_captures_a_partial_refresh(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", score=10)])
        cycle.run([valid("BTC/USDT", score=13)])   # same tier -> SAME_THESIS
        self.assertIn("SAME_THESIS", self.log_types())
        refresh = [e for e in self.events()
                   if e.mutation_type == "PartialRefreshMutation"]
        self.assertEqual(len(refresh), 1)
        self.assertEqual(refresh[0].mutation_payload["score"], 13)

    def test_kill_captures_the_eviction_the_log_and_the_alert(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        evictions = [e for e in self.events() if e.evict_reason == "kill"]
        self.assertEqual(len(evictions), 1)
        self.assertEqual(evictions[0].symbol, "BTC/USDT")
        self.assertIn("KILL", self.log_types())
        self.assertIn("kill", [e.alert_kind for e in self.events()
                               if e.kind == KIND_ALERT])

    def test_decay_captures_three_misses_then_an_eviction(self):
        cycle = _Cycle(self.run_dir, config=LifecycleConfig(miss_count_limit=3))
        cycle.run([valid("BTC/USDT")])
        for _ in range(3):
            cycle.run([skip("BTC/USDT")])
        misses = [e for e in self.events()
                  if e.mutation_type == "IncrementMissMutation"]
        self.assertEqual(len(misses), 3)
        self.assertIn("DECAY", self.log_types())
        self.assertEqual([e.evict_reason for e in self.events()
                          if e.evict_reason == "decay"], ["decay"])

    def test_replace_captures_the_target_symbol(self):
        config = LifecycleConfig(max_open_positions=1)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=10)])
        cycle.run([valid("ETH/USDT", score=15)])
        replaces = [e for e in self.events()
                    if e.mutation_type == "ReplaceMutation"]
        self.assertEqual(len(replaces), 1)
        self.assertEqual(replaces[0].symbol, "ETH/USDT")
        self.assertEqual(replaces[0].target_symbol, "BTC/USDT")
        self.assertIn("REPLACE", self.log_types())

    def test_capped_captures_a_log_with_no_mutation(self):
        config = LifecycleConfig(max_open_positions=1)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=15)])
        cycle.run([valid("ETH/USDT", score=9)])
        capped = [e for e in self.events() if e.log_event_type == "CAPPED"]
        self.assertEqual(len(capped), 1)
        eth = [e for e in self.events()
               if e.symbol == "ETH/USDT" and e.kind == KIND_MUTATION]
        self.assertEqual(eth, [])

    def test_kill_cooldown_captures_a_log_with_no_mutation(self):
        """Exercised with a 2 h cooldown, because the shipped 1 h value cannot
        fire at H1 replay cadence at all — see the test below."""
        cycle = _Cycle(self.run_dir,
                       config=LifecycleConfig(kill_cooldown_seconds=2.0 * HOUR))
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])                    # KILL
        cycle.run([valid("BTC/USDT")])                           # blocked
        cooldown = [e for e in self.events()
                    if e.log_event_type == "KILL_COOLDOWN"]
        self.assertEqual(len(cooldown), 1)
        self.assertEqual(cooldown[0].log_details["direction"], "LONG")
        self.assertEqual(cooldown[0].log_details["entry_mode"], "RETRACEMENT")
        eviction_free = [e for e in self.events()
                         if e.cycle_index == 2 and e.kind == KIND_MUTATION]
        self.assertEqual(eviction_free, [])

    def test_the_shipped_kill_cooldown_can_never_fire_at_h1_cadence(self):
        """A capture-layer observation, not a capture-layer bug.

        A kill in cycle `k` stamps `killed_ts = T_k + 1` (C-5.2: the clock does
        not move within a cycle). The next scan reads `T_k + 3601`, so the age is
        exactly `kill_cooldown_seconds = 3600`, and the gate is a strict `<`
        ([reconciler.py:363](v2/lifecycle/reconciler.py#L363)). The killed-thesis
        prune then drops the entry at the end of that same cycle.

        Consequences, pinned here so they cannot be discovered by surprise later:
        `LogEvent("KILL_COOLDOWN")` never appears in a replay capture, C-2.2's
        `KILL_COOLDOWN` shadow class is unreachable in replay, and findings
        A18/A33 — what does the 1 h cooldown cost — cannot be answered at this
        cadence. See the M9 findings.
        """
        cycle = _Cycle(self.run_dir)          # shipped LifecycleConfig
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        cycle.run([valid("BTC/USDT")])
        self.assertEqual([e for e in self.events()
                          if e.log_event_type == "KILL_COOLDOWN"], [])
        # ... and the re-insertion the cooldown was meant to block goes through.
        third = [e for e in self.events()
                 if e.cycle_index == 2 and e.mutation_type == "InsertMutation"]
        self.assertEqual(len(third), 1)

    def test_guard_captures_the_invalid_levels(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", entry=100.0, sl=100.0, tp=100.0)])
        self.assertIn("GUARD", self.log_types())

    def test_stale_captures_a_log_and_no_mutation(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", live=80.0)])
        self.assertIn("STALE", self.log_types())
        self.assertEqual([e for e in self.events()
                          if e.kind == KIND_MUTATION], [])


# ==========================================================================
# TTL: attribution by elimination (C-15.5)
# ==========================================================================

class TestTtlAttribution(CaptureTestCase):

    def config(self):
        """1.5 h, so a record inserted in cycle 0 is swept in cycle 2.

        The sweep requires `age > ttl` strictly, and the clock advances in exact
        3600 s steps, so a TTL that is a whole multiple of an hour is never
        exceeded at the cycle where it looks like it should be — the same strict
        inequality that makes the kill cooldown unreachable.
        """
        return LifecycleConfig(untapped_ttl_seconds=1.5 * HOUR,
                               tapped_ttl_seconds=1.5 * HOUR,
                               manual_action_ttl_seconds=1.5 * HOUR)

    def test_a_ttl_eviction_is_inferred_and_marked_as_such(self):
        cycle = _Cycle(self.run_dir, config=self.config())
        cycle.run([valid("BTC/USDT")])
        cycle.run([])
        cycle.run([])                # now 2h old -> swept
        inferred = [e for e in self.events()
                    if e.attribution == "inferred_ttl"]
        self.assertEqual(len(inferred), 1)
        event = inferred[0]
        self.assertEqual(event.symbol, "BTC/USDT")
        self.assertEqual(event.evict_reason, "ttl")
        self.assertEqual(event.mutation_type, "EvictMutation")
        self.assertEqual(event.phase, "end_of_cycle")
        self.assertIsNone(event.universe_rank)

    def test_an_inferred_event_carries_the_contracts_h1_last_ts_formula(self):
        """No per-symbol reconcile produced the line, so `h1_last_ts = T - 3600`
        (C-15.3) rather than a frame-derived value that does not exist here."""
        cycle = _Cycle(self.run_dir, config=self.config())
        cycle.run([valid("BTC/USDT")])
        cycle.run([])
        cycle.run([])
        event = [e for e in self.events() if e.attribution == "inferred_ttl"][0]
        self.assertEqual(event.h1_last_ts, float(event.scan_T - HOUR))

    def test_end_of_cycle_events_sort_after_every_per_symbol_event(self):
        """C-5.6.4."""
        cycle = _Cycle(self.run_dir, config=self.config())
        cycle.run([valid("BTC/USDT")])
        cycle.run([])
        cycle.run([valid("ETH/USDT")])       # BTC is swept in this cycle
        third = [e for e in self.events() if e.cycle_index == 2]
        phases = [e.phase for e in third]
        self.assertEqual(phases, sorted(phases, key=lambda p: p == "end_of_cycle"))
        self.assertEqual(third[-1].attribution, "inferred_ttl")

    def test_the_residue_equals_the_rendered_ttl_line_count(self):
        """C-15.5's mandatory cross-check, asserted on a real sweep."""
        cycle = _Cycle(self.run_dir, config=self.config())
        cycle.run([valid("BTC/USDT"), valid("ETH/USDT", score=11)])
        cycle.run([])
        capture = cycle.run([])
        self.assertEqual(capture.ttl_inferred, 2)
        self.assertEqual(capture.ttl_log_lines, 2)
        self.assertEqual(
            sum(1 for line in cycle.recorder.console
                if TTL_RENDERED_MARKER in line), 2)

    def test_a_kill_is_never_attributed_to_ttl(self):
        """The decay/kill eviction is in `A_k`, so the residue excludes it —
        C-15.5's second edge case."""
        cycle = _Cycle(self.run_dir, config=self.config())
        cycle.run([valid("BTC/USDT")])
        capture = cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                                   h4_bias="BEARISH")])
        self.assertEqual(capture.ttl_inferred, 0)
        self.assertEqual(capture.ttl_log_lines, 0)

    def test_a_replace_target_is_never_attributed_to_ttl(self):
        config = LifecycleConfig(max_open_positions=1,
                                 untapped_ttl_seconds=1.5 * HOUR)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=10)])
        capture = cycle.run([valid("ETH/USDT", score=15)])
        self.assertEqual(capture.ttl_inferred, 0)

    def test_an_unprocessed_cycle_writes_no_snapshot_and_skips_the_differ(self):
        """C-5.6.5 / C-15.5's fourth edge case: an empty universe performs no
        end-of-cycle maintenance, so a reader treats the gap as "unchanged"."""
        cycle = _Cycle(self.run_dir, config=self.config())
        cycle.run([valid("BTC/USDT")])
        capture = cycle.run([], processed=False)
        self.assertFalse(capture.snapshot_written)
        self.assertEqual(capture.ttl_inferred, 0)
        self.assertEqual(len(self.snapshots()), 1)

    def test_every_memory_removal_is_attributed(self):
        """§17.8: the completeness assertion, over a scenario that exercises
        KILL, DECAY, TTL and REPLACE together."""
        config = LifecycleConfig(max_open_positions=2, miss_count_limit=1,
                                 untapped_ttl_seconds=2.5 * HOUR,
                                 tapped_ttl_seconds=2.5 * HOUR)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=15),
                   valid("ETH/USDT", score=11)])            # two inserts
        cycle.run([valid("SOL/USDT", score=12)])            # REPLACE ETH
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])               # KILL BTC
        cycle.run([skip("SOL/USDT")])                       # DECAY SOL
        cycle.run([valid("ETH/USDT", score=11)])            # re-insert ETH
        cycle.run([])
        cycle.run([])
        cycle.run([])                                       # TTL ETH

        snapshots = {s.cycle_index: set(s.active) for s in self.snapshots()}
        removals = 0
        for index in sorted(snapshots):
            before = snapshots.get(index - 1, set())
            removals += len(before - snapshots[index])
        attributed = [e for e in self.events()
                      if e.evict_reason is not None or e.target_symbol is not None]
        self.assertGreater(removals, 0)
        self.assertEqual(removals, len(attributed))

    def test_a_removal_the_layer_cannot_see_fails_the_run(self):
        """The cross-check is not decorative: a memory removal with neither a
        captured mutation nor a rendered TTL line stops the cycle."""
        recorder = CaptureRecorder(self.run_dir, run_id="r")
        recorder.begin_cycle(0, T0)
        recorder.record_memory_snapshot({"BTC/USDT": _fake_signal()}, {})
        recorder.end_cycle(True)
        recorder.begin_cycle(1, T0 + HOUR)
        recorder.record_memory_snapshot({}, {})     # vanished, unexplained
        with self.assertRaises(CaptureError) as ctx:
            recorder.end_cycle(True)
        self.assertIn("TTL attribution mismatch", str(ctx.exception))


def _fake_signal():
    from v2.domain.active_signal import ActiveSignal
    return ActiveSignal(
        direction="LONG", score=10, decision="VALID", entry_price=100.0,
        sl=95.0, tp=115.0, regime="expansion_up", timestamp=float(T0 + 1),
        entry_mode="RETRACEMENT")


# ==========================================================================
# Alerts are a cross-check, not a source (C-15.7)
# ==========================================================================

class TestAlertCrossCheck(CaptureTestCase):

    def test_the_notifier_content_equals_the_captured_decisions_alerts(self):
        cycle = _Cycle(self.run_dir)
        capture = cycle.run([valid("BTC/USDT"), valid("ETH/USDT", score=11)])
        self.assertEqual(capture.alerts, capture.alerts_from_decisions)
        self.assertEqual(capture.alerts, 2)

    def test_a_lost_dispatch_fails_the_run(self):
        """The exact failure C-2.1 anticipates: `_render_symbol` raising between
        `store.apply` and `_dispatch`, so an applied decision never reaches the
        notifier."""
        recorder = CaptureRecorder(self.run_dir, run_id="r")
        recorder.begin_cycle(0, T0)
        clock = FakeClock(datetime.fromtimestamp(T0 + 1, tz=timezone.utc))
        wrapper = RecordingReconciler(Reconciler(clock), recorder)
        signal, htf = make_signal(valid("BTC/USDT", h1_last_ts=float(T0 - HOUR)))
        wrapper.reconcile(signal, SignalStore().view(), htf=htf,
                          live_price=101.0, h1_last_ts=float(T0 - HOUR))
        # dispatch never happens
        with self.assertRaises(CaptureError) as ctx:
            recorder.end_cycle(True)
        self.assertIn("alert cross-check failed", str(ctx.exception))

    def test_alerts_jsonl_keeps_its_pre_capture_shape(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        rows = [json.loads(line) for line in
                (self.run_dir / "alerts.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        self.assertEqual(set(rows[0]), {"run_id", "cycle_index", "seq", "alert"})
        self.assertEqual(rows[0]["alert"]["kind"], "new")
        self.assertEqual(rows[0]["seq"], 1)


# ==========================================================================
# Single ownership (C-15.2.1)
# ==========================================================================

class TestOwnership(CaptureTestCase):

    def test_the_sinks_hold_no_file_of_their_own(self):
        """C-15.2.1: `ReplayPersistence` and `RecordingNotifier` are thin
        forwarders. Asserted structurally — neither opens a path."""
        source = (Path("backtest") / "replay" / "sinks.py").read_text(
            encoding="utf-8")
        self.assertNotIn("open(", source)
        self.assertNotIn("write_text", source)

    def test_both_sinks_share_the_recorder_they_are_given(self):
        recorder = CaptureRecorder(self.run_dir, run_id="r")
        persistence = ReplayPersistence(self.run_dir, recorder=recorder)
        notifier = RecordingNotifier(self.run_dir, recorder=recorder)
        self.assertIs(persistence.recorder, recorder)
        self.assertIs(notifier.recorder, recorder)

    def test_a_sink_without_a_recorder_still_has_exactly_one_owner(self):
        persistence = ReplayPersistence(self.run_dir)
        self.assertIsInstance(persistence.recorder, CaptureRecorder)

    def test_the_ledger_and_audit_counts_come_from_the_recorder(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        self.assertEqual(cycle.persistence.ledger_rows,
                         cycle.recorder.ledger_rows)
        self.assertEqual(cycle.persistence.saves, cycle.recorder.saves)

    def test_the_memory_snapshot_records_active_and_killed(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        snapshots = self.snapshots()
        self.assertEqual(set(snapshots[0].active), {"BTC/USDT"})
        self.assertEqual(snapshots[0].active["BTC/USDT"]["direction"], "LONG")
        self.assertEqual(snapshots[1].active, {})
        self.assertEqual(set(snapshots[1].killed),
                         {"BTC/USDT|LONG|RETRACEMENT"})

    def test_the_snapshot_is_recorded_before_the_write_that_could_fail(self):
        """A failed file write must not also cost the observation — the same
        principle as C-15.2's record-before-return."""
        class _Failing(CaptureRecorder):
            def save_active_signals(self, signals):
                raise RuntimeError("disk full")

        recorder = _Failing(self.run_dir, run_id="r")
        recorder.begin_cycle(0, T0)
        persistence = ReplayPersistence(self.run_dir, recorder=recorder)
        with self.assertRaises(RuntimeError):
            persistence.save_active_signals({"BTC/USDT": _fake_signal()})
        capture = recorder.end_cycle(True)
        self.assertTrue(capture.snapshot_written)


# ==========================================================================
# The trade builder (C-9.2)
# ==========================================================================

class TestTradeIntentBuilder(CaptureTestCase):

    def test_an_alerted_insert_becomes_one_trade_with_the_frozen_levels(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        report = self.build()

        self.assertEqual(len(report), 1)
        intent = report.intents[0]
        self.assertEqual(intent.symbol, "BTC/USDT")
        self.assertEqual(intent.direction, "LONG")
        self.assertEqual(intent.entry_mode, "RETRACEMENT")
        self.assertEqual(intent.entry_price, 100.0)
        self.assertEqual(intent.sl, 95.0)
        self.assertEqual(intent.tp, 115.0)
        self.assertEqual(intent.alert_tier, "VALID")
        self.assertIsNone(intent.shadow_kind)
        self.assertEqual(intent.boundary, T0)
        self.assertEqual(intent.h1_last_ts, T0 - HOUR)
        self.assertEqual(intent.cycle_index, 0)
        self.assertEqual(intent.universe_rank, 1)
        self.assertEqual(intent.tp_source, "EQH_cluster")
        self.assertEqual(intent.entry_source, "retracement_ob")
        self.assertEqual(report.alert_bearing, 1)

    def test_the_alerts_file_is_never_read(self):
        """C-2.1: the decision stream is the source of truth. Deleting the
        cross-check file must change nothing."""
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        self.write_manifest()
        before = build_trade_intents(self.run_dir).intents
        (self.run_dir / "alerts.jsonl").unlink()
        self.assertEqual(build_trade_intents(self.run_dir).intents, before)

    def test_the_builder_needs_only_the_capture_files_and_the_manifest(self):
        """Behavioural, not textual: every other artifact in the run directory is
        deleted and the build must be unchanged. Prose must not decide an
        architecture test."""
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        self.write_manifest()
        before = build_trade_intents(self.run_dir).intents

        keep = {LIFECYCLE_EVENTS_FILENAME, "signal_ledger.jsonl",
                "manifest.json"}
        removed = []
        for path in sorted(self.run_dir.iterdir()):
            if path.is_file() and path.name not in keep:
                path.unlink()
                removed.append(path.name)
        self.assertIn("alerts.jsonl", removed)
        self.assertIn(MEMORY_SNAPSHOTS_FILENAME, removed)
        self.assertEqual(build_trade_intents(self.run_dir).intents, before)

    def test_an_unalerted_insert_becomes_an_unalerted_shadow(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", decision="WATCH", score=7)])
        report = self.build()
        self.assertEqual(len(report), 1)
        self.assertEqual(report.intents[0].shadow_kind, "UNALERTED_INSERT")
        self.assertEqual(report.alert_bearing, 0)
        self.assertEqual(report.shadows, {"UNALERTED_INSERT": 1})

    def test_shadows_can_be_excluded(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", decision="WATCH", score=7)])
        self.write_manifest()
        self.assertEqual(
            len(build_trade_intents(self.run_dir, include_shadows=False)), 0)

    def test_a_kill_cancels_the_pending_trade_and_marks_the_boundary(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        intent = self.build().intents[0]
        self.assertIsNotNone(intent.cancellation)
        self.assertEqual(intent.cancellation.reason, "KILL")
        self.assertEqual(intent.cancellation.boundary, T0 + HOUR)
        self.assertEqual(intent.htf_kill_boundary, T0 + HOUR)

    def test_a_decay_eviction_cancels_with_decay(self):
        cycle = _Cycle(self.run_dir, config=LifecycleConfig(miss_count_limit=3))
        cycle.run([valid("BTC/USDT")])
        for _ in range(3):
            cycle.run([skip("BTC/USDT")])
        intent = self.build().intents[0]
        self.assertEqual(intent.cancellation.reason, "DECAY")
        self.assertEqual(intent.cancellation.boundary, T0 + 3 * HOUR)

    def test_a_ttl_eviction_cancels_with_ttl(self):
        config = LifecycleConfig(untapped_ttl_seconds=1.5 * HOUR)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT")])
        cycle.run([])
        cycle.run([])
        intent = self.build().intents[0]
        self.assertEqual(intent.cancellation.reason, "TTL")
        self.assertEqual(intent.cancellation.boundary, T0 + 2 * HOUR)

    def test_a_replace_cancels_the_evicted_thesis_with_replaced(self):
        config = LifecycleConfig(max_open_positions=1)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=10)])
        cycle.run([valid("ETH/USDT", score=15)])
        report = self.build()
        by_symbol = {intent.symbol: intent for intent in report.intents}
        self.assertEqual(by_symbol["BTC/USDT"].cancellation.reason, "REPLACED")
        self.assertIsNone(by_symbol["ETH/USDT"].cancellation)

    def test_an_upgrade_supersedes_the_pending_trade_and_instantiates_a_new_one(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", decision="VALID", score=10)])
        cycle.run([valid("BTC/USDT", decision="A+", score=15, tp=120.0)])
        report = self.build()
        self.assertEqual(len(report), 2)
        first, second = report.intents
        self.assertEqual(first.alert_tier, "VALID")
        self.assertEqual(first.cancellation.reason, "SUPERSEDED")
        self.assertEqual(first.cancellation.boundary, T0 + HOUR)
        self.assertEqual(second.alert_tier, "A+")
        self.assertEqual(second.tp, 120.0)
        self.assertIsNone(second.cancellation)
        self.assertEqual(report.superseded, 1)

    def test_a_silent_thesis_flip_supersedes_and_instantiates_nothing(self):
        """C-4.7's third bullet / finding A43: a direction flip with no alert
        overwrites the resident record. The old pending trade dies; no trade is
        born, because there was no alert (C-2.1)."""
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", decision="VALID", score=10)])
        # Neutral biases, so neither the resident LONG is killed nor the fresh
        # SHORT is stale (bullish biases would flip *against* a SHORT and make it
        # stale before it reached the memory engine). The fresh thesis is a
        # SHORT/CONTINUATION at the same tier, so no alert fires and the record
        # is silently overwritten — finding A43.
        cycle.run([valid("BTC/USDT", direction="SHORT", entry_mode="CONTINUATION",
                         decision="VALID", score=12, entry=100.0, sl=105.0,
                         tp=90.0, live=99.0, d1_bias="CHOPPY",
                         h4_bias="CHOPPY")])
        report = self.build(include_shadows=False)
        self.assertEqual(len(report), 1)
        self.assertEqual(report.intents[0].direction, "LONG")
        self.assertEqual(report.intents[0].cancellation.reason, "SUPERSEDED")

    def test_a_same_thesis_refresh_does_not_touch_the_trade(self):
        """C-4.6: the operator was not re-alerted, so their order stands. Memory
        levels and trade levels are allowed to diverge."""
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", score=10)])
        cycle.run([valid("BTC/USDT", score=13, entry=99.0, tp=130.0)])
        report = self.build()
        self.assertEqual(len(report), 1)
        intent = report.intents[0]
        self.assertIsNone(intent.cancellation)
        self.assertEqual(intent.entry_price, 100.0)
        self.assertEqual(intent.tp, 115.0)

    def test_a_capped_decision_becomes_a_fixed_window_shadow(self):
        config = LifecycleConfig(max_open_positions=1)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=15)])
        cycle.run([valid("ETH/USDT", score=9, entry=50.0, sl=48.0, tp=56.0)])
        self._write_ledger_row("ETH/USDT", T0 + HOUR, decision="VALID",
                               entry=50.0, sl=48.0, tp=56.0)
        report = self.build()
        shadow = [i for i in report.intents if i.shadow_kind == "CAPPED"]
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0].entry_price, 50.0)
        self.assertTrue(shadow[0].has_fixed_shadow_window)
        self.assertIsNone(shadow[0].cancellation)
        self.assertEqual(report.ledger_rows_used, 1)

    def test_a_kill_cooldown_shadow_needs_an_alert_tier_row(self):
        """C-2.2 gates the class on the canonical row's tier: a suppressed WATCH
        would never have been alerted, so the cooldown cost nothing."""
        cycle = _Cycle(self.run_dir,
                       config=LifecycleConfig(kill_cooldown_seconds=2.0 * HOUR))
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        cycle.run([valid("BTC/USDT")])
        self._write_ledger_row("BTC/USDT", T0 + 2 * HOUR, decision="WATCH")
        self.assertEqual(
            [i for i in self.build().intents
             if i.shadow_kind == "KILL_COOLDOWN"], [])

        self._write_ledger_row("BTC/USDT", T0 + 2 * HOUR, decision="VALID")
        shadow = [i for i in self.build().intents
                  if i.shadow_kind == "KILL_COOLDOWN"]
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0].boundary, T0 + 2 * HOUR)

    def test_a_suppressed_shadow_without_a_ledger_row_is_not_invented(self):
        config = LifecycleConfig(max_open_positions=1)
        cycle = _Cycle(self.run_dir, config=config)
        cycle.run([valid("BTC/USDT", score=15)])
        cycle.run([valid("ETH/USDT", score=9)])
        report = self.build()
        self.assertEqual([i for i in report.intents
                          if i.shadow_kind == "CAPPED"], [])
        self.assertEqual(report.missing_ledger_rows, 1)

    def test_a_watch_only_insert_produces_no_trade(self):
        """WATCH_ONLY and NONE have no fill rule (C-4.1/C-4.2)."""
        cycle = _Cycle(self.run_dir)
        cycle.run([CycleSpec(
            symbol="BTC/USDT", decision="WATCH", direction="LONG",
            entry_mode="WATCH_ONLY", regime="compression", score=6,
            entry_ready=False, entry_price=100.0, sl=95.0, tp=115.0,
            d1_bias="BULLISH", h4_bias="BULLISH", atr_last=1.0,
            live_price=101.0)])
        self.assertEqual(len(self.build()), 0)

    def test_multiple_symbols_produce_independent_trades(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", score=15),
                   valid("ETH/USDT", score=12, entry=50.0, sl=48.0, tp=56.0),
                   valid("SOL/USDT", score=11, entry=10.0, sl=9.0, tp=13.0)])
        report = self.build()
        self.assertEqual(len(report), 3)
        self.assertEqual([i.universe_rank for i in report.intents], [1, 2, 3])
        self.assertEqual([i.symbol for i in report.intents],
                         ["BTC/USDT", "ETH/USDT", "SOL/USDT"])

    def test_burn_in_is_labelled_from_the_boundary(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("ETH/USDT")])
        report = self.build(burn_in_until=T0 + HOUR)
        flags = {i.symbol: i.in_burn_in for i in report.intents}
        self.assertTrue(flags["BTC/USDT"])
        self.assertFalse(flags["ETH/USDT"])

    def test_burn_in_is_required_not_defaulted(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        with self.assertRaises(TradeSourceError):
            TradeIntentBuilder(self.run_dir)

    def test_an_empty_replay_builds_nothing_without_complaining(self):
        self.write_manifest()
        report = build_trade_intents(self.run_dir)
        self.assertEqual(len(report), 0)
        self.assertEqual(report.events, 0)
        self.assertEqual(report.run_id, "")

    def test_a_cycle_with_no_actionable_symbol_builds_nothing(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([skip("BTC/USDT"), skip("ETH/USDT")])
        report = self.build()
        self.assertEqual(len(report), 0)
        self.assertEqual(report.decisions, 2)

    def test_the_build_is_deterministic_and_repeatable(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", score=15), valid("ETH/USDT", score=12)])
        cycle.run([valid("BTC/USDT", live=80.0, d1_bias="BEARISH",
                         h4_bias="BEARISH")])
        self.write_manifest()
        first = build_trade_intents(self.run_dir).intents
        second = build_trade_intents(self.run_dir).intents
        self.assertEqual(first, second)

    # -- duplicate protection and stream integrity ------------------------

    def test_a_duplicated_event_is_refused(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        path = self.run_dir / LIFECYCLE_EVENTS_FILENAME
        text = path.read_text(encoding="utf-8")
        path.write_text(text + text, encoding="utf-8")
        self.write_manifest()
        with self.assertRaises(TradeSourceError) as ctx:
            build_trade_intents(self.run_dir)
        self.assertIn("duplicate event", str(ctx.exception))

    def test_an_out_of_order_stream_is_refused(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        cycle.run([valid("ETH/USDT")])
        path = self.run_dir / LIFECYCLE_EVENTS_FILENAME
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
        self.write_manifest()
        with self.assertRaises(TradeSourceError) as ctx:
            build_trade_intents(self.run_dir)
        self.assertIn("out of", str(ctx.exception))

    def test_two_runs_in_one_file_are_refused(self):
        first = _Cycle(self.run_dir, run_id="replay-aaa")
        first.run([valid("BTC/USDT")])
        second = _Cycle(self.run_dir, run_id="replay-bbb")
        second.run([valid("ETH/USDT")])
        self.write_manifest()
        with self.assertRaises(TradeSourceError) as ctx:
            build_trade_intents(self.run_dir)
        self.assertIn("run ids", str(ctx.exception))

    def test_two_runs_in_separate_directories_stay_separate(self):
        other = self.run_dir.parent / "replay-other"
        other.mkdir()
        left = _Cycle(self.run_dir, run_id="replay-aaa")
        left.run([valid("BTC/USDT")])
        right = _Cycle(other, run_id="replay-bbb")
        right.run([valid("ETH/USDT")])
        for directory in (self.run_dir, other):
            (directory / "manifest.json").write_text(
                json.dumps({"burn_in_until": 0}), encoding="utf-8")
        self.assertEqual(build_trade_intents(self.run_dir).run_id, "replay-aaa")
        self.assertEqual(build_trade_intents(other).run_id, "replay-bbb")

    def test_an_insertion_without_direction_is_refused_not_guessed(self):
        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT")])
        path = self.run_dir / LIFECYCLE_EVENTS_FILENAME
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line]
        rows[0]["mutation_payload"]["record"]["direction"] = ""
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        self.write_manifest()
        with self.assertRaises(TradeSourceError):
            build_trade_intents(self.run_dir)

    # -- helpers ----------------------------------------------------------

    def _write_ledger_row(self, symbol: str, boundary: int, *,
                          decision: str = "VALID", entry: float = 100.0,
                          sl: float = 95.0, tp: float = 115.0) -> None:
        """A canonical ledger row for one scan (C-9.4). Only the fields C-2.2
        takes from it are needed."""
        path = self.run_dir / "signal_ledger.jsonl"
        row = {
            "symbol": symbol, "h1_last_ts": float(boundary - HOUR),
            "decision": decision, "direction": "LONG",
            "entry_mode": "RETRACEMENT", "entry_price": entry, "sl": sl,
            "tp": tp, "score": 9, "tp_source": "EQH_cluster",
            "entry_source": "retracement_ob",
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")


# ==========================================================================
# End to end: capture -> TradeIntent -> Outcome Engine
# ==========================================================================

def ohlc(index: int, o: float, h: float, l: float, c: float,
         *, first_open_ms: int) -> tuple:
    """One explicit H1 kline row, in Binance's twelve-column layout.

    Hand-written rather than generated: an integration test that asserts a
    specific terminal state has to control the prices that produce it, and a
    series whose levels happen to pre-cross the limit proves nothing about the
    bridge.
    """
    open_ms = first_open_ms + index * HOUR * 1000
    return (open_ms, o, h, l, c, 10.0, open_ms + HOUR * 1000 - 1,
            1000.0, 100, 5.0, 500.0, 0.0)


class TestOutcomeEngineIntegration(CaptureTestCase):

    def test_captured_trades_resolve_through_the_engine_unchanged(self):
        """The bridge, end to end. The engine is not modified: it takes the same
        `TradeIntent` boundary it always took, now populated from capture."""
        from tests.backtest.test_historical_adapter import write_repository
        from backtest.data import KlineRepository

        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", entry=100.0, sl=95.0, tp=115.0)])
        report = self.build()
        self.assertEqual(len(report), 1)
        intent = report.intents[0]

        # `C_ref` closes above the limit (so it is not pre-crossed, C-4.1), `W1`
        # touches 100 (LIMIT_TOUCH), `W3` takes the stop.
        first = (T0 - HOUR) * 1000
        rows = [
            ohlc(0, 104.0, 106.0, 103.0, 105.0, first_open_ms=first),  # C_ref
            ohlc(1, 105.0, 106.0, 102.0, 103.0, first_open_ms=first),  # W0
            ohlc(2, 103.0, 104.0,  99.5, 101.0, first_open_ms=first),  # W1 fill
            ohlc(3, 101.0, 102.0,  98.0,  99.0, first_open_ms=first),  # W2
            ohlc(4,  99.0,  99.5,  94.0,  95.5, first_open_ms=first),  # W3 SL
            ohlc(5,  95.5,  96.0,  95.0,  95.5, first_open_ms=first),
        ]
        root = Path(self._tmp.name) / "repo"
        write_repository(root, {("BTCUSDT", "1h"): rows})
        engine = OutcomeEngine(KlineRepository(root),
                               config=OutcomeConfig(max_holding_h1=48))
        record = engine.resolve(intent)

        self.assertEqual(record.symbol, "BTC/USDT")
        self.assertEqual(record.entry_price, 100.0)
        self.assertEqual(record.run_id, "replay-test")
        self.assertEqual(record.source, "replay")
        self.assertTrue(record.filled)
        self.assertEqual(record.fill_kind, "LIMIT_TOUCH")
        self.assertEqual(record.fill_price, 100.0)
        self.assertEqual(record.candles_to_fill, 1)
        self.assertTrue(record.is_terminal)
        self.assertEqual(record.state, "LOSS")
        self.assertEqual(record.exit_reason, "SL")
        self.assertEqual(record.realized_r, -1.0)
        self.assertEqual(record.holding_candles, 3)

    def test_a_run_resolves_and_writes_outcomes_without_touching_capture(self):
        """The full bridge: replay artifacts in, `outcomes.jsonl` out, and every
        captured file bit-identical afterwards (C-9.5)."""
        import hashlib

        from tests.backtest.test_historical_adapter import write_repository
        from backtest.data import KlineRepository

        cycle = _Cycle(self.run_dir)
        cycle.run([valid("BTC/USDT", score=15),
                   valid("ETH/USDT", score=12, entry=50.0, sl=48.0, tp=56.0)])
        report = self.build()

        first = (T0 - HOUR) * 1000
        root = Path(self._tmp.name) / "repo"
        write_repository(root, {
            ("BTCUSDT", "1h"): [
                ohlc(i, 104.0, 106.0, 99.0, 105.0, first_open_ms=first)
                for i in range(8)],
            ("ETHUSDT", "1h"): [
                ohlc(i, 52.0, 53.0, 49.5, 52.5, first_open_ms=first)
                for i in range(8)],
        })
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(self.run_dir.iterdir()) if path.is_file()}

        engine = OutcomeEngine(KlineRepository(root))
        written = engine.write(report.intents, self.run_dir)
        self.assertEqual(len(written), 2)

        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(self.run_dir.iterdir()) if path.is_file()}
        self.assertEqual(set(after) - set(before), {"outcomes.jsonl"})
        for name, digest in before.items():
            self.assertEqual(after[name], digest, name)

    def test_the_engine_still_imports_nothing_from_replay(self):
        """§10: `outcome -> data + v2.domain`. The bridge is a shared schema
        module, not a dependency on the capture layer."""
        from tests.backtest.test_historical_adapter import imported_modules
        for path in (Path("backtest") / "outcome").glob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.replay"),
                                 f"{path.name}: {module}")


if __name__ == "__main__":
    unittest.main()
