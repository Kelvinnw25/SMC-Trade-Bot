"""Tests for tests.regression.oracle + runner — the V1 golden oracle.

No network, no Telegram. Real V1 analysis runs only on the committed
deterministic fixture; the payload-projection, M5-gate, and kill-switch paths use
an injected fake strategy so they are exercised without depending on V1 emitting
a particular decision from synthetic data.
"""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from typing import Any, Optional

from tests.regression.oracle import Analyzer, V1Oracle, _alert_rank, _alert_tier
from tests.regression.runner import (
    compare_against_golden,
    compare_analyzers,
    load_golden,
)
from tests.regression.snapshot import FRAME_COLUMNS, InputSnapshot
from tests.regression.test_snapshot import make_frame

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNAPSHOT_FILE = FIXTURES / "synthetic_short.json"
GOLDEN_FILE = FIXTURES / "synthetic_short.golden.json"


class FakeStrategy:
    """Injected stand-in for SMCStrategy: returns a preset context, records the
    M5-enrichment call, and returns a preset kill verdict."""

    def __init__(self, context: dict[str, Any], *, kill: bool = False) -> None:
        self._context = context
        self.kill = kill
        self.enrich_calls = 0
        self.kill_calls = 0

    def build_signal_context(self, d1, h1) -> dict[str, Any]:
        return self._context

    def enrich_with_m5(self, context, m5) -> dict[str, Any]:
        self.enrich_calls += 1
        return dict(context["result"])

    def is_poi_invalidated(self, prior, context) -> bool:
        self.kill_calls += 1
        return self.kill


def valid_short_context() -> dict[str, Any]:
    """A context shaped like V1's, carrying a persisted-worthy VALID SHORT."""
    result = {
        "score": 13, "decision": "VALID", "direction": "SHORT",
        "notes": ["weekly_aligned", "sweep"], "regime": "rotational", "rr": 1.8,
        "entry_mode": "RETRACEMENT", "volume_spike": True,
        "entry_source": "retracement_ob", "tp_source": "EQL_cluster", "tp_rr": 1.9,
        "entry_price": 100.0, "sl": 103.0, "tp": 94.0, "entry_ready": True,
        "reason": "rr_pass",
    }
    return {
        "htf_map": {"weekly_bias": "BEARISH", "d1_bias": "BEARISH",
                    "h4_bias": "BEARISH", "location": "premium"},
        "regime": "rotational",
        "h1_setup": {"entry_mode": "RETRACEMENT", "atr_last": 2.0,
                     "volume_spike": True, "sweep_valid": True, "fvg_valid": True,
                     "displacement_valid": False, "fresh_ob": True,
                     "entry_source": "retracement_ob", "tp_source": "EQL_cluster"},
        "result": result,
        "debug": {
            "htf": {"weekly": "BEARISH", "d1": "BEARISH", "h4": "BEARISH",
                    "loc": "premium", "regime": "rotational", "dir": "SHORT",
                    "src": "weighted_bear", "bull": 1.0, "bear": 7.0,
                    "anchor_override": False},
            "conf": {"sweep": True, "fvg": True, "disp": False, "ob": True},
            "ob_audit": {"ob_index": 180, "ob_top": 101.0, "ob_bottom": 100.0,
                         "ob_candidates": [{"index": 180}]},
        },
    }


def m5_frame():
    return make_frame(30, start="2026-03-02 10:00", freq="5min")[list(FRAME_COLUMNS)]


class TestAnalyzerProtocol(unittest.TestCase):
    def test_v1_oracle_is_an_analyzer(self):
        self.assertIsInstance(V1Oracle(), Analyzer)

    def test_plain_object_is_not(self):
        self.assertNotIsInstance(object(), Analyzer)

    def test_alert_tier_table_matches_v1(self):
        # Pins the reproduced lookup to V1 main.py:32-50.
        self.assertEqual(_alert_tier("VALID"), "VALID")
        self.assertEqual(_alert_tier("A+"), "A+")
        self.assertEqual(_alert_tier("WATCH"), "")
        self.assertEqual(_alert_tier("SKIP"), "")
        self.assertEqual([_alert_rank(t) for t in ("", "VALID", "A+")], [0, 1, 2])


class TestV1OracleOnFixture(unittest.TestCase):
    def setUp(self):
        self.snapshot = InputSnapshot.load(SNAPSHOT_FILE)

    def test_matches_committed_golden(self):
        golden = load_golden(GOLDEN_FILE)
        report = compare_against_golden(self.snapshot, V1Oracle(), golden)
        self.assertTrue(report.ok, msg="\n".join(f"{f.path}: {f.detail}" for f in report.failures))

    def test_is_deterministic(self):
        # Two independent oracle runs on the same input never diverge — the
        # regression baseline every future V2 result is measured against.
        report = compare_analyzers(self.snapshot, V1Oracle(), V1Oracle())
        self.assertTrue(report.ok)

    def test_known_observable_outcome(self):
        ob = V1Oracle().analyze(self.snapshot.to_market_snapshot())
        self.assertEqual(ob.direction, "LONG")
        self.assertEqual(ob.regime, "rotational")
        self.assertEqual(ob.entry_mode, "RETRACEMENT")
        self.assertEqual(ob.decision, "SKIP")
        self.assertEqual(ob.weekly_bias, "BULLISH")
        self.assertIsNone(ob.persistence_payload)
        self.assertIsNone(ob.notification)


