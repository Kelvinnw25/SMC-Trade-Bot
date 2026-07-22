"""Unit tests for v2.infra.persistence — the V1 persistence adapter (Phase 2.3).

Every test operates inside a fresh temporary directory; no real project file is
touched. Covers load/save of active signals and killed theses, the OB-audit
append, missing files, malformed JSON, per-record skipping, exact V1 format,
exception translation, and immutable reconstruction.
"""
from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from v2.domain import ActiveSignal, KilledThesisRecord, OBAuditRecord
from v2.infra.persistence import PersistenceAdapter, PersistenceError


def make_active_signal(**overrides) -> ActiveSignal:
    base = dict(
        direction="SHORT", score=12, decision="VALID",
        entry_price=67.03, sl=67.66679878018542, tp=65.9522662600618,
        regime="expansion_down", timestamp=1781301522.5820653,
        entry_mode="CONTINUATION", miss_count=1, status="WATCH",
        alert_sent_tier="VALID", alert_sent_ts=1781298098.9254858,
        last_miss_h1_ts=1781298000.0, last_ob_audit_h1_ts=0.0,
        entry_source="continuation_micro_pullback",
        signal_id="SOL/USDT|CONTINUATION|1781294400.0",
    )
    base.update(overrides)
    return ActiveSignal(**base)


def make_ob_audit(**overrides) -> OBAuditRecord:
    base = dict(
        symbol="INTC/USDT", h1_last_ts=1780545600.0, direction="SHORT",
        entry_mode="RETRACEMENT", decision="WATCH", score=9,
        regime="accumulation", location="", market_price=111.18,
        entry_price=110.64, sl=112.82940170105435, tp=107.5180097668173,
        rr=1.4259558817731997, entry_source="retracement_ob",
        tp_source="EQL_cluster", volume_spike=False,
    )
    base.update(overrides)
    return OBAuditRecord(**base)


class PersistenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.adapter = PersistenceAdapter(self.base)
        self.active_pois = self.base / "active_pois.json"
        self.killed_thesis = self.base / "killed_thesis.json"
        self.ob_audit = self.base / "retracement_ob_audit.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestLoadActiveSignals(PersistenceTestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(self.adapter.load_active_signals(), {})

    def test_load_existing_v1_file(self):
        record = make_active_signal().to_v1_dict()
        self.active_pois.write_text(json.dumps({"SOL/USDT": record}, indent=4))
        loaded = self.adapter.load_active_signals()
        self.assertEqual(set(loaded), {"SOL/USDT"})
        sig = loaded["SOL/USDT"]
        self.assertIsInstance(sig, ActiveSignal)
        self.assertEqual(sig.direction, "SHORT")
        self.assertEqual(sig.score, 12.0)  # V1 coerces to float on load
        # V1 load drops these two fields.
        self.assertIsNone(sig.entry_source)
        self.assertIsNone(sig.signal_id)

    def test_loaded_objects_are_immutable(self):
        self.active_pois.write_text(
            json.dumps({"SOL/USDT": make_active_signal().to_v1_dict()})
        )
        sig = self.adapter.load_active_signals()["SOL/USDT"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sig.score = 99  # type: ignore[misc]

    def test_invalid_record_is_skipped(self):
        good = make_active_signal().to_v1_dict()
        bad = make_active_signal().to_v1_dict()
        del bad["sl"]  # missing required field -> V1 skips the record
        self.active_pois.write_text(json.dumps({"GOOD/USDT": good, "BAD/USDT": bad}))
        loaded = self.adapter.load_active_signals()
        self.assertEqual(set(loaded), {"GOOD/USDT"})

    def test_default_timestamp_injected_when_missing(self):
        record = make_active_signal().to_v1_dict()
        del record["timestamp"]
        self.active_pois.write_text(json.dumps({"SOL/USDT": record}))
        # Without an injected default, a timestamp-less record is skipped.
        self.assertEqual(self.adapter.load_active_signals(), {})
        # With one supplied by the caller, it loads using that value.
        loaded = self.adapter.load_active_signals(default_timestamp=123.0)
        self.assertEqual(loaded["SOL/USDT"].timestamp, 123.0)

    def test_malformed_json_raises(self):
        self.active_pois.write_text("{ this is not json")
        with self.assertRaises(PersistenceError):
            self.adapter.load_active_signals()

    def test_non_object_top_level_raises(self):
        self.active_pois.write_text("[1, 2, 3]")
        with self.assertRaises(PersistenceError):
            self.adapter.load_active_signals()


class TestSaveActiveSignals(PersistenceTestCase):
    def test_save_writes_exact_v1_format(self):
        signals = {"SOL/USDT": make_active_signal()}
        self.adapter.save_active_signals(signals)
        expected = {"SOL/USDT": make_active_signal().to_v1_dict()}
        # Byte-exact against V1's json.dump(indent=4).
        self.assertEqual(
            self.active_pois.read_text(encoding="utf-8"),
            json.dumps(expected, indent=4),
        )

    def test_saved_record_has_v1_key_order(self):
        self.adapter.save_active_signals({"SOL/USDT": make_active_signal()})
        raw = json.loads(self.active_pois.read_text(encoding="utf-8"))
        self.assertEqual(
            list(raw["SOL/USDT"].keys()),
            list(make_active_signal().to_v1_dict().keys()),
        )

    def test_round_trip_applies_v1_load_lossiness(self):
        original = make_active_signal()
        self.adapter.save_active_signals({"SOL/USDT": original})
        loaded = self.adapter.load_active_signals()["SOL/USDT"]
        # entry_source / signal_id are dropped on load, score becomes float —
        # exactly V1's own round-trip behavior. Everything else is preserved.
        self.assertIsNone(loaded.entry_source)
        self.assertIsNone(loaded.signal_id)
        self.assertEqual(loaded.direction, original.direction)
        self.assertEqual(loaded.entry_price, original.entry_price)
        self.assertEqual(loaded.miss_count, original.miss_count)

    def test_save_is_loadable_by_v1_gate(self):
        self.adapter.save_active_signals({"SOL/USDT": make_active_signal()})
        raw = json.loads(self.active_pois.read_text(encoding="utf-8"))["SOL/USDT"]
        for required in ("score", "decision", "entry_price", "sl", "tp", "direction"):
            self.assertIn(required, raw)

    def test_save_overwrites_previous(self):
        self.adapter.save_active_signals({"A/USDT": make_active_signal()})
        self.adapter.save_active_signals({"B/USDT": make_active_signal()})
        self.assertEqual(set(self.adapter.load_active_signals()), {"B/USDT"})

    def test_save_empty_writes_empty_object(self):
        self.adapter.save_active_signals({})
        self.assertEqual(self.active_pois.read_text(encoding="utf-8"), "{}")


class TestKilledTheses(PersistenceTestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(self.adapter.load_killed_theses(), {})

    def test_save_and_load_round_trip(self):
        key = "SOL/USDT|SHORT|CONTINUATION"
        theses = {key: KilledThesisRecord(key, 1781298000.0)}
        self.adapter.save_killed_theses(theses)
        loaded = self.adapter.load_killed_theses()
        self.assertEqual(loaded, theses)

    def test_save_writes_flat_dict(self):
        key = "SOL/USDT|SHORT|CONTINUATION"
        self.adapter.save_killed_theses({key: KilledThesisRecord(key, 100.0)})
        raw = json.loads(self.killed_thesis.read_text(encoding="utf-8"))
        self.assertEqual(raw, {key: 100.0})

    def test_malformed_value_is_skipped(self):
        self.killed_thesis.write_text(json.dumps({"A|LONG|RETRACEMENT": "not-a-number"}))
        self.assertEqual(self.adapter.load_killed_theses(), {})

    def test_malformed_json_raises(self):
        self.killed_thesis.write_text("{oops")
        with self.assertRaises(PersistenceError):
            self.adapter.load_killed_theses()


class TestOBAuditAppend(PersistenceTestCase):
    def test_append_creates_file_and_writes_one_line(self):
        record = make_ob_audit()
        self.adapter.append_ob_audit(record)
        lines = self.ob_audit.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), record.to_v1_dict())

    def test_append_multiple_preserves_order(self):
        self.adapter.append_ob_audit(make_ob_audit(symbol="A/USDT"))
        self.adapter.append_ob_audit(make_ob_audit(symbol="B/USDT"))
        lines = self.ob_audit.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["symbol"], "A/USDT")
        self.assertEqual(json.loads(lines[1])["symbol"], "B/USDT")

    def test_appended_line_round_trips_to_record(self):
        record = make_ob_audit()
        self.adapter.append_ob_audit(record)
        line = self.ob_audit.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(OBAuditRecord.from_v1_dict(json.loads(line)), record)


class TestExceptionTranslation(PersistenceTestCase):
    def test_read_failure_is_translated(self):
        # A directory where the file is expected -> open("r") raises OSError.
        self.active_pois.mkdir()
        with self.assertRaises(PersistenceError):
            self.adapter.load_active_signals()

    def test_write_failure_is_translated(self):
        # A directory occupying the temp-write path -> open("w") raises OSError.
        (self.base / "active_pois.json.tmp").mkdir()
        with self.assertRaises(PersistenceError):
            self.adapter.save_active_signals({"SOL/USDT": make_active_signal()})

    def test_audit_append_failure_is_translated(self):
        self.ob_audit.mkdir()  # can't append to a directory
        with self.assertRaises(PersistenceError):
            self.adapter.append_ob_audit(make_ob_audit())


if __name__ == "__main__":
    unittest.main()
