"""Full V1-equivalence replay of the lifecycle layer.

A deterministic, seeded, multi-symbol, multi-cycle sequence of ``CycleSpec``s is
fed to BOTH the ``V1LifecycleReference`` (a verbatim transcription of ``main.py``,
calling V1's real ``is_poi_invalidated``) and the V2 lifecycle
(``SignalStore`` + ``Reconciler``). After every symbol-cycle and every
end-of-cycle maintenance step, three things must be identical:

1. the active-signal memory (serialized to V1 dict form, field-for-field);
2. the killed-thesis cooldown store;
3. the alerts emitted and the lifecycle event-type sequence.

This is the lifecycle analogue of the analysis phases' corpus equivalence, and
it exercises — by construction over the random matrix — kill, stale, cooldown,
same-thesis suppression, upgrade routing, slot replace/cap, H1-gated decay,
eviction, TTL expiry, and determinism.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timezone

from v2.infra.clock import FakeClock
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.reconciler import Reconciler
from v2.lifecycle.store import SignalStore

from tests.lifecycle.support import (
    CycleSpec,
    active_as_v1,
    make_signal,
    normalize_alert,
    normalize_ref_alert,
)
from tests.lifecycle.v1_reference import V1LifecycleReference

_BIASES = ["BULLISH", "BEARISH", "CHOPPY"]
_MODES = ["RETRACEMENT", "CONTINUATION", "WATCH_ONLY"]


def _levels(direction: str):
    """Consistent valid levels: entry 100 with sl/tp on the correct sides."""
    if direction == "LONG":
        return 100.0, 95.0, 115.0
    return 100.0, 105.0, 85.0


def _gen_spec(symbol: str, cycle: int, rng: random.Random) -> CycleSpec:
    roll = rng.random()
    direction = rng.choice(["LONG", "SHORT"])
    mode = rng.choice(_MODES)
    d1 = rng.choice(_BIASES)
    h4 = rng.choice(_BIASES)
    weekly = rng.choice(_BIASES)
    location = rng.choice(["discount", "premium", "equilibrium"])
    live_price = rng.choice([100.0, 90.0, 110.0, 96.0, 104.0, 0.0])
    atr = rng.choice([0.0, 1.0, 2.0])
    # H1 timestamp advances every 2 cycles -> exercises both gate-open and
    # gate-closed decay paths, identically on both sides.
    h1_ts = 1000.0 + (cycle // 2) * 3600.0
    entry, sl, tp = _levels(direction)

    common = dict(
        symbol=symbol, direction=direction, entry_mode=mode,
        regime=rng.choice(["expansion_up", "rotational", "accumulation", "neutral"]),
        d1_bias=d1, h4_bias=h4, weekly_bias=weekly, location=location,
        atr_last=atr, live_price=live_price, h1_last_ts=h1_ts,
        entry_source="retracement_ob" if mode == "RETRACEMENT" else "continuation_micro_pullback",
        tp_source="SWING_HIGH", volume_spike=rng.random() < 0.5,
    )

    if roll < 0.45:  # scored, actionable, valid levels
        return CycleSpec(
            decision=rng.choice(["WATCH", "VALID", "A+"]),
            score=rng.randint(8, 18), entry_ready=True,
            entry_price=entry, sl=sl, tp=tp, **common,
        )
    if roll < 0.55:  # actionable but INVALID levels -> GUARD
        return CycleSpec(
            decision=rng.choice(["WATCH", "VALID", "A+"]),
            score=rng.randint(8, 18), entry_ready=True,
            entry_price=entry, sl=0.0, tp=tp, **common,
        )
    if roll < 0.72:  # scored SKIP (levels present, e.g. bad_rr / low score)
        return CycleSpec(
            decision="SKIP", score=rng.randint(0, 8), entry_ready=True,
            entry_price=entry, sl=sl, tp=tp, reason="bad_rr", **common,
        )
    if roll < 0.84:  # not-ready SKIP (no levels)
        return CycleSpec(
            decision="SKIP", entry_ready=False, reason="no_entry", **common,
        )
    if roll < 0.91:  # veto
        return CycleSpec(
            decision="SKIP", veto=True,
            veto_reason="weekly_bearish_expansion_down_long_veto", **common,
        )
    if roll < 0.96:  # no_direction
        c = dict(common)
        c["direction"] = None
        c["entry_mode"] = "NONE"
        return CycleSpec(decision="SKIP", **c)
    # invalid_input
    c = dict(common)
    c["direction"] = None
    c["entry_mode"] = "NONE"
    c["has_htf"] = False
    return CycleSpec(decision="SKIP", **c)


class _V2Driver:
    """Wraps the V2 store + reconciler with the reference's process() shape."""

    def __init__(self, clock: FakeClock, config: LifecycleConfig) -> None:
        self.store = SignalStore(config=config)
        self.reconciler = Reconciler(clock, config)

    def process(self, spec: CycleSpec):
        signal, htf = make_signal(spec)
        decision = self.reconciler.reconcile(
            signal, self.store.view(), htf=htf,
            live_price=spec.live_price, h1_last_ts=spec.h1_last_ts,
        )
        self.store.apply(decision)
        return decision

    def ttl(self, clock: FakeClock):
        decision = self.store.ttl_sweep(clock)
        self.store.apply(decision)
        return decision

    def prune(self, clock: FakeClock):
        self.store.prune_killed_theses(clock)


