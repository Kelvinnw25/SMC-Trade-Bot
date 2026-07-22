"""Phase 1 DoD: immutability is enforced — mutation attempts provably fail.

Roadmap §8 Phase 1: "Every domain object in the architecture spec exists and
is immutable (mutation attempts provably fail)."
"""
from __future__ import annotations

import dataclasses
import unittest

from tests.domain import fixtures


class TestImmutability(unittest.TestCase):
    def _assert_frozen(self, obj, field_name: str):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(obj, field_name, "mutated")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            delattr(obj, field_name)

    def test_every_domain_object_is_frozen(self):
        cases = [
            (fixtures.make_market_snapshot(), "symbol"),
            (fixtures.make_htf(), "weekly_bias"),
            (fixtures.make_regime(), "label"),
            (fixtures.make_swing(), "price"),
            (fixtures.make_order_block(), "top"),
            (fixtures.make_fvg(), "valid"),
            (fixtures.make_sweep(), "detected"),
            (fixtures.make_entry_plan(), "entry_price"),
            (fixtures.make_risk_plan(), "sl"),
            (fixtures.make_candidate(), "atr_last"),
            (fixtures.make_signal(), "score"),
            (fixtures.make_active_signal(), "miss_count"),
            (fixtures.make_killed_thesis(), "thesis_key"),
            (fixtures.make_ob_audit(), "symbol"),
            (fixtures.make_log_event(), "event_type"),
            (fixtures.make_alert(), "kind"),
            (fixtures.make_lifecycle_decision(), "mutations"),
        ]
        for obj, field_name in cases:
            with self.subTest(obj=type(obj).__name__):
                self._assert_frozen(obj, field_name)

    def test_every_mutation_type_is_frozen(self):
        decision = fixtures.make_lifecycle_decision()
        for mutation in decision.mutations:
            with self.subTest(mutation=type(mutation).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(mutation, "symbol", "X/USDT")

    def test_collections_are_tuples(self):
        """Boundary collections are tuples, not lists (plan §5)."""
        candidate = fixtures.make_candidate()
        self.assertIsInstance(candidate.swing_highs, tuple)
        self.assertIsInstance(candidate.swing_lows, tuple)
        self.assertIsInstance(candidate.order_blocks, tuple)
        signal = fixtures.make_signal()
        self.assertIsInstance(signal.notes, tuple)
        decision = fixtures.make_lifecycle_decision()
        self.assertIsInstance(decision.mutations, tuple)
        self.assertIsInstance(decision.alerts, tuple)
        self.assertIsInstance(decision.log_events, tuple)
        self.assertIsInstance(decision.killed_theses, tuple)
        audit = fixtures.make_ob_audit()
        self.assertIsInstance(audit.ob_candidates, tuple)

    def test_lifecycle_state_enum_members(self):
        from v2.domain import LifecycleState

        self.assertEqual(
            [s.name for s in LifecycleState],
            ["ABSENT", "DISCOVERED", "ACTIVE", "DECAYING", "EVICTED"],
        )


if __name__ == "__main__":
    unittest.main()
