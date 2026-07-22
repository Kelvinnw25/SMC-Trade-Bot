"""KilledThesisRecord — V1 key format and killed_thesis.json item round-trip."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from v2.domain import KilledThesisRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
KILLED_FILE = REPO_ROOT / "killed_thesis.json"


class TestKilledThesisRecord(unittest.TestCase):
    def test_v1_key_format_no_transforms(self):
        """V1: f"{symbol}|{direction}|{entry_mode}" — no casing/whitespace
        changes applied."""
        self.assertEqual(
            KilledThesisRecord.make_key("SOL/USDT", "SHORT", "CONTINUATION"),
            "SOL/USDT|SHORT|CONTINUATION",
        )
        # V1 kill-path uses stored values verbatim, whatever their casing.
        self.assertEqual(
            KilledThesisRecord.make_key("x/USDT", "short", "retracement"),
            "x/USDT|short|retracement",
        )

    def test_item_round_trip(self):
        rec = KilledThesisRecord(
            thesis_key="SOL/USDT|SHORT|CONTINUATION", killed_ts=1781298000.0
        )
        key, ts = rec.to_v1_item()
        self.assertEqual(KilledThesisRecord.from_v1_item(key, ts), rec)

    def test_from_v1_item_coerces_like_v1(self):
        """V1 compares via float(ts) — string/int timestamps coerce."""
        rec = KilledThesisRecord.from_v1_item("A/USDT|LONG|RETRACEMENT", "123")
        self.assertEqual(rec.killed_ts, 123.0)
        self.assertIsInstance(rec.killed_ts, float)

    @unittest.skipUnless(KILLED_FILE.exists(), "killed_thesis.json not present")
    def test_real_file_items_round_trip(self):
        raw = json.loads(KILLED_FILE.read_text(encoding="utf-8"))
        for key, ts in raw.items():
            with self.subTest(key=key):
                rec = KilledThesisRecord.from_v1_item(key, ts)
                out_key, out_ts = rec.to_v1_item()
                self.assertEqual(out_key, key)
                self.assertEqual(out_ts, float(ts))


if __name__ == "__main__":
    unittest.main()
