"""SignalLedgerRecord — schema, immutability, JSON round-trip, forward-compat."""
from __future__ import annotations

import dataclasses
import json
import unittest

from v2.domain import LEDGER_SCHEMA_VERSION, SignalLedgerRecord


def _rec(**over) -> SignalLedgerRecord:
    base = dict(
        schema_version=LEDGER_SCHEMA_VERSION,
        strategy_version="2.0.0",
        recorded_at=1_700_000_000.0,
        symbol="BTC/USDT",
        h1_last_ts=5000.0,
        decision="VALID",
        score=12,
        entry_mode="RETRACEMENT",
        regime="expansion_up",
        session_active=True,
        live_price=100.0,
    )
    base.update(over)
    return SignalLedgerRecord(**base)


#: The nine lineage keys added by schema 2 (REPLAY_CONTRACT.md C-16.1).
LINEAGE_KEYS = (
    "source", "run_id", "cycle_index", "universe_rank", "contract_version",
    "data_snapshot_id", "d1_rows", "h1_rows", "m5_rows",
)


def _schema1_line(**over) -> dict:
    """A genuine schema-1 JSONL line.

    Built by taking a schema-2 line and removing exactly the keys schema 2 added
    — which is faithful precisely because schema 2 is purely additive (C-16.1):
    every remaining key existed in schema 1 with the same meaning.
    """
    d = _rec(**over).to_jsonl_dict()
    for key in LINEAGE_KEYS:
        del d[key]
    d["schema_version"] = 1
    return d


class TestSchema(unittest.TestCase):
    def test_is_frozen(self):
        rec = _rec()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rec.score = 99  # type: ignore[misc]

    def test_minimal_required_fields_only(self):
        rec = _rec()
        # optionals default to None / () — a SKIP with no candidate is valid.
        self.assertIsNone(rec.direction)
        self.assertEqual(rec.notes, ())
        self.assertIsNone(rec.entry_price)
        self.assertIsNone(rec.selected_ob_top)

    def test_jsonl_dict_is_json_native(self):
        rec = _rec(notes=("weekly_aligned", "sweep"), sweep=True, entry_price=100.0)
        d = rec.to_jsonl_dict()
        # notes serialized as a list; the whole dict must json.dumps cleanly.
        self.assertEqual(d["notes"], ["weekly_aligned", "sweep"])
        s = json.dumps(d)  # must not raise
        self.assertIn('"symbol": "BTC/USDT"', s)

    def test_round_trip_equals(self):
        rec = _rec(
            direction="LONG", notes=("a", "b"), sweep=True, fresh_ob=False,
            entry_price=100.0, sl=95.0, tp=115.0, rr=4.0, tp_rr=3.2,
            selected_ob_top=100.0, selected_ob_bottom=98.0, selected_ob_index=5,
            selected_ob_rank=3, selected_ob_fresh=True, ob_candidate_count=4,
            sweep_reclaim_strength=0.6, atr_last=1.0, weekly_bias="BULLISH",
        )
        back = SignalLedgerRecord.from_jsonl_dict(json.loads(json.dumps(rec.to_jsonl_dict())))
        self.assertEqual(back, rec)

    def test_from_jsonl_ignores_unknown_and_defaults_missing(self):
        # A future line with an extra key, and an old line missing a newer key.
        d = _rec(direction="SHORT").to_jsonl_dict()
        d["some_future_field"] = 123        # unknown -> ignored
        del d["sweep_reclaim_strength"]     # missing -> default None
        back = SignalLedgerRecord.from_jsonl_dict(d)
        self.assertEqual(back.direction, "SHORT")
        self.assertIsNone(back.sweep_reclaim_strength)

    def test_notes_default_is_independent(self):
        a, b = _rec(), _rec()
        self.assertEqual(a.notes, ())
        self.assertEqual(b.notes, ())
        self.assertIsNot(a, b)


