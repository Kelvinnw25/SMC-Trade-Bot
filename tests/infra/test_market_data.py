"""Unit tests for v2.infra.market_data — the V1 market-data adapter (Phase 2.2).

V1's BinanceHandler is replaced by an in-memory fake; no network call is ever
made. Covers successful retrieval, empty/malformed responses, timestamp
normalization, forming-candle strip, live-price, M5 attachment guards,
exception translation, the universe, and interface behavior.
"""
from __future__ import annotations

import unittest
from typing import Any, Optional

import pandas as pd

from v2.domain.market import MarketSnapshot
from v2.infra.market_data import MarketDataAdapter, MarketDataError, SymbolVolume


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

def make_frame(timestamps: pd.DatetimeIndex, *, drop_close: bool = False) -> pd.DataFrame:
    """A V1-shaped OHLCV+ATR frame with distinct, positional close values."""
    n = len(timestamps)
    closes = [100.0 + i for i in range(n)]
    data = {
        "timestamp": timestamps,
        "open": [c - 0.5 for c in closes],
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "v": [1000.0 + i for i in range(n)],
        "atr": [2.0] * n,
    }
    if drop_close:
        del data["close"]
    return pd.DataFrame(data)


class FakeHandler:
    """Stand-in for V1 BinanceHandler; returns preset frames, records calls."""

    def __init__(
        self,
        *,
        universe: Optional[list] = None,
        frames: Optional[dict] = None,
        raise_on: Optional[set] = None,
    ) -> None:
        self._universe = universe
        self._frames = frames or {}
        self._raise_on = raise_on or set()
        self.fetch_calls: list[tuple[str, str, int]] = []
        self.universe_calls: list[int] = []

    def get_top_volume_coins(self, limit: int) -> Any:
        self.universe_calls.append(limit)
        if "universe" in self._raise_on:
            raise RuntimeError("network down")
        return self._universe

    def fetch_data_with_atr(self, symbol: str, timeframe: str, limit: int) -> Any:
        self.fetch_calls.append((symbol, timeframe, limit))
        if timeframe in self._raise_on:
            raise RuntimeError("network down")
        return self._frames.get(timeframe)


# Reusable frames: H1 [00,01,02,03] -> forming 03:00, last closed 02:00.
H1_TS = pd.date_range("2026-07-22 00:00", periods=4, freq="h")
D1_TS = pd.date_range("2026-07-20", periods=3, freq="D")
EXPECTED_H1_LAST = pd.Timestamp("2026-07-22 02:00:00")


def base_frames(**overrides) -> dict:
    frames = {"1d": make_frame(D1_TS), "1h": make_frame(H1_TS)}
    frames.update(overrides)
    return frames


# --------------------------------------------------------------------------
# Snapshot retrieval
# --------------------------------------------------------------------------

