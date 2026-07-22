"""Phase 1 DoD: ActiveSignal serialization is schema-compatible with V1.

- Every record in the repo's real ``active_pois.json`` loads through
  ``from_v1_dict`` with results identical to V1's ``load_memory`` coercions.
- A V2-serialized record passes V1's ``load_memory`` validation logic.
- Round-trips are lossless modulo V1's OWN documented load-lossiness
  (``entry_source``/``signal_id`` dropped; score coerced to float).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict

from tests.domain import fixtures
from v2.domain import ActiveSignal, V1_REQUIRED_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_POIS = REPO_ROOT / "active_pois.json"

#: The V1 payload key order (main.py signal_payload literal + signal_id last).
V1_PAYLOAD_KEY_ORDER = [
    "direction", "score", "decision", "entry_price", "sl", "tp", "regime",
    "timestamp", "miss_count", "status", "alert_sent_tier", "alert_sent_ts",
    "last_miss_h1_ts", "last_ob_audit_h1_ts", "entry_mode", "entry_source",
    "signal_id",
]


def v1_load_coercion(data: Dict[str, Any]) -> Dict[str, Any]:
    """Test-local mirror of V1 ``load_memory``'s per-record transform
    (main.py:73-100), used as the expected-output oracle."""
    if not all(k in data for k in V1_REQUIRED_FIELDS):
        raise ValueError("missing required fields")
    return {
        "direction": str(data["direction"]),
        "score": float(data["score"]),
        "decision": str(data["decision"]),
        "entry_price": float(data["entry_price"]),
        "sl": float(data["sl"]),
        "tp": float(data["tp"]),
        "regime": str(data.get("regime", "unknown")),
        "timestamp": float(data["timestamp"]),  # time.time() default handled via injection
        "miss_count": int(data.get("miss_count", 0)),
        "last_miss_h1_ts": float(data.get("last_miss_h1_ts", 0.0)),
        "status": str(data.get("status", "WATCH")),
        "last_ob_audit_h1_ts": float(data.get("last_ob_audit_h1_ts", 0.0)),
        "entry_mode": str(data.get("entry_mode", "RETRACEMENT")),
        "alert_sent_tier": str(data.get("alert_sent_tier", data.get("decision", ""))),
        "alert_sent_ts": float(data.get("alert_sent_ts", 0.0)),
    }


class TestActiveSignalAgainstRealMemoryFile(unittest.TestCase):
    @unittest.skipUnless(ACTIVE_POIS.exists(), "active_pois.json not present")
    def test_real_records_load_like_v1(self):
        raw = json.loads(ACTIVE_POIS.read_text(encoding="utf-8"))
        self.assertGreater(len(raw), 0)
        for symbol, data in raw.items():
            with self.subTest(symbol=symbol):
                sig = ActiveSignal.from_v1_dict(data)
                expected = v1_load_coercion(data)
                for field_name, value in expected.items():
                    self.assertEqual(getattr(sig, field_name), value)
                # V1 load drops these two — None marks absence.
                self.assertIsNone(sig.entry_source)
                self.assertIsNone(sig.signal_id)

    @unittest.skipUnless(ACTIVE_POIS.exists(), "active_pois.json not present")
    def test_real_records_round_trip_to_v1_post_restart_shape(self):
        raw = json.loads(ACTIVE_POIS.read_text(encoding="utf-8"))
        for symbol, data in raw.items():
            with self.subTest(symbol=symbol):
                out = ActiveSignal.from_v1_dict(data).to_v1_dict()
                expected = {
                    k: v for k, v in data.items()
                    if k not in ("entry_source", "signal_id")
                }
                # int==float equality makes this exact in the V1 sense.
                self.assertEqual(out, expected)
                self.assertEqual(
                    list(out.keys()),
                    [k for k in V1_PAYLOAD_KEY_ORDER
                     if k not in ("entry_source", "signal_id")],
                )


class TestActiveSignalSerializationContract(unittest.TestCase):
    def test_fresh_payload_serializes_in_v1_key_order(self):
        out = fixtures.make_active_signal().to_v1_dict()
        self.assertEqual(list(out.keys()), V1_PAYLOAD_KEY_ORDER)

    def test_fresh_payload_preserves_stored_value_types(self):
        # V1 writes an int score on first insert; V2 must not coerce it.
        out = fixtures.make_active_signal(score=12).to_v1_dict()
        self.assertIsInstance(out["score"], int)

    def test_round_trip_applies_v1_load_lossiness_then_stabilizes(self):
        original = fixtures.make_active_signal(score=12)
        first = ActiveSignal.from_v1_dict(original.to_v1_dict())
        # V1 load coerces score to float and drops entry_source/signal_id.
        self.assertEqual(first.score, 12.0)
        self.assertIsInstance(first.score, float)
        self.assertIsNone(first.entry_source)
        self.assertIsNone(first.signal_id)
        for field_name in ("direction", "decision", "entry_price", "sl", "tp",
                           "regime", "timestamp", "miss_count", "status",
                           "alert_sent_tier", "alert_sent_ts",
                           "last_miss_h1_ts", "last_ob_audit_h1_ts",
                           "entry_mode"):
            self.assertEqual(getattr(first, field_name),
                             getattr(original, field_name))
        # Second pass is the identity (stable fixpoint).
        second = ActiveSignal.from_v1_dict(first.to_v1_dict())
        self.assertEqual(first, second)

    def test_post_restart_record_omits_dropped_keys(self):
        loaded = ActiveSignal.from_v1_dict(fixtures.make_active_signal().to_v1_dict())
        out = loaded.to_v1_dict()
        self.assertNotIn("entry_source", out)
        self.assertNotIn("signal_id", out)

    def test_v2_written_record_passes_v1_load_gate(self):
        """A V2-serialized record must be loadable by V1 (Phase 1 DoD)."""
        out = fixtures.make_active_signal().to_v1_dict()
        # Raises if the V1 required-field gate or coercions would fail.
        coerced = v1_load_coercion(out)
        self.assertEqual(coerced["direction"], "SHORT")

    def test_required_field_gate(self):
        data = fixtures.make_active_signal().to_v1_dict()
        del data["sl"]
        with self.assertRaises(ValueError):
            ActiveSignal.from_v1_dict(data)

    def test_coercion_failure_raises(self):
        data = fixtures.make_active_signal().to_v1_dict()
        data["entry_price"] = "not-a-number"
        with self.assertRaises(ValueError):
            ActiveSignal.from_v1_dict(data)

    def test_missing_timestamp_requires_injected_default(self):
        data = fixtures.make_active_signal().to_v1_dict()
        del data["timestamp"]
        with self.assertRaises(ValueError):
            ActiveSignal.from_v1_dict(data)  # domain cannot read the clock
        sig = ActiveSignal.from_v1_dict(data, default_timestamp=1781300000.0)
        self.assertEqual(sig.timestamp, 1781300000.0)

    def test_alert_sent_tier_defaults_to_decision(self):
        """V1: data.get("alert_sent_tier", data.get("decision", ""))."""
        data = fixtures.make_active_signal(decision="VALID").to_v1_dict()
        del data["alert_sent_tier"]
        sig = ActiveSignal.from_v1_dict(data)
        self.assertEqual(sig.alert_sent_tier, "VALID")

    def test_optional_field_defaults_match_v1(self):
        minimal = {
            "direction": "LONG", "score": 10, "decision": "WATCH",
            "entry_price": 1.0, "sl": 0.9, "tp": 1.2, "timestamp": 123.0,
        }
        sig = ActiveSignal.from_v1_dict(minimal)
        self.assertEqual(sig.regime, "unknown")
        self.assertEqual(sig.miss_count, 0)
        self.assertEqual(sig.last_miss_h1_ts, 0.0)
        self.assertEqual(sig.status, "WATCH")
        self.assertEqual(sig.last_ob_audit_h1_ts, 0.0)
        self.assertEqual(sig.entry_mode, "RETRACEMENT")
        self.assertEqual(sig.alert_sent_tier, "WATCH")  # falls back to decision
        self.assertEqual(sig.alert_sent_ts, 0.0)


if __name__ == "__main__":
    unittest.main()
