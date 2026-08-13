"""lifecycle.rules — pure predicates, pinned to V1 where a V1 function exists.

The kill predicate is pinned field-by-field against the REAL
``SMCStrategy.is_poi_invalidated`` over a matrix; the tier/same-thesis/levels/TTL
rules are checked against V1's exact tables and branches.
"""
from __future__ import annotations

import itertools
import unittest

from tests.regression.v1_recorded import SMCStrategy
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.rules import (
    alert_rank,
    alert_tier,
    h1_gate_open,
    is_poi_invalidated,
    is_same_thesis,
    levels_valid,
    ttl_for,
)


class TestAlertTiering(unittest.TestCase):
    def test_alert_tier_table(self):
        self.assertEqual(alert_tier("VALID"), "VALID")
        self.assertEqual(alert_tier("A+"), "A+")
        self.assertEqual(alert_tier("WATCH"), "")
        self.assertEqual(alert_tier("SKIP"), "")
        self.assertEqual(alert_tier("valid"), "VALID")  # upper-cased

    def test_alert_rank_table(self):
        self.assertEqual(alert_rank(""), 0)
        self.assertEqual(alert_rank("VALID"), 1)
        self.assertEqual(alert_rank("A+"), 2)
        self.assertEqual(alert_rank("unknown"), 0)


class TestSameThesis(unittest.TestCase):
    def test_matches_on_direction_and_mode_case_insensitive(self):
        self.assertTrue(is_same_thesis("LONG", "RETRACEMENT", "long", "retracement"))

    def test_differs_on_direction(self):
        self.assertFalse(is_same_thesis("LONG", "RETRACEMENT", "SHORT", "RETRACEMENT"))

    def test_differs_on_mode(self):
        self.assertFalse(is_same_thesis("LONG", "RETRACEMENT", "LONG", "CONTINUATION"))


class TestLevelsValid(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(levels_valid(100.0, 95.0, 110.0))

    def test_rejects_non_positive(self):
        self.assertFalse(levels_valid(0.0, 95.0, 110.0))
        self.assertFalse(levels_valid(100.0, 0.0, 110.0))
        self.assertFalse(levels_valid(100.0, 95.0, 0.0))

    def test_rejects_entry_equal_to_sl_or_tp(self):
        self.assertFalse(levels_valid(100.0, 100.0, 110.0))
        self.assertFalse(levels_valid(100.0, 95.0, 100.0))

    def test_rejects_non_finite(self):
        self.assertFalse(levels_valid(float("inf"), 95.0, 110.0))
        self.assertFalse(levels_valid(100.0, float("nan"), 110.0))


class TestTTLTiering(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = LifecycleConfig()

    def test_manual_action_wins(self):
        self.assertEqual(
            ttl_for("MANUAL_ACTION", 5, self.cfg), self.cfg.manual_action_ttl_seconds
        )

    def test_tapped_when_missed(self):
        self.assertEqual(ttl_for("WATCH", 1, self.cfg), self.cfg.tapped_ttl_seconds)

    def test_untapped_when_no_miss(self):
        self.assertEqual(ttl_for("WATCH", 0, self.cfg), self.cfg.untapped_ttl_seconds)


class TestH1Gate(unittest.TestCase):
    def test_open_when_new_candle(self):
        self.assertTrue(h1_gate_open(200.0, 100.0))

    def test_closed_when_same_or_older(self):
        self.assertFalse(h1_gate_open(100.0, 100.0))
        self.assertFalse(h1_gate_open(50.0, 100.0))


class TestKillPredicatePinnedToV1(unittest.TestCase):
    """`is_poi_invalidated` must equal V1's `SMCStrategy.is_poi_invalidated`
    (strategy.py:1964-2013) for every input combination."""

    def setUp(self) -> None:
        self.strategy = SMCStrategy()

    def _v1(self, *, direction, sl, live_price, entry_price, d1, h4, atr) -> bool:
        saved = {"direction": direction, "sl": sl}
        context = {
            "market_price": live_price,
            "result": {"entry_price": entry_price},
            "htf_map": {"d1_bias": d1, "h4_bias": h4},
            "h1_setup": {"atr_last": atr},
        }
        return bool(self.strategy.is_poi_invalidated(saved, context))

    def test_matches_v1_over_matrix(self):
        directions = ["LONG", "SHORT", None, "long"]
        sls = [95.0, 105.0, 0.0]
        live_prices = [100.0, 90.0, 110.0, 0.0, -1.0]
        entry_prices = [100.0, 0.0]
        biases = ["BULLISH", "BEARISH", "CHOPPY"]
        atrs = [0.0, 2.0]

        checked = 0
        for direction, sl, live, entry, d1, h4, atr in itertools.product(
            directions, sls, live_prices, entry_prices, biases, biases, atrs
        ):
            expected = self._v1(
                direction=direction, sl=sl, live_price=live,
                entry_price=entry, d1=d1, h4=h4, atr=atr,
            )
            actual = is_poi_invalidated(
                direction=direction, sl=sl, live_price=live,
                fallback_entry_price=entry, d1_bias=d1, h4_bias=h4, atr_last=atr,
            )
            self.assertEqual(
                actual, expected,
                f"mismatch dir={direction} sl={sl} live={live} entry={entry} "
                f"d1={d1} h4={h4} atr={atr}: V2={actual} V1={expected}",
            )
            checked += 1
        self.assertGreater(checked, 500)

    def test_none_sl_returns_false_like_v1(self):
        # STALE not-ready path: V1 read result.get("sl") == None -> float(None)
        # raises -> except -> False.
        self.assertFalse(
            is_poi_invalidated(
                direction="SHORT", sl=None, live_price=100.0,
                fallback_entry_price=0.0, d1_bias="CHOPPY", h4_bias="CHOPPY",
                atr_last=0.0,
            )
        )
        # V1 agrees:
        self.assertFalse(
            self.strategy.is_poi_invalidated(
                {"direction": "SHORT", "sl": None},
                {"market_price": 100.0, "result": {}, "htf_map": {}, "h1_setup": {}},
            )
        )


if __name__ == "__main__":
    unittest.main()