class TestSchemaVersion2Lineage(unittest.TestCase):
    """Milestone M1 — REPLAY_CONTRACT.md C-16.1 / C-16.5."""

    def test_schema_version_is_2(self):
        self.assertEqual(LEDGER_SCHEMA_VERSION, 2)

    def test_lineage_defaults(self):
        # A record built without lineage: only `source` has a non-None default,
        # because only live could have written a pre-lineage row (C-16.5).
        rec = _rec()
        self.assertEqual(rec.source, "live")
        for name in LINEAGE_KEYS:
            if name == "source":
                continue
            self.assertIsNone(getattr(rec, name), name)

    def test_lineage_round_trips(self):
        rec = _rec(
            source="replay", run_id="replay-0123456789abcdef", cycle_index=17,
            universe_rank=3, contract_version="1.1",
            data_snapshot_id="fedcba9876543210",
            d1_rows=119, h1_rows=499, m5_rows=199,
        )
        back = SignalLedgerRecord.from_jsonl_dict(
            json.loads(json.dumps(rec.to_jsonl_dict()))
        )
        self.assertEqual(back, rec)
        self.assertEqual(back.source, "replay")
        self.assertEqual(back.cycle_index, 17)
        self.assertEqual((back.d1_rows, back.h1_rows, back.m5_rows), (119, 499, 199))

    def test_lineage_keys_are_appended_last(self):
        # C-16.1: schema-2 keys are appended, never interleaved, so a diff of two
        # lines stays readable and key order is stable for schema-1 consumers.
        keys = list(_rec().to_jsonl_dict())
        self.assertEqual(tuple(keys[-9:]), LINEAGE_KEYS)
        self.assertEqual(keys[0], "schema_version")


class TestSchema1BackwardCompatibility(unittest.TestCase):
    """A schema-1 ledger must keep loading forever (C-16.5)."""

    def test_schema1_line_loads_with_documented_defaults(self):
        line = _schema1_line(direction="LONG", entry_price=100.0)
        self.assertNotIn("source", line)          # genuinely a v1 line
        self.assertEqual(line["schema_version"], 1)

        rec = SignalLedgerRecord.from_jsonl_dict(line)

        # Every schema-1 field keeps its value...
        self.assertEqual(rec.schema_version, 1)   # the row states what it is
        self.assertEqual(rec.symbol, "BTC/USDT")
        self.assertEqual(rec.decision, "VALID")
        self.assertEqual(rec.direction, "LONG")
        self.assertEqual(rec.entry_price, 100.0)
        # ...and the lineage defaults are exactly as the contract specifies.
        self.assertEqual(rec.source, "live")
        self.assertIsNone(rec.run_id)
        self.assertIsNone(rec.cycle_index)
        self.assertIsNone(rec.universe_rank)
        self.assertIsNone(rec.contract_version)   # NOT mislabeled as "1.1"
        self.assertIsNone(rec.data_snapshot_id)
        self.assertIsNone(rec.h1_rows)

    def test_schema1_and_schema2_lines_load_from_one_file(self):
        # The realistic case: an appended-to ledger holding both generations.
        lines = [_schema1_line(symbol="A/USDT"),
                 _rec(symbol="B/USDT", source="replay", cycle_index=1).to_jsonl_dict()]
        recs = [SignalLedgerRecord.from_jsonl_dict(l) for l in lines]
        self.assertEqual([r.symbol for r in recs], ["A/USDT", "B/USDT"])
        self.assertEqual([r.schema_version for r in recs], [1, 2])
        self.assertEqual([r.source for r in recs], ["live", "replay"])

    def test_schema1_reader_tolerates_a_schema2_line(self):
        # Forward compatibility: a reader that only knows the schema-1 keys reads
        # a schema-2 line by ignoring what it does not recognize.
        v2_line = _rec(source="replay", run_id="r", h1_rows=499).to_jsonl_dict()
        known = set(_schema1_line())
        projected = {k: v for k, v in v2_line.items() if k in known}
        rec = SignalLedgerRecord.from_jsonl_dict(projected)
        self.assertEqual(rec.symbol, "BTC/USDT")
        self.assertEqual(rec.source, "live")      # falls back to the default


if __name__ == "__main__":
    unittest.main()
