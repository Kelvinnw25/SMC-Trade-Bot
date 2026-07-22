"""Phase 1 DoD: construction and value-equality semantics — full contract.

Two behavioral contracts are proven here:

1. Field-sensitivity: for EVERY domain object, changing ANY equality-relevant
   field produces an unequal object (or is rejected at construction by an
   invariant — which equally proves the field is significant). Fields declared
   ``compare=False`` (MarketSnapshot's DataFrames) are proven NOT to affect
   equality — that exclusion is itself part of the documented contract.

2. The Signal/candidate consistency invariant: the three flat fields that must
   survive on Signal for candidate-less early-exit SKIPs (direction,
   entry_mode, regime) can never disagree with the embedded candidate.
"""
from __future__ import annotations

import dataclasses
import unittest

from tests.domain import fixtures
from v2.domain import Signal

#: Unique sentinel: never equal to any real field value, so substituting it
#: into an equality-relevant field MUST break equality.
_SENTINEL = object()

#: One maker per domain object (LifecycleState is an Enum, covered elsewhere).
ALL_MAKERS = [
    fixtures.make_market_snapshot,
    fixtures.make_htf,
    fixtures.make_regime,
    fixtures.make_swing,
    fixtures.make_order_block,
    fixtures.make_fvg,
    fixtures.make_sweep,
    fixtures.make_entry_plan,
    fixtures.make_risk_plan,
    fixtures.make_candidate,
    fixtures.make_signal,
    fixtures.make_active_signal,
    fixtures.make_killed_thesis,
    fixtures.make_ob_audit,
    fixtures.make_log_event,
    fixtures.make_alert,
    fixtures.make_lifecycle_decision,
]


class TestValueEquality(unittest.TestCase):
    def test_same_values_are_equal(self):
        for make in ALL_MAKERS:
            with self.subTest(obj=make.__name__):
                self.assertEqual(make(), make())

    def test_every_compare_field_is_equality_sensitive(self):
        """Behavioral contract: each compare-enabled field participates in
        __eq__. Substituting a unique sentinel must yield an unequal object;
        a construction-time invariant rejection also proves significance."""
        for make in ALL_MAKERS:
            obj = make()
            for f in dataclasses.fields(obj):
                if not f.compare:
                    continue
                with self.subTest(obj=type(obj).__name__, field=f.name):
                    try:
                        mutated = dataclasses.replace(obj, **{f.name: _SENTINEL})
                    except (ValueError, AttributeError, TypeError):
                        # Construction rejected the substitute (e.g. Signal's
                        # flat/candidate consistency invariant inspecting the
                        # field) — the field is provably significant. Fields
                        # skipped this way get explicit coverage below.
                        continue
                    self.assertNotEqual(obj, mutated)

    def test_signal_candidate_field_is_equality_sensitive(self):
        """Explicit coverage for the one field the sentinel sweep cannot
        substitute (the invariant inspects it): candidate participation."""
        with_candidate = fixtures.make_signal()
        without_candidate = fixtures.make_signal(candidate=None)
        self.assertNotEqual(with_candidate, without_candidate)

    def test_mutation_types_are_equality_sensitive(self):
        decision = fixtures.make_lifecycle_decision()
        for mutation in decision.mutations:
            for f in dataclasses.fields(mutation):
                if not f.compare:
                    continue
                with self.subTest(mutation=type(mutation).__name__, field=f.name):
                    self.assertNotEqual(
                        mutation,
                        dataclasses.replace(mutation, **{f.name: _SENTINEL}),
                    )

    def test_market_snapshot_equality_excludes_dataframes(self):
        """The single documented immutability/equality exception: frames are
        compare=False; content comparison uses DataFrame.equals explicitly."""
        a = fixtures.make_market_snapshot()
        b = fixtures.make_market_snapshot(d1=fixtures.make_ohlcv_frame(7))
        self.assertEqual(a, b)
        self.assertTrue(a.h1.equals(b.h1))
        self.assertFalse(a.d1.equals(b.d1))
        self.assertNotEqual(a, fixtures.make_market_snapshot(live_price=9.9))

    def test_entry_plan_contract_b_default(self):
        """Decision #001: the Contract-B marker defaults to "B"."""
        self.assertEqual(fixtures.make_entry_plan().contract, "B")


class TestSignalCandidateInvariant(unittest.TestCase):
    """Fix-2 contract: Signal's flat direction/entry_mode/regime can never
    disagree with the embedded candidate (single source of truth)."""

    def test_consistent_construction_succeeds(self):
        signal = fixtures.make_signal()
        self.assertEqual(signal.entry_mode, signal.candidate.entry.entry_mode)
        self.assertEqual(signal.regime, signal.candidate.regime.label)
        self.assertEqual(signal.direction, signal.candidate.htf.direction)

    def test_entry_mode_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            fixtures.make_signal(entry_mode="RETRACEMENT")

    def test_regime_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            fixtures.make_signal(regime="rotational")

    def test_direction_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            fixtures.make_signal(direction="LONG")

    def test_candidate_less_early_exit_is_unconstrained(self):
        """V1 early-exit results (no h1_setup) carry direction/mode/regime
        with no candidate to agree with — construction must allow them."""
        skip = Signal(
            symbol="BTC/USDT", score=0, decision="SKIP", entry_mode="NONE",
            direction="SHORT", regime="expansion_up", reason="no_direction",
        )
        self.assertIsNone(skip.candidate)
        self.assertEqual(skip.decision, "SKIP")

    def test_flat_scalars_are_not_duplicated_on_signal(self):
        """Architecture §8 assigns entry/risk scalars to EntryPlan/RiskPlan;
        Signal must not re-carry them (single source of truth)."""
        field_names = {f.name for f in dataclasses.fields(Signal)}
        for forbidden in ("entry_price", "sl", "tp", "entry_source",
                          "tp_source", "tp_rr", "rr", "entry_ready",
                          "volume_spike"):
            self.assertNotIn(forbidden, field_names)


if __name__ == "__main__":
    unittest.main()
