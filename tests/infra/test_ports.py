"""Port conformance — REPLAY_CONTRACT.md §17.1 (Milestone M2).

The two I/O collaborators the scan loop consumes are declared as Protocols
(`MarketDataPort`, `PersistencePort`) so a consumer depends on a written contract
rather than on a concrete adapter. This module is the suite that makes those
contracts real rather than decorative: it checks *structure* (the methods exist
with the declared signatures) and *behavior* (the invariants the Protocol
docstrings promise) against every implementation.

Today there is exactly one implementation of each — the live adapter. When a
second one appears, add it to the parameter list of the corresponding test: the
helpers below take the implementation as an argument precisely so no new test
machinery is needed, and so a replay implementation is held to the identical bar.

No network, no real filesystem outside a temp dir, no wall clock.
"""
from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from v2.domain import (
    ActiveSignal,
    KilledThesisRecord,
    OBAuditRecord,
    SignalLedgerRecord,
)
from v2.infra.market_data import (
    MarketDataAdapter,
    MarketDataError,
    MarketDataPort,
    SymbolVolume,
)
from v2.infra.persistence import (
    PersistenceAdapter,
    PersistenceError,
    PersistencePort,
)

from tests.infra.test_market_data import FakeHandler, base_frames


# --------------------------------------------------------------------------
# Conformance helpers (plain functions — no base class, no inheritance)
# --------------------------------------------------------------------------

def port_methods(port: type) -> tuple[str, ...]:
    """The method names a Protocol declares, sorted.

    Uses only public reflection (`vars`), never `typing`'s private
    `__protocol_attrs__`, so this helper cannot break on a Python upgrade.
    """
    return tuple(sorted(
        name for name, value in vars(port).items()
        if callable(value) and not name.startswith("_")
    ))


def assert_signature_matches(case: unittest.TestCase, port: type, impl: object) -> None:
    """Every port method exists on `impl` with the same parameters.

    Compares parameter names and kinds (excluding `self`), so a keyword-only
    argument cannot silently become positional and an optional argument cannot
    silently become required. Return annotations are not compared: an
    implementation may legitimately narrow them.
    """
    for name in port_methods(port):
        with case.subTest(method=name):
            case.assertTrue(
                callable(getattr(impl, name, None)),
                f"{type(impl).__name__} is missing {name}()",
            )
            expected = inspect.signature(getattr(port, name))
            actual = inspect.signature(getattr(impl, name))
            exp = [p for p in expected.parameters.values() if p.name != "self"]
            act = list(actual.parameters.values())
            case.assertEqual(
                [(p.name, p.kind) for p in exp],
                [(p.name, p.kind) for p in act],
                f"{name}() parameters diverge from {port.__name__}",
            )
            case.assertEqual(
                [p.default is inspect.Parameter.empty for p in exp],
                [p.default is inspect.Parameter.empty for p in act],
                f"{name}() optionality diverges from {port.__name__}",
            )


# --------------------------------------------------------------------------
# Implementation builders — one per implementation of each port
# --------------------------------------------------------------------------

def live_market_data(**kw) -> MarketDataAdapter:
    """The live adapter over an in-memory source (no network)."""
    return MarketDataAdapter(FakeHandler(**kw))


def live_persistence(base_dir) -> PersistenceAdapter:
    return PersistenceAdapter(base_dir)


# --------------------------------------------------------------------------
# MarketDataPort
# --------------------------------------------------------------------------

class TestMarketDataPortStructure(unittest.TestCase):
    def test_port_declares_exactly_two_methods(self):
        # Minimal surface: the orchestrator calls two, so the port has two.
        # `set_time` is deliberately absent (C-7: driver-only).
        self.assertEqual(
            list(port_methods(MarketDataPort)),
            ["get_market_snapshot", "get_top_volume_symbols"],
        )

    def test_live_adapter_satisfies_the_port(self):
        self.assertIsInstance(live_market_data(), MarketDataPort)

    def test_live_adapter_signatures_match(self):
        assert_signature_matches(self, MarketDataPort, live_market_data())

    def test_an_incomplete_object_does_not_satisfy_the_port(self):
        class Half:
            def get_top_volume_symbols(self, limit):
                return ()

        self.assertNotIsInstance(Half(), MarketDataPort)


class TestMarketDataPortBehavior(unittest.TestCase):
    """The invariants the Protocol docstring promises."""

    def test_empty_universe_is_an_empty_tuple(self):
        adapter = live_market_data(universe=[])
        self.assertEqual(adapter.get_top_volume_symbols(30), ())

    def test_universe_order_is_preserved_not_sorted(self):
        # Descending-volume order is the scan's processing order (C-5.6.2); the
        # port must hand it back untouched.
        adapter = live_market_data(universe=[
            {"symbol": "B/USDT", "volume": 900.0},
            {"symbol": "A/USDT", "volume": 100.0},
        ])
        got = adapter.get_top_volume_symbols(30)
        self.assertEqual([e.symbol for e in got], ["B/USDT", "A/USDT"])
        self.assertIsInstance(got[0], SymbolVolume)

    def test_missing_data_returns_none_not_an_exception(self):
        adapter = live_market_data(frames={})
        self.assertIsNone(adapter.get_market_snapshot("BTC/USDT", include_m5=False))

    def test_failures_raise_market_data_error_only(self):
        adapter = live_market_data(raise_on={"universe"})
        with self.assertRaises(MarketDataError):
            adapter.get_top_volume_symbols(30)

    def test_short_frames_are_returned_not_suppressed(self):
        # The D1<10 / H1<50 sufficiency guards belong to the caller (C-6.8).
        adapter = live_market_data(frames=base_frames())
        snap = adapter.get_market_snapshot("BTC/USDT", include_m5=False)
        self.assertIsNotNone(snap)
        self.assertLess(len(snap.h1), 50)      # short, but served

    def test_include_m5_is_keyword_only(self):
        adapter = live_market_data()
        with self.assertRaises(TypeError):
            adapter.get_market_snapshot("BTC/USDT", True)  # type: ignore[misc]


