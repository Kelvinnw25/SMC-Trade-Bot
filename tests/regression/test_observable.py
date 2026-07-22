"""Tests for tests.regression.observable — the comparison record + field map."""
from __future__ import annotations

import dataclasses
import math
import unittest

from tests.regression.observable import ObservableBehavior


def make_ob(**overrides) -> ObservableBehavior:
    """An ObservableBehavior with plausible defaults; override any field."""
    base = dict(
        symbol="BTC/USDT", h1_last_ts=1770692400.0, live_price=100.0,
        d1_rows=40, h1_rows=220, m5_rows=None, atr_last=2.0,
        weekly_bias="BULLISH", d1_bias="BULLISH", h4_bias="BULLISH",
        location="premium", regime="rotational", direction="LONG",
        direction_source="weighted_bull", bull=7.0, bear=1.0,
        anchor_override=False, veto_reason=None,
        sweep_valid=False, displacement_valid=False, fvg_valid=False,
        fresh_ob=False, m5_choch=None,
        ob_selected_index=None, ob_selected_top=None, ob_selected_bottom=None,
        ob_candidate_count=0,
        entry_mode="RETRACEMENT", entry_ready=False, entry_source="none",
        entry_price=0.0, sl=0.0, tp=None, tp_source=None, tp_rr=None, rr=None,
        score=0, decision="SKIP", reason="ob_quality_failed", notes=(),
        alert_tier="", alert_rank=0, kill_triggered=None,
        persistence_payload=None, notification=None,
    )
    base.update(overrides)
    return ObservableBehavior(**base)


class TestFieldMap(unittest.TestCase):
    def test_flattens_nested_payloads_and_notes(self):
        ob = make_ob(
            notes=("weekly_aligned", "sweep"),
            persistence_payload={"direction": "SHORT", "score": 13},
            notification={"kind": "new", "htf": {"weekly": "BEARISH"}},
        )
        fm = ob.as_field_map()
        self.assertEqual(fm["notes.0"], "weekly_aligned")
        self.assertEqual(fm["notes.1"], "sweep")
        self.assertEqual(fm["persistence_payload.direction"], "SHORT")
        self.assertEqual(fm["persistence_payload.score"], 13)
        self.assertEqual(fm["notification.kind"], "new")
        self.assertEqual(fm["notification.htf.weekly"], "BEARISH")

    def test_empty_container_and_none_leaves(self):
        fm = make_ob(notes=(), persistence_payload=None).as_field_map()
        self.assertEqual(fm["notes"], [])
        self.assertIsNone(fm["persistence_payload"])

    def test_nan_collapses_to_none(self):
        fm = make_ob(atr_last=float("nan")).as_field_map()
        self.assertIsNone(fm["atr_last"])

    def test_scalar_fields_present(self):
        fm = make_ob(score=12, decision="VALID").as_field_map()
        self.assertEqual(fm["score"], 12)
        self.assertEqual(fm["decision"], "VALID")
        self.assertEqual(fm["symbol"], "BTC/USDT")


class TestImmutability(unittest.TestCase):
    def test_is_frozen(self):
        ob = make_ob()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ob.score = 99  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
