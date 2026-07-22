"""Tests for tests.regression.snapshot — deterministic input capture.

Everything runs in a temp directory; no committed file is written, no network.
Covers frame fidelity (including NaN ATR), MarketSnapshot reconstruction, M5
attachment, JSON round-trip, strict-JSON output, multi-snapshot loading, and
format guarding.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from tests.regression.runner import load_snapshots
from tests.regression.snapshot import FRAME_COLUMNS, InputSnapshot, _epoch
from v2.domain.market import MarketSnapshot


def make_frame(n: int, *, start: str = "2026-03-01 00:00", freq: str = "h") -> pd.DataFrame:
    """A V1-shaped OHLCV+ATR frame with NaN ATR in the first two rows."""
    ts = pd.date_range(start, periods=n, freq=freq)
    closes = [100.0 + i * 0.5 for i in range(n)]
    atr = [float("nan"), float("nan")] + [1.0 + 0.01 * i for i in range(n - 2)]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [c - 0.3 for c in closes],
            "high": [c + 0.7 for c in closes],
            "low": [c - 0.7 for c in closes],
            "close": closes,
            "v": [1000.0 + i for i in range(n)],
            "atr": atr,
        }
    )[list(FRAME_COLUMNS)]


class TestRoundTrip(unittest.TestCase):
    def _snapshot(self, *, with_m5: bool = False) -> InputSnapshot:
        return InputSnapshot.from_frames(
            symbol="BTC/USDT",
            d1=make_frame(8, start="2026-02-01", freq="D"),
            h1=make_frame(40),
            live_price=123.75,
            m5=make_frame(30, start="2026-03-02 10:00", freq="5min") if with_m5 else None,
        )

    def test_save_load_preserves_frames_and_nan(self):
        original = self._snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            original.save(path)
            loaded = InputSnapshot.load(path)

        ms_orig = original.to_market_snapshot()
        ms_loaded = loaded.to_market_snapshot()
        self.assertTrue(ms_orig.h1.equals(ms_loaded.h1))
        self.assertTrue(ms_orig.d1.equals(ms_loaded.d1))
        # NaN ATR in the first two rows survives the JSON round-trip.
        self.assertTrue(ms_loaded.h1["atr"].iloc[:2].isna().all())
        self.assertFalse(ms_loaded.h1["atr"].iloc[2:].isna().any())

    def test_reconstructs_market_snapshot_fields(self):
        snap = self._snapshot()
        ms = snap.to_market_snapshot()
        self.assertIsInstance(ms, MarketSnapshot)
        self.assertEqual(ms.symbol, "BTC/USDT")
        self.assertEqual(ms.live_price, 123.75)
        self.assertIsNone(ms.m5)
        last_ts = ms.h1["timestamp"].iloc[-1]
        self.assertEqual(ms.h1_last_ts, _epoch(last_ts))
        self.assertEqual(ms.h1_last_ts_dt, pd.to_datetime(last_ts))

    def test_m5_attached_round_trips(self):
        snap = self._snapshot(with_m5=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snap.json"
            snap.save(path)
            ms = InputSnapshot.load(path).to_market_snapshot()
        self.assertIsNotNone(ms.m5)
        self.assertEqual(len(ms.m5), 30)

    def test_from_market_snapshot(self):
        source = self._snapshot(with_m5=True).to_market_snapshot()
        snap = InputSnapshot.from_market_snapshot(source, source="live-v1")
        rebuilt = snap.to_market_snapshot()
        self.assertEqual(snap.source, "live-v1")
        self.assertTrue(rebuilt.h1.equals(source.h1))
        self.assertEqual(rebuilt.h1_last_ts, source.h1_last_ts)
        self.assertEqual(rebuilt.live_price, source.live_price)


class TestSerializationRules(unittest.TestCase):
    def test_output_is_strict_json_with_null_for_nan(self):
        snap = InputSnapshot.from_frames(
            symbol="ETH/USDT",
            d1=make_frame(6, start="2026-02-01", freq="D"),
            h1=make_frame(30),
            live_price=1.0,
        )
        text = json.dumps(snap.to_json_obj(), allow_nan=False)  # would raise if NaN leaked
        self.assertIn("null", text)
        self.assertNotIn("NaN", text)

    def test_unknown_format_rejected(self):
        with self.assertRaises(ValueError):
            InputSnapshot.from_json_obj({"format": 999})


class TestMultipleSnapshots(unittest.TestCase):
    def test_load_directory_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("b_eth", "a_btc"):
                InputSnapshot.from_frames(
                    symbol=name,
                    d1=make_frame(6, start="2026-02-01", freq="D"),
                    h1=make_frame(30),
                    live_price=1.0,
                ).save(base / f"{name}.json")
            snapshots = load_snapshots(base)
        self.assertEqual([s.symbol for s in snapshots], ["a_btc", "b_eth"])


if __name__ == "__main__":
    unittest.main()