class TestGetMarketSnapshot(unittest.TestCase):
    def _adapter(self, **frame_overrides) -> tuple[MarketDataAdapter, FakeHandler]:
        handler = FakeHandler(frames=base_frames(**frame_overrides))
        return MarketDataAdapter(handler), handler

    def test_successful_snapshot(self):
        adapter, _ = self._adapter()
        snap = adapter.get_market_snapshot("BTC/USDT")
        self.assertIsInstance(snap, MarketSnapshot)
        self.assertEqual(snap.symbol, "BTC/USDT")
        self.assertIsNone(snap.m5)

    def test_forming_candle_stripped(self):
        adapter, _ = self._adapter()
        snap = adapter.get_market_snapshot("BTC/USDT")
        # Raw had 4 H1 / 3 D1 rows; the forming candle is dropped from each.
        self.assertEqual(len(snap.h1), 3)
        self.assertEqual(len(snap.d1), 2)
        self.assertEqual(snap.h1["timestamp"].iloc[-1], EXPECTED_H1_LAST)

    def test_live_price_is_forming_candle_close(self):
        adapter, _ = self._adapter()
        snap = adapter.get_market_snapshot("BTC/USDT")
        # Forming candle is the 4th H1 row: close = 100.0 + 3.
        self.assertEqual(snap.live_price, 103.0)

    def test_timestamp_normalization(self):
        adapter, _ = self._adapter()
        snap = adapter.get_market_snapshot("BTC/USDT")
        self.assertIsInstance(snap.h1_last_ts_dt, pd.Timestamp)
        self.assertEqual(snap.h1_last_ts_dt, EXPECTED_H1_LAST)
        self.assertIsInstance(snap.h1_last_ts, float)
        # Same conversion path V1 used (pandas tz-naive -> UTC epoch).
        self.assertEqual(snap.h1_last_ts, EXPECTED_H1_LAST.timestamp())

    def test_uses_v1_fetch_limits_and_timeframes(self):
        adapter, handler = self._adapter()
        adapter.get_market_snapshot("BTC/USDT")
        self.assertIn(("BTC/USDT", "1d", 120), handler.fetch_calls)
        self.assertIn(("BTC/USDT", "1h", 500), handler.fetch_calls)

    def test_no_m5_fetch_when_not_requested(self):
        adapter, handler = self._adapter()
        adapter.get_market_snapshot("BTC/USDT")
        self.assertFalse(any(tf == "5m" for _, tf, _ in handler.fetch_calls))


# --------------------------------------------------------------------------
# Empty / insufficient responses
# --------------------------------------------------------------------------

class TestEmptyResponses(unittest.TestCase):
    def test_none_h1_returns_none(self):
        handler = FakeHandler(frames={"1d": make_frame(D1_TS), "1h": None})
        self.assertIsNone(MarketDataAdapter(handler).get_market_snapshot("BTC/USDT"))

    def test_none_d1_returns_none(self):
        handler = FakeHandler(frames={"1d": None, "1h": make_frame(H1_TS)})
        self.assertIsNone(MarketDataAdapter(handler).get_market_snapshot("BTC/USDT"))

    def test_empty_h1_frame_returns_none(self):
        handler = FakeHandler(
            frames={"1d": make_frame(D1_TS), "1h": make_frame(H1_TS.__class__([]))}
        )
        self.assertIsNone(MarketDataAdapter(handler).get_market_snapshot("BTC/USDT"))

    def test_single_row_h1_strips_to_empty_returns_none(self):
        # One raw H1 row -> no closed candle after the strip -> cannot build.
        handler = FakeHandler(
            frames={"1d": make_frame(D1_TS), "1h": make_frame(H1_TS[:1])}
        )
        self.assertIsNone(MarketDataAdapter(handler).get_market_snapshot("BTC/USDT"))


# --------------------------------------------------------------------------
# Malformed responses / exception translation
# --------------------------------------------------------------------------

class TestErrorTranslation(unittest.TestCase):
    def test_malformed_h1_missing_close_raises(self):
        handler = FakeHandler(
            frames={"1d": make_frame(D1_TS), "1h": make_frame(H1_TS, drop_close=True)}
        )
        with self.assertRaises(MarketDataError):
            MarketDataAdapter(handler).get_market_snapshot("BTC/USDT")

    def test_fetch_exception_wrapped(self):
        handler = FakeHandler(frames=base_frames(), raise_on={"1h"})
        with self.assertRaises(MarketDataError):
            MarketDataAdapter(handler).get_market_snapshot("BTC/USDT")

    def test_universe_exception_wrapped(self):
        handler = FakeHandler(universe=None, raise_on={"universe"})
        with self.assertRaises(MarketDataError):
            MarketDataAdapter(handler).get_top_volume_symbols(30)


# --------------------------------------------------------------------------
# M5 attachment guards
# --------------------------------------------------------------------------