def _killed_v2(store: SignalStore):
    return {k: v.killed_ts for k, v in store.killed_theses.items()}


class TestLifecycleEquivalence(unittest.TestCase):
    SYMBOLS = ["S0", "S1", "S2", "S3", "S4", "S5"]
    CYCLES = 60

    def _run(self, seed: int) -> None:
        rng = random.Random(seed)
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        config = LifecycleConfig(
            max_open_positions=3,
            miss_count_limit=3,
            kill_cooldown_seconds=3600.0,
            untapped_ttl_seconds=4000.0,
            tapped_ttl_seconds=1500.0,
            manual_action_ttl_seconds=8000.0,
        )
        ref = V1LifecycleReference(clock, config=config)
        v2 = _V2Driver(clock, config)

        for cycle in range(self.CYCLES):
            for symbol in self.SYMBOLS:
                spec = _gen_spec(symbol, cycle, rng)

                ref.reset_capture()
                ref.process(spec)
                decision = v2.process(spec)

                # (1) memory state
                self.assertEqual(
                    active_as_v1(v2.store.active), ref.active,
                    f"seed={seed} cycle={cycle} {symbol}: active memory diverged",
                )
                # (2) killed-thesis store
                self.assertEqual(
                    _killed_v2(v2.store), ref.killed,
                    f"seed={seed} cycle={cycle} {symbol}: killed store diverged",
                )
                # (3a) alerts
                self.assertEqual(
                    [normalize_alert(a) for a in decision.alerts],
                    [normalize_ref_alert(a) for a in ref.alerts],
                    f"seed={seed} cycle={cycle} {symbol}: alerts diverged",
                )
                # (3b) lifecycle event-type sequence
                self.assertEqual(
                    [le.event_type for le in decision.log_events],
                    [e[0] for e in ref.events],
                    f"seed={seed} cycle={cycle} {symbol}: event sequence diverged",
                )

                clock.advance(7.0)

            # end-of-cycle maintenance: TTL sweep then killed-prune (same now).
            ref.ttl_cleanup()
            v2.ttl(clock)
            self.assertEqual(
                active_as_v1(v2.store.active), ref.active,
                f"seed={seed} cycle={cycle}: post-TTL memory diverged",
            )
            ref.prune_killed()
            v2.prune(clock)
            self.assertEqual(
                _killed_v2(v2.store), ref.killed,
                f"seed={seed} cycle={cycle}: post-prune killed store diverged",
            )

            clock.advance(rng.choice([120.0, 480.0, 900.0, 1800.0]))

    def test_equivalence_across_seeds(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                self._run(seed)

    def test_determinism(self):
        # Same seed twice -> identical final state (no hidden nondeterminism).
        def final(seed):
            rng = random.Random(seed)
            clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
            cfg = LifecycleConfig(max_open_positions=3)
            v2 = _V2Driver(clock, cfg)
            for cycle in range(20):
                for symbol in self.SYMBOLS:
                    v2.process(_gen_spec(symbol, cycle, rng))
                    clock.advance(7.0)
                v2.ttl(clock)
                v2.prune(clock)
                clock.advance(300.0)
            return active_as_v1(v2.store.active), _killed_v2(v2.store)

        self.assertEqual(final(7), final(7))


if __name__ == "__main__":
    unittest.main()
