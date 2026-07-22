"""OBAuditRecord fidelity against the real V1 audit trail.

Every line of the repo's ``retracement_ob_audit.jsonl`` must survive
``from_v1_dict -> to_v1_dict`` with identical content AND identical key order.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.domain import fixtures
from v2.domain import OBAuditRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_FILE = REPO_ROOT / "retracement_ob_audit.jsonl"


class TestOBAuditAgainstRealTrail(unittest.TestCase):
    @unittest.skipUnless(AUDIT_FILE.exists(), "retracement_ob_audit.jsonl not present")
    def test_every_real_line_round_trips_exactly(self):
        lines = [
            line for line in
            AUDIT_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreater(len(lines), 0)
        for i, line in enumerate(lines):
            parsed = json.loads(line)
            with self.subTest(line=i, symbol=parsed.get("symbol")):
                out = OBAuditRecord.from_v1_dict(parsed).to_v1_dict()
                self.assertEqual(out, parsed)
                self.assertEqual(list(out.keys()), list(parsed.keys()))


class TestOBAuditContract(unittest.TestCase):
    def test_v1_key_order(self):
        out = fixtures.make_ob_audit().to_v1_dict()
        self.assertEqual(list(out.keys()), [
            "symbol", "h1_last_ts", "direction", "entry_mode", "decision",
            "score", "regime", "location", "market_price", "entry_price",
            "sl", "tp", "rr", "entry_source", "tp_source", "volume_spike",
            "window_bounds", "ob_meta", "ob_candidates", "selected_ob_index",
            "selected_ob_rank", "selected_ob_top", "selected_ob_bottom",
            "selected_ob_size", "fresh_ob", "fvg_valid", "current_price",
            "entry_equals_ob_bottom",
        ])

    def test_nested_shapes_serialize_like_v1(self):
        out = fixtures.make_ob_audit(
            window_bounds=(489, 499),
        ).to_v1_dict()
        self.assertEqual(out["window_bounds"],
                         {"start_index": 489, "end_index": 499})
        self.assertEqual(out["ob_meta"], {"top": 111.77, "bottom": 110.64})
        self.assertEqual(out["ob_candidates"][0]["rank_from_last"], 2)

    def test_round_trip_synthetic(self):
        record = fixtures.make_ob_audit()
        again = OBAuditRecord.from_v1_dict(record.to_v1_dict())
        self.assertEqual(record, again)


if __name__ == "__main__":
    unittest.main()