class TestM5Attachment(unittest.TestCase):
    def _adapter(self, m5_frame) -> MarketDataAdapter:
        return MarketDataAdapter(FakeHandler(frames=base_frames(**{"5m": m5_frame})))

    def test_m5_attached_when_aligned(self):
        # 21 raw rows ending 03:00 -> last closed 02:55, floor 02:00 <= H1 02:00.
        m5 = make_frame(pd.date_range("2026-07-22 01:20", periods=21, freq="5min"))
        snap = self._adapter(m5).get_market_snapshot("BTC/USDT", include_m5=True)
        self.assertIsNotNone(snap.m5)
        self.assertEqual(len(snap.m5), 20)  # forming candle stripped
        self.assertEqual(snap.m5["timestamp"].iloc[-1], pd.Timestamp("2026-07-22 02:55"))

    def test_m5_dropped_when_misaligned(self):
        # 21 raw rows ending 03:05 -> last closed 03:00, floor 03:00 > H1 02:00.
        m5 = make_frame(pd.date_range("2026-07-22 01:25", periods=21, freq="5min"))
        snap = self._adapter(m5).get_market_snapshot("BTC/USDT", include_m5=True)
        self.assertIsNone(snap.m5)

    def test_m5_dropped_when_too_few_rows(self):
        m5 = make_frame(pd.date_range("2026-07-22 01:20", periods=19, freq="5min"))
        snap = self._adapter(m5).get_market_snapshot("BTC/USDT", include_m5=True)
        self.assertIsNone(snap.m5)

    def test_m5_none_when_handler_returns_none(self):
        handler = FakeHandler(frames=base_frames())  # no "5m" key -> None
        snap = MarketDataAdapter(handler).get_market_snapshot("BTC/USDT", include_m5=True)
        self.assertIsNone(snap.m5)

    def test_m5_fetch_only_when_requested(self):
        m5 = make_frame(pd.date_range("2026-07-22 01:20", periods=21, freq="5min"))
        adapter = self._adapter(m5)
        handler = adapter._handler  # type: ignore[attr-defined]
        adapter.get_market_snapshot("BTC/USDT", include_m5=True)
        self.assertIn(("BTC/USDT", "5m", 200), handler.fetch_calls)


# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------

class TestGetTopVolumeSymbols(unittest.TestCase):
    def test_translates_and_preserves_order(self):
        universe = [
            {"symbol": "BTC/USDT", "volume": 100.0},
            {"symbol": "ETH/USDT", "volume": 50.0},
        ]
        adapter = MarketDataAdapter(FakeHandler(universe=universe))
        result = adapter.get_top_volume_symbols(30)
        self.assertEqual(
            result,
            (SymbolVolume("BTC/USDT", 100.0), SymbolVolume("ETH/USDT", 50.0)),
        )

    def test_empty_universe_returns_empty_tuple(self):
        for empty in ([], None):
            adapter = MarketDataAdapter(FakeHandler(universe=empty))
            self.assertEqual(adapter.get_top_volume_symbols(30), ())

    def test_passes_limit_through(self):
        handler = FakeHandler(universe=[])
        MarketDataAdapter(handler).get_top_volume_symbols(7)
        self.assertEqual(handler.universe_calls, [7])

    def test_malformed_entry_raises(self):
        handler = FakeHandler(universe=[{"symbol": "BTC/USDT"}])  # no volume
        with self.assertRaises(MarketDataError):
            MarketDataAdapter(handler).get_top_volume_symbols(30)


# --------------------------------------------------------------------------
# Adapter-level value object
# --------------------------------------------------------------------------

class TestSymbolVolume(unittest.TestCase):
    def test_is_immutable(self):
        import dataclasses

        sv = SymbolVolume("BTC/USDT", 1.0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sv.volume = 2.0  # type: ignore[misc]

    def test_value_equality(self):
        self.assertEqual(SymbolVolume("BTC/USDT", 1.0), SymbolVolume("BTC/USDT", 1.0))
        self.assertNotEqual(SymbolVolume("BTC/USDT", 1.0), SymbolVolume("ETH/USDT", 1.0))


if __name__ == "__main__":
    unittest.main()
