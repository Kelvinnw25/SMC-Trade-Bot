"""Phase 3.0 architecture: AnalysisContext construction and incremental enrichment.

Structure only — no analysis behavior is exercised.
"""
from __future__ import annotations

import dataclasses
import unittest

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.domain import MarketSnapshot, Regime


def make_snapshot(symbol: str = "TEST/USDT") -> MarketSnapshot:
    ts = pd.date_range("2026-01-01", periods=3, freq="h")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [1.0, 1.1, 1.2],
            "high": [2.0, 2.1, 2.2],
            "low": [0.5, 0.6, 0.7],
            "close": [1.5, 1.6, 1.7],
            "v": [10.0, 11.0, 12.0],
            "atr": [0.1, 0.1, 0.1],
        }
    )
    return MarketSnapshot(
        symbol=symbol,
        d1=frame,
        h1=frame,
        h1_last_ts=float(ts[-1].timestamp()),
        h1_last_ts_dt=ts[-1],
        live_price=1.75,
    )


class TestConstruction(unittest.TestCase):
    def test_initial_holds_input_and_nothing_derived(self):
        snapshot = make_snapshot()
        context = AnalysisContext.initial(snapshot)
        self.assertIs(context.snapshot, snapshot)
        derived = [
            f.name for f in dataclasses.fields(context) if f.name != "snapshot"
        ]
        for name in derived:
            with self.subTest(field=name):
                self.assertIsNone(getattr(context, name))

    def test_has_a_field_per_pipeline_stage(self):
        field_names = {f.name for f in dataclasses.fields(AnalysisContext)}
        expected = {
            "snapshot", "htf", "regime", "swing_highs", "swing_lows",
            "atr_last", "displacement_valid", "volume_spike", "sweep",
            "order_blocks", "fvg", "entry", "risk", "candidate", "signal",
        }
        self.assertEqual(field_names, expected)


class TestImmutabilityAndEnrichment(unittest.TestCase):
    def setUp(self) -> None:
        self.context = AnalysisContext.initial(make_snapshot())

    def test_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.context.htf = None  # type: ignore[misc]

    def test_with_returns_a_new_enriched_context(self):
        regime = Regime(label="unknown", entry_mode="NONE")
        enriched = self.context.with_(regime=regime)
        self.assertIsNot(enriched, self.context)
        self.assertEqual(enriched.regime, regime)
        # The original is untouched — enrichment never mutates.
        self.assertIsNone(self.context.regime)

    def test_enrichment_preserves_earlier_fields(self):
        regime = Regime(label="unknown", entry_mode="NONE")
        enriched = self.context.with_(regime=regime).with_(swing_highs=())
        self.assertEqual(enriched.regime, regime)
        self.assertEqual(enriched.swing_highs, ())
        self.assertIs(enriched.snapshot, self.context.snapshot)

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(TypeError):
            self.context.with_(not_a_stage_product=1)


if __name__ == "__main__":
    unittest.main()