class TestPayloadProjection(unittest.TestCase):
    def setUp(self):
        self.market = InputSnapshot.load(SNAPSHOT_FILE).to_market_snapshot()

    def test_persistence_payload_shape(self):
        oracle = V1Oracle(FakeStrategy(valid_short_context()))
        ob = oracle.analyze(self.market)
        payload = ob.persistence_payload
        self.assertIsNotNone(payload)
        self.assertEqual(payload["direction"], "SHORT")
        self.assertEqual(payload["decision"], "VALID")
        self.assertEqual(payload["entry_price"], 100.0)
        self.assertEqual(payload["sl"], 103.0)
        self.assertEqual(payload["tp"], 94.0)
        self.assertEqual(payload["entry_mode"], "RETRACEMENT")
        self.assertEqual(payload["miss_count"], 0)
        self.assertEqual(payload["status"], "WATCH")
        self.assertEqual(payload["last_miss_h1_ts"], self.market.h1_last_ts)
        self.assertEqual(
            payload["signal_id"],
            f"{self.market.symbol}|RETRACEMENT|{self.market.h1_last_ts}",
        )
        # Wall-clock fields are intentionally excluded (validated later with an
        # injected clock, not here).
        self.assertNotIn("timestamp", payload)
        self.assertNotIn("alert_sent_ts", payload)

    def test_notification_payload_shape(self):
        ob = V1Oracle(FakeStrategy(valid_short_context())).analyze(self.market)
        note = ob.notification
        self.assertIsNotNone(note)
        self.assertEqual(note["kind"], "new")
        self.assertEqual(note["decision"], "VALID")
        self.assertEqual(note["direction"], "SHORT")
        self.assertEqual(note["entry_source"], "retracement_ob")
        self.assertEqual(note["tp_source"], "EQL_cluster")
        self.assertEqual(note["volume_spike"], True)
        self.assertEqual(note["htf"]["weekly"], "BEARISH")
        self.assertEqual(ob.alert_tier, "VALID")
        self.assertEqual(ob.alert_rank, 1)

    def test_watch_decision_persists_but_does_not_notify(self):
        context = valid_short_context()
        context["result"]["decision"] = "WATCH"
        ob = V1Oracle(FakeStrategy(context)).analyze(self.market)
        self.assertIsNotNone(ob.persistence_payload)  # WATCH is persisted
        self.assertIsNone(ob.notification)             # but tier "" -> no alert

    def test_invalid_levels_block_persistence(self):
        context = valid_short_context()
        context["result"]["tp"] = context["result"]["entry_price"]  # entry == tp
        ob = V1Oracle(FakeStrategy(context)).analyze(self.market)
        self.assertIsNone(ob.persistence_payload)
        self.assertIsNone(ob.notification)


class TestM5Gate(unittest.TestCase):
    def _market_with_m5(self):
        base = InputSnapshot.load(SNAPSHOT_FILE).to_market_snapshot()
        return dataclasses.replace(base, m5=m5_frame())

    def test_enriches_when_score_ge_8_and_m5_present(self):
        fake = FakeStrategy(valid_short_context())  # score 13
        V1Oracle(fake).analyze(self._market_with_m5())
        self.assertEqual(fake.enrich_calls, 1)

    def test_no_enrich_below_score_gate(self):
        context = valid_short_context()
        context["result"]["score"] = 5
        fake = FakeStrategy(context)
        V1Oracle(fake).analyze(self._market_with_m5())
        self.assertEqual(fake.enrich_calls, 0)

    def test_no_enrich_without_m5(self):
        fake = FakeStrategy(valid_short_context())  # score 13 but m5 is None
        V1Oracle(fake).analyze(InputSnapshot.load(SNAPSHOT_FILE).to_market_snapshot())
        self.assertEqual(fake.enrich_calls, 0)


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self.market = InputSnapshot.load(SNAPSHOT_FILE).to_market_snapshot()
        self.prior = {"direction": "SHORT", "sl": 105.0, "entry_price": 100.0}

    def test_kill_evaluated_only_with_prior(self):
        fake = FakeStrategy(valid_short_context(), kill=True)
        self.assertIsNone(V1Oracle(fake).analyze(self.market).kill_triggered)
        self.assertEqual(fake.kill_calls, 0)

    def test_kill_true_when_prior_and_invalidated(self):
        ob = V1Oracle(FakeStrategy(valid_short_context(), kill=True)).analyze(
            self.market, prior=self.prior
        )
        self.assertIs(ob.kill_triggered, True)

    def test_kill_false_when_prior_and_valid(self):
        ob = V1Oracle(FakeStrategy(valid_short_context(), kill=False)).analyze(
            self.market, prior=self.prior
        )
        self.assertIs(ob.kill_triggered, False)


if __name__ == "__main__":
    unittest.main()