# --------------------------------------------------------------------------
# PersistencePort
# --------------------------------------------------------------------------

def _active_signal() -> ActiveSignal:
    return ActiveSignal(
        direction="LONG", score=12, decision="VALID", entry_price=100.0,
        sl=95.0, tp=115.0, regime="expansion_up", timestamp=1000.0,
        entry_mode="RETRACEMENT",
    )


def _ledger_record() -> SignalLedgerRecord:
    return SignalLedgerRecord(
        schema_version=2, strategy_version="2.0.1", recorded_at=1.0,
        symbol="BTC/USDT", h1_last_ts=5000.0, decision="SKIP", score=0,
        entry_mode="NONE", regime="unknown", session_active=False, live_price=1.0,
    )


def _ob_audit_record() -> OBAuditRecord:
    return OBAuditRecord(
        symbol="BTC/USDT", h1_last_ts=5000.0, direction="LONG",
        entry_mode="RETRACEMENT", decision="VALID", score=12,
        regime="expansion_up", location="discount", market_price=101.0,
        entry_price=100.0, sl=95.0, tp=115.0, rr=3.0,
        entry_source="retracement_ob", tp_source="SWING_HIGH", volume_spike=False,
    )


class TestPersistencePortStructure(unittest.TestCase):
    def test_port_declares_exactly_six_methods(self):
        self.assertEqual(
            list(port_methods(PersistencePort)),
            [
                "append_ob_audit", "append_signal_ledger",
                "load_active_signals", "load_killed_theses",
                "save_active_signals", "save_killed_theses",
            ],
        )

    def test_live_adapter_satisfies_the_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(live_persistence(tmp), PersistencePort)

    def test_live_adapter_signatures_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert_signature_matches(self, PersistencePort, live_persistence(tmp))

    def test_an_incomplete_object_does_not_satisfy_the_port(self):
        class Half:
            def load_active_signals(self, *, default_timestamp=None):
                return {}

        self.assertNotIsInstance(Half(), PersistencePort)

    def test_port_has_no_reader_for_the_observability_sinks(self):
        # Decision #010: audit and ledger records are never read back at runtime.
        names = port_methods(PersistencePort)
        self.assertNotIn("load_ob_audit", names)
        self.assertNotIn("load_signal_ledger", names)


class TestPersistencePortBehavior(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.impl = live_persistence(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_absent_storage_loads_empty(self):
        self.assertEqual(self.impl.load_active_signals(), {})
        self.assertEqual(self.impl.load_killed_theses(), {})

    def test_save_then_load_round_trips(self):
        self.impl.save_active_signals({"BTC/USDT": _active_signal()})
        self.impl.save_killed_theses({"K": KilledThesisRecord("K", 500.0)})
        self.assertIn("BTC/USDT", self.impl.load_active_signals())
        self.assertIn("K", self.impl.load_killed_theses())

    def test_default_timestamp_is_keyword_only(self):
        with self.assertRaises(TypeError):
            self.impl.load_active_signals(1.0)  # type: ignore[misc]

    def test_malformed_store_raises_persistence_error(self):
        (self.dir / "active_pois.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(PersistenceError):
            self.impl.load_active_signals()

    def test_appends_are_append_only(self):
        self.impl.append_signal_ledger(_ledger_record())
        self.impl.append_signal_ledger(_ledger_record())
        lines = (self.dir / "signal_ledger.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 2)
        json.loads(lines[0])            # each line is independently parseable

    def test_ob_audit_appends(self):
        self.impl.append_ob_audit(_ob_audit_record())
        lines = (self.dir / "retracement_ob_audit.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 1)

    def test_save_replaces_rather_than_merges(self):
        self.impl.save_active_signals({"A/USDT": _active_signal()})
        self.impl.save_active_signals({"B/USDT": _active_signal()})
        self.assertEqual(list(self.impl.load_active_signals()), ["B/USDT"])


class TestOrchestratorDependsOnPortsOnly(unittest.TestCase):
    """The point of M2: `app` names no concrete infra type in its signature."""

    def test_constructor_annotations_are_ports(self):
        from v2.app.orchestrator import Orchestrator

        hints = inspect.signature(Orchestrator.__init__).parameters
        self.assertEqual(hints["market_data"].annotation, "MarketDataPort")
        self.assertEqual(hints["persistence"].annotation, "PersistencePort")
        self.assertEqual(hints["notifier"].annotation, "Notifier")
        self.assertEqual(hints["clock"].annotation, "Clock")


if __name__ == "__main__":
    unittest.main()
