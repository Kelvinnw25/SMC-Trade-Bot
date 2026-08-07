"""Tests for the HistoricalMarketDataAdapter (Milestone M6).

The centrepiece is `TestLiveVersusHistoricalParity`: it drives the **real** live
adapter — `MarketDataAdapter` over the real `BinanceMarketDataSource`, with only
`requests.get` stubbed — and the historical adapter over a repository holding the
same candles, then compares the two `MarketSnapshot`s field by field. Stubbing at
the HTTP boundary rather than faking the source means live's own parsing, float
casting, timestamp conversion and ATR call are all exercised, so the comparison
tests the production path instead of a re-implementation of it.

Two fixture rules make that comparison meaningful:

- every series **ends with the candle that is still forming at `T`**, because
  live fetches it and then strips it (`iloc[:-1]`). A series that stopped at the
  last closed candle would make live strip a *closed* one and drift by a bar.
- temporary directories are cleaned with `ignore_cleanup_errors`: the repository
  memory-maps its datasets and holds the mapping, which keeps the files locked on
  Windows for the lifetime of the process.

Hermetic: no network, no wall clock, no real Binance.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest.data.repository import (
    FORMAT_VERSION, KLINE_DTYPE, KlineRepository, MANIFEST_NAME,
    compute_snapshot_id, dataset_path,
)
from backtest.replay import (
    D1_ROWS, FRAME_COLUMNS, H1_ROWS, HistoricalMarketDataAdapter,
    M5_ROWS, ReplayDataError,
)
from v2.infra.config import ATR_PERIOD
from v2.infra.market_data import (
    BinanceMarketDataSource, MarketDataAdapter, MarketDataError, MarketDataPort,
    SymbolVolume,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
M5_MS = 300_000

#: 2026-03-01T00:00:00Z — on an hour and on a day, so it is a valid scan
#: boundary for all three timeframes at once.
T0 = 1_772_323_200
T0_MS = T0 * 1000

_RAW_COLUMNS = ["timestamp", "open", "high", "low", "close", "v",
                "ct", "qv", "nt", "tb", "tq", "i"]


# ---------------------------------------------------------------------------
# Candle fixtures — one deterministic series, shared by both adapters
# ---------------------------------------------------------------------------

def series(first_open_ms: int, interval_ms: int, count: int,
           *, base: float = 100.0, qv: float = 1000.0) -> list[tuple]:
    """A deterministic series with genuinely varying ranges, so ATR is not a
    constant and a mismatch between the adapters would be visible."""
    rows = []
    for index in range(count):
        drift = (index % 17) - 8
        wobble = (index % 7) * 0.5
        open_ = base + drift
        close = base + drift + (1.0 if index % 3 else -1.5)
        high = max(open_, close) + wobble + 0.75
        low = min(open_, close) - wobble - 0.5
        open_ms = first_open_ms + index * interval_ms
        rows.append((
            open_ms, open_, high, low, close, 10.0 + index % 5,
            open_ms + interval_ms - 1, qv + index, 100 + index,
            5.0, 500.0, 0.0,
        ))
    return rows


def ending_at(interval_ms: int, count: int, *, end_open_ms: int = T0_MS,
              **kw) -> list[tuple]:
    """`count` candles whose LAST one opens at `end_open_ms` — i.e. the candle
    still forming at that boundary, which is what the exchange would return."""
    return series(end_open_ms - (count - 1) * interval_ms, interval_ms, count,
                  **kw)


def h1_series(count: int, **kw) -> list[tuple]:
    return ending_at(HOUR_MS, count, **kw)


def open_ms(frame: pd.DataFrame) -> np.ndarray:
    """Open times as epoch milliseconds, independent of the datetime resolution
    pandas happens to use (pandas 3 yields `datetime64[ms]`, pandas 2 `[ns]`)."""
    return frame["timestamp"].to_numpy().astype("datetime64[ms]").astype("int64")


def write_repository(root: Path, datasets: dict[tuple[str, str], list[tuple]],
                     *, builder_version: str = "m4.2-test") -> KlineRepository:
    """A minimal canonical repository, written the way M4.2 writes one."""
    meta: dict[str, dict] = {}
    for (symbol, timeframe), rows in datasets.items():
        array = np.array(rows, dtype=KLINE_DTYPE)
        path = dataset_path(root, symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, array)
        meta[f"{symbol}|{timeframe}"] = {
            "rows": len(array),
            "first_open_ms": int(array["timestamp"][0]),
            "last_open_ms": int(array["timestamp"][-1]),
            "raw_sha256": [],
        }
    manifest = {
        "format_version": FORMAT_VERSION,
        "builder_version": builder_version,
        "data_snapshot_id": compute_snapshot_id(
            builder_version=builder_version, datasets=meta),
        "datasets": meta,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return KlineRepository(root)


class RepositoryCase(unittest.TestCase):
    """Temporary storage that tolerates the repository's retained mappings."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def repository(self, datasets, *, name: str = "repo") -> KlineRepository:
        return write_repository(self.tmp / name, datasets)

    def positioned(self, datasets, *, boundary: int = T0, name: str = "repo",
                   **kw) -> HistoricalMarketDataAdapter:
        adapter = HistoricalMarketDataAdapter(self.repository(datasets,
                                                              name=name), **kw)
        adapter.set_time(boundary)
        return adapter


class AdapterCase(RepositoryCase):
    """One full repository (D1 + H1 + M5), one adapter positioned at T0."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "repo"
        self.repo = write_repository(self.root, self.datasets())
        self.adapter = HistoricalMarketDataAdapter(self.repo)
        self.adapter.set_time(T0)

    def datasets(self) -> dict[tuple[str, str], list[tuple]]:
        return {
            ("BTCUSDT", "1d"): ending_at(DAY_MS, 130),
            ("BTCUSDT", "1h"): ending_at(HOUR_MS, 600),
            ("BTCUSDT", "5m"): ending_at(M5_MS, 250),
        }

    def snapshot(self, symbol: str = "BTC/USDT", **kw):
        return self.adapter.get_market_snapshot(symbol, **kw)


# ==========================================================================
# TASK 1 — the port contract
# ==========================================================================

class TestPortConformance(AdapterCase):

    def test_the_adapter_satisfies_market_data_port(self):
        self.assertIsInstance(self.adapter, MarketDataPort)

    def test_the_live_adapter_satisfies_the_same_port(self):
        self.assertIsInstance(MarketDataAdapter(BinanceMarketDataSource()),
                              MarketDataPort)

    def test_neither_adapter_inherits_from_the_protocol(self):
        """Both satisfy `MarketDataPort` by duck typing. Inheritance would turn
        a contract into a base class and invite shared implementation."""
        for cls in (HistoricalMarketDataAdapter, MarketDataAdapter):
            self.assertNotIn(MarketDataPort, cls.__mro__)

    def test_set_time_is_not_part_of_the_port(self):
        """C-7: positioning is driver-only. A `set_time` in the Protocol would
        leak replay into every production consumer."""
        self.assertFalse(hasattr(MarketDataPort, "set_time"))
        self.assertFalse(hasattr(MarketDataAdapter, "set_time"))


# ==========================================================================
# TASK 4 / TASK 5 — closed-candle semantics and window behaviour
# ==========================================================================

class TestWindows(AdapterCase):

    def test_window_sizes_are_the_closed_candle_counts(self):
        snap = self.snapshot(include_m5=True)
        self.assertEqual(len(snap.d1), D1_ROWS)
        self.assertEqual(len(snap.h1), H1_ROWS)
        self.assertEqual(len(snap.m5), M5_ROWS)

    def test_no_candle_that_had_not_elapsed_at_T_is_served(self):
        snap = self.snapshot(include_m5=True)
        for frame, interval in ((snap.d1, DAY_MS), (snap.h1, HOUR_MS),
                                (snap.m5, M5_MS)):
            self.assertTrue(bool((open_ms(frame) + interval <= T0_MS).all()))

    def test_the_last_candle_of_each_frame_is_the_one_that_closed_at_T(self):
        snap = self.snapshot(include_m5=True)
        for frame, interval in ((snap.d1, DAY_MS), (snap.h1, HOUR_MS),
                                (snap.m5, M5_MS)):
            self.assertEqual(int(open_ms(frame)[-1]), T0_MS - interval)

    def test_rows_are_strictly_increasing(self):
        snap = self.snapshot(include_m5=True)
        for frame in (snap.d1, snap.h1, snap.m5):
            self.assertTrue(bool((np.diff(open_ms(frame)) > 0).all()))

    def test_short_history_truncates_and_never_pads(self):
        adapter = self.positioned(
            {("NEWUSDT", "1d"): ending_at(DAY_MS, 12),
             ("NEWUSDT", "1h"): ending_at(HOUR_MS, 30)}, name="short")
        snap = adapter.get_market_snapshot("NEW/USDT")
        self.assertEqual(len(snap.d1), 11)      # 12 minus the forming candle
        self.assertEqual(len(snap.h1), 29)

    def test_a_gap_shortens_the_window_and_is_never_filled(self):
        rows = ending_at(HOUR_MS, 600)
        with_gap = rows[:400] + rows[410:]      # ten hours simply absent
        adapter = self.positioned(
            {("GAPUSDT", "1d"): ending_at(DAY_MS, 130),
             ("GAPUSDT", "1h"): with_gap}, name="gap")
        snap = adapter.get_market_snapshot("GAP/USDT")
        self.assertEqual(len(snap.h1), H1_ROWS)
        times = open_ms(snap.h1)
        # The window spans more wall time than 499 hours, and holds no filler.
        self.assertGreater(int(times[-1]) - int(times[0]),
                           (H1_ROWS - 1) * HOUR_MS)
        self.assertFalse(bool(snap.h1[list(FRAME_COLUMNS[1:6])].isna().any().any()))
        self.assertEqual(int(np.count_nonzero(np.diff(times) != HOUR_MS)), 1)

    def test_a_missing_symbol_is_none_not_an_error(self):
        self.assertIsNone(self.snapshot("NOPE/USDT"))

    def test_a_boundary_before_all_history_is_none(self):
        adapter = HistoricalMarketDataAdapter(self.repo)
        adapter.set_time(T0 - 5000 * 3600)
        self.assertIsNone(adapter.get_market_snapshot("BTC/USDT"))

    def test_a_symbol_with_h1_but_no_d1_is_none(self):
        """C-6.7 / decision R-5: live would return an empty d1 and the
        orchestrator would skip on `len(d1) < 10`. Same outcome, simpler rule."""
        adapter = self.positioned({("ODDUSDT", "1h"): ending_at(HOUR_MS, 600)},
                                  name="nod1")
        self.assertIsNone(adapter.get_market_snapshot("ODD/USDT"))

    def test_the_adapter_applies_no_sufficiency_guard(self):
        """C-6.8: the `D1 < 10` / `H1 < 50` guards belong to the orchestrator.
        A snapshot is returned even when it is far too short to analyse."""
        adapter = self.positioned(
            {("TINYUSDT", "1d"): ending_at(DAY_MS, 3),
             ("TINYUSDT", "1h"): ending_at(HOUR_MS, 4)}, name="tiny")
        snap = adapter.get_market_snapshot("TINY/USDT")
        self.assertIsNotNone(snap)
        self.assertEqual((len(snap.d1), len(snap.h1)), (2, 3))


class TestSnapshotTimeFields(AdapterCase):

    def test_h1_last_ts_is_the_reference_candle_open_time(self):
        """C-5.3: `h1_last_ts == T - 3600`, the OPEN time of `C_ref`, not `T`."""
        self.assertEqual(self.snapshot().h1_last_ts, float(T0 - 3600))

    def test_h1_last_ts_dt_matches_the_frame(self):
        snap = self.snapshot()
        self.assertEqual(snap.h1_last_ts_dt, snap.h1["timestamp"].iloc[-1])

    def test_h1_last_ts_dt_is_tz_naive(self):
        self.assertIsNone(self.snapshot().h1_last_ts_dt.tz)

    def test_live_price_is_the_reference_candle_close(self):
        snap = self.snapshot()
        self.assertEqual(snap.live_price, float(snap.h1["close"].iloc[-1]))

    def test_h1_last_ts_follows_the_frame_when_history_has_a_hole_at_T(self):
        """If the candle that should have closed at `T` does not exist, live's
        `h1["timestamp"].iloc[-1]` is the previous one — so replay reports that
        too, rather than a timestamp for a candle nobody holds (finding H1)."""
        rows = ending_at(HOUR_MS, 600)
        without_c_ref = rows[:-2] + rows[-1:]   # the T-3600 candle is missing
        adapter = self.positioned(
            {("HOLEUSDT", "1d"): ending_at(DAY_MS, 130),
             ("HOLEUSDT", "1h"): without_c_ref}, name="hole")
        snap = adapter.get_market_snapshot("HOLE/USDT")
        self.assertEqual(snap.h1_last_ts, float(T0 - 2 * 3600))
        self.assertEqual(snap.h1_last_ts_dt, snap.h1["timestamp"].iloc[-1])


class TestM5Attachment(AdapterCase):

    def test_m5_is_absent_unless_requested(self):
        self.assertIsNone(self.snapshot(include_m5=False).m5)

    def test_m5_attaches_when_the_guards_pass(self):
        snap = self.snapshot(include_m5=True)
        self.assertIsNotNone(snap.m5)
        self.assertEqual(self.adapter.counters.m5_attached, 1)
        self.assertEqual(self.adapter.counters.m5_guard_failures, 0)

    def test_the_last_m5_candle_precedes_the_boundary(self):
        snap = self.snapshot(include_m5=True)
        self.assertEqual(int(open_ms(snap.m5)[-1]), T0_MS - M5_MS)

    def test_the_hour_alignment_guard_holds_at_the_boundary(self):
        """C-5.5.2: at `T + 1s` the guard always passes, which is why replay is
        *more* M5-complete than live rather than less."""
        snap = self.snapshot(include_m5=True)
        self.assertLessEqual(snap.m5["timestamp"].iloc[-1].floor("h"),
                             snap.h1_last_ts_dt.floor("h"))

    def test_fewer_than_nineteen_closed_m5_rows_detaches(self):
        adapter = self.positioned(
            {("BTCUSDT", "1d"): ending_at(DAY_MS, 130),
             ("BTCUSDT", "1h"): ending_at(HOUR_MS, 600),
             ("BTCUSDT", "5m"): ending_at(M5_MS, 15)}, name="m5short")
        snap = adapter.get_market_snapshot("BTC/USDT", include_m5=True)
        self.assertIsNone(snap.m5)
        self.assertEqual(adapter.counters.m5_too_short, 1)

    def test_no_m5_dataset_detaches_without_error(self):
        adapter = self.positioned(
            {("BTCUSDT", "1d"): ending_at(DAY_MS, 130),
             ("BTCUSDT", "1h"): ending_at(HOUR_MS, 600)}, name="nom5")
        self.assertIsNone(adapter.get_market_snapshot("BTC/USDT",
                                                      include_m5=True).m5)


# ==========================================================================
# TASK 3 — ATR parity
# ==========================================================================

class TestAtr(AdapterCase):

    def test_atr_is_the_last_column(self):
        self.assertEqual(tuple(self.snapshot().h1.columns), FRAME_COLUMNS)

    def test_atr_is_float64(self):
        self.assertEqual(self.snapshot().h1["atr"].dtype, np.dtype("float64"))

    def test_the_first_thirteen_values_are_nan(self):
        atr = self.snapshot().h1["atr"]
        self.assertTrue(bool(atr.iloc[:ATR_PERIOD - 1].isna().all()))
        self.assertFalse(bool(atr.iloc[ATR_PERIOD - 1:].isna().any()))

    def test_atr_is_computed_on_exactly_the_served_rows(self):
        """C-6.5: after slicing, never on longer history then sliced."""
        h1 = self.snapshot().h1
        expected = ta.atr(h1["high"], h1["low"], h1["close"], length=ATR_PERIOD)
        self.assertTrue(np.array_equal(h1["atr"].to_numpy(),
                                       expected.to_numpy(), equal_nan=True))

    def test_compute_then_strip_equals_slice_then_compute(self):
        """The equivalence the whole design rests on: live computes ATR on the
        raw frame including the forming candle and then strips it; replay
        computes on the closed rows only. ATR's RMA is causal, so the two agree
        bit for bit — which is why no ATR is ever stored (C-6.5)."""
        raw = pd.DataFrame(ending_at(HOUR_MS, 600), columns=_RAW_COLUMNS)
        raw = raw[["timestamp", "open", "high", "low", "close", "v"]].astype(float)
        live_style = ta.atr(raw["high"], raw["low"], raw["close"],
                            length=ATR_PERIOD).iloc[:-1]
        sliced = raw.iloc[:-1]
        replay_style = ta.atr(sliced["high"], sliced["low"], sliced["close"],
                              length=ATR_PERIOD)
        self.assertTrue(np.array_equal(live_style.to_numpy(),
                                       replay_style.to_numpy(), equal_nan=True))

    def test_atr_is_not_stored(self):
        self.assertNotIn("atr", KLINE_DTYPE.names)


class TestFrameShape(AdapterCase):

    def test_columns_and_order(self):
        snap = self.snapshot(include_m5=True)
        for frame in (snap.d1, snap.h1, snap.m5):
            self.assertEqual(tuple(frame.columns), FRAME_COLUMNS)

    def test_dtypes(self):
        snap = self.snapshot(include_m5=True)
        for frame in (snap.d1, snap.h1, snap.m5):
            self.assertEqual(frame["timestamp"].dtype.kind, "M")
            for name in FRAME_COLUMNS[1:]:
                self.assertEqual(frame[name].dtype, np.dtype("float64"))

    def test_index_is_a_fresh_range_index(self):
        snap = self.snapshot(include_m5=True)
        for frame in (snap.d1, snap.h1, snap.m5):
            self.assertIsInstance(frame.index, pd.RangeIndex)
            self.assertEqual(frame.index[0], 0)
            self.assertEqual(len(frame.index), len(frame))

    def test_the_storage_only_columns_never_reach_analysis(self):
        """C-6.4: `ct, qv, nt, tb, tq, i` are storage. Live drops them, so any
        column-order-sensitive code downstream would diverge if replay kept
        them."""
        snap = self.snapshot(include_m5=True)
        for frame in (snap.d1, snap.h1, snap.m5):
            for dropped in ("ct", "qv", "nt", "tb", "tq", "i"):
                self.assertNotIn(dropped, frame.columns)


# ==========================================================================
# TASK 7 — live versus historical parity, through the real live adapter
# ==========================================================================

class TestLiveVersusHistoricalParity(RepositoryCase):
    """Same candles in, indistinguishable MarketSnapshot out."""

    def setUp(self) -> None:
        super().setUp()
        self.bars = {
            "1d": ending_at(DAY_MS, 130),
            "1h": ending_at(HOUR_MS, 600),
            "5m": ending_at(M5_MS, 250),
        }
        self.historical = self.positioned({
            ("BTCUSDT", timeframe): rows
            for timeframe, rows in self.bars.items()
        })

    def _binance_response(self, url, **kwargs):
        """What Binance would return at wall time `T + 1s`: every candle whose
        open time is <= T, last `limit` of them — so the final row is the one
        still forming, exactly as live sees it."""
        query = parse_qs(urlparse(url).query)
        timeframe = query["interval"][0]
        limit = int(query["limit"][0])
        rows = [r for r in self.bars[timeframe] if r[0] <= T0_MS][-limit:]
        payload = [[str(value) for value in row] for row in rows]
        return mock.Mock(status_code=200, json=mock.Mock(return_value=payload))

    def live_snapshot(self, *, include_m5: bool):
        adapter = MarketDataAdapter(BinanceMarketDataSource())
        with mock.patch("v2.infra.market_data.requests.get",
                        side_effect=self._binance_response):
            return adapter.get_market_snapshot("BTC/USDT", include_m5=include_m5)

    def historical_snapshot(self, *, include_m5: bool):
        return self.historical.get_market_snapshot("BTC/USDT",
                                                   include_m5=include_m5)

    # -- fixture guards ---------------------------------------------------

    def test_the_stub_feeds_the_live_adapter_a_forming_candle(self):
        """Guard on the fixture itself: if the stub omitted the forming candle,
        live's `iloc[:-1]` would strip a closed one and the whole comparison
        would silently be off by a bar."""
        with mock.patch("v2.infra.market_data.requests.get",
                        side_effect=self._binance_response):
            for timeframe, limit in (("1d", 120), ("1h", 500), ("5m", 200)):
                raw = BinanceMarketDataSource().fetch_data_with_atr(
                    "BTC/USDT", timeframe, limit)
                self.assertEqual(int(open_ms(raw)[-1]), T0_MS, timeframe)

    # -- the comparison ---------------------------------------------------

    def test_frames_are_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            pd.testing.assert_frame_equal(getattr(live, name),
                                          getattr(replay, name),
                                          check_exact=True, obj=name)

    def test_columns_and_order_are_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            self.assertEqual(list(getattr(live, name).columns),
                             list(getattr(replay, name).columns), name)

    def test_dtypes_are_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            self.assertTrue(getattr(live, name).dtypes.equals(
                getattr(replay, name).dtypes), name)

    def test_the_index_is_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            pd.testing.assert_index_equal(getattr(live, name).index,
                                          getattr(replay, name).index)

    def test_ohlcv_values_are_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            for column in ("open", "high", "low", "close", "v"):
                self.assertTrue(np.array_equal(
                    getattr(live, name)[column].to_numpy(),
                    getattr(replay, name)[column].to_numpy()), f"{name}.{column}")

    def test_the_atr_values_and_nan_mask_are_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            left = getattr(live, name)["atr"]
            right = getattr(replay, name)["atr"]
            self.assertTrue(np.array_equal(left.isna().to_numpy(),
                                           right.isna().to_numpy()), name)
            self.assertTrue(np.array_equal(left.to_numpy(), right.to_numpy(),
                                           equal_nan=True), name)

    def test_time_fields_are_identical(self):
        live = self.live_snapshot(include_m5=False)
        replay = self.historical_snapshot(include_m5=False)
        self.assertEqual(live.h1_last_ts, replay.h1_last_ts)
        self.assertEqual(live.h1_last_ts_dt, replay.h1_last_ts_dt)
        self.assertEqual(type(live.h1_last_ts), type(replay.h1_last_ts))
        self.assertEqual(live.h1_last_ts, float(T0 - 3600))

    def test_live_price_differs_only_by_the_declared_substitution(self):
        """C-5.5.3: live reads the forming candle's close, which is
        unreconstructible at H1 cadence; replay uses `close(C_ref)`. This is the
        one declared fidelity loss in the snapshot, asserted here so it stays
        declared rather than drifting into an accident."""
        live = self.live_snapshot(include_m5=False)
        replay = self.historical_snapshot(include_m5=False)
        forming_close = float(
            [r for r in self.bars["1h"] if r[0] == T0_MS][0][4])
        self.assertEqual(live.live_price, forming_close)
        self.assertEqual(replay.live_price, float(replay.h1["close"].iloc[-1]))

    def test_the_snapshot_shape_is_identical(self):
        live = self.live_snapshot(include_m5=True)
        replay = self.historical_snapshot(include_m5=True)
        self.assertEqual(type(live), type(replay))
        self.assertEqual(live.symbol, replay.symbol)
        self.assertEqual((len(live.d1), len(live.h1), len(live.m5)),
                         (len(replay.d1), len(replay.h1), len(replay.m5)))

    def test_m5_attachment_decision_is_identical(self):
        for include in (False, True):
            self.assertEqual(self.live_snapshot(include_m5=include).m5 is None,
                             self.historical_snapshot(include_m5=include).m5 is None)


# ==========================================================================
# C-6.9 — universe reconstruction
# ==========================================================================

class TestUniverse(RepositoryCase):

    def test_symbols_rank_by_trailing_24_hour_quote_volume(self):
        adapter = self.positioned({
            ("AAAUSDT", "1h"): h1_series(48, qv=100.0),
            ("BBBUSDT", "1h"): h1_series(48, qv=900.0),
            ("CCCUSDT", "1h"): h1_series(48, qv=500.0),
        })
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["BBB/USDT", "CCC/USDT", "AAA/USDT"])

    def test_the_volume_is_the_sum_over_exactly_24_closed_candles(self):
        rows = h1_series(48, qv=1000.0)
        adapter = self.positioned({("AAAUSDT", "1h"): rows})
        expected = sum(row[7] for row in rows[-25:-1])   # 24 closed, not 25
        self.assertEqual(adapter.get_top_volume_symbols(1)[0].volume, expected)

    def test_a_symbol_without_a_full_day_is_excluded_entirely(self):
        """C-6.9: never ranked on a partial sum, which would under-rank new
        listings and make their entry depend on the window's start date."""
        adapter = self.positioned({
            ("AAAUSDT", "1h"): h1_series(48, qv=100.0),
            ("NEWUSDT", "1h"): h1_series(20, qv=99999.0),
        })
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["AAA/USDT"])

    def test_exactly_24_closed_candles_qualifies(self):
        adapter = self.positioned({("AAAUSDT", "1h"): h1_series(25, qv=1.0)})
        self.assertEqual(len(adapter.get_top_volume_symbols(10)), 1)

    def test_equal_volumes_break_by_name_ascending(self):
        adapter = self.positioned({
            ("ZZZUSDT", "1h"): h1_series(48, qv=100.0),
            ("AAAUSDT", "1h"): h1_series(48, qv=100.0),
            ("MMMUSDT", "1h"): h1_series(48, qv=100.0),
        })
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["AAA/USDT", "MMM/USDT", "ZZZ/USDT"])

    def test_the_limit_is_applied_after_ranking(self):
        adapter = self.positioned({
            (f"S{index:02d}USDT", "1h"): h1_series(48, qv=float(index))
            for index in range(10)
        })
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(3)],
                         ["S09/USDT", "S08/USDT", "S07/USDT"])

    def test_entries_are_symbol_volume_in_live_format(self):
        adapter = self.positioned({("BTCUSDT", "1h"): h1_series(48)})
        entry = adapter.get_top_volume_symbols(1)[0]
        self.assertIsInstance(entry, SymbolVolume)
        self.assertEqual(entry.symbol, "BTC/USDT")
        self.assertIsInstance(entry.volume, float)

    def test_eligibility_moves_with_the_boundary(self):
        datasets = {
            ("AAAUSDT", "1h"): h1_series(48, qv=100.0),
            ("BBBUSDT", "1h"): h1_series(30, qv=100.0),
        }
        self.assertEqual(len(self.positioned(datasets).get_top_volume_symbols(10)), 2)
        earlier = self.positioned(datasets, boundary=T0 - 10 * 3600,
                                  name="earlier")
        self.assertEqual([e.symbol for e in earlier.get_top_volume_symbols(10)],
                         ["AAA/USDT"])

    def test_an_empty_universe_is_an_empty_tuple(self):
        adapter = self.positioned({("AAAUSDT", "1h"): h1_series(48)},
                                  boundary=T0 - 5000 * 3600)
        self.assertEqual(adapter.get_top_volume_symbols(10), ())

    def test_non_usdt_and_dated_contracts_are_filtered_like_live(self):
        adapter = self.positioned({
            ("BTCUSDT", "1h"): h1_series(48, qv=100.0),
            ("BTCBUSD", "1h"): h1_series(48, qv=900.0),
            ("BTCUSDT_240628", "1h"): h1_series(48, qv=900.0),
        })
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["BTC/USDT"])

    def test_a_static_universe_pins_the_candidate_set(self):
        adapter = self.positioned({
            ("AAAUSDT", "1h"): h1_series(48, qv=100.0),
            ("BBBUSDT", "1h"): h1_series(48, qv=900.0),
        }, static_universe=("AAAUSDT",))
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["AAA/USDT"])

    def test_the_universe_is_a_pure_function_of_the_data(self):
        datasets = {(f"S{i}USDT", "1h"): h1_series(48, qv=float(i % 3))
                    for i in range(9)}
        first = self.positioned(datasets, name="a").get_top_volume_symbols(9)
        second = self.positioned(datasets, name="b").get_top_volume_symbols(9)
        self.assertEqual(first, second)


# ==========================================================================
# Finding A1 — a delisted instrument must not rank on frozen volume
# ==========================================================================

class TestStaleSymbolsAreNotRanked(RepositoryCase):
    """`window` is row-based (C-6.3), so before the fix a dead symbol kept
    returning its final 24 candles at every later `T` and ranked on them
    forever. Ranking now also requires the newest of those rows to open inside
    the trailing day.

    Every fixture below gives the stale symbol a *higher* qv than the live one,
    so each test is its own control: without the recency test the stale symbol
    would rank first, not be absent.
    """

    def test_a_delisted_symbol_does_not_rank(self):
        adapter = self.positioned({
            ("LIVEUSDT", "1h"): h1_series(48, qv=1.0),
            ("DEADUSDT", "1h"): h1_series(
                48, qv=999_999.0, end_open_ms=T0_MS - 5_000 * HOUR_MS),
        })
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["LIVE/USDT"])

    def test_a_symbol_trading_at_T_is_unaffected(self):
        """The regression half: surviving symbols keep their exact volume."""
        rows = h1_series(48, qv=1000.0)
        adapter = self.positioned({("LIVEUSDT", "1h"): rows})
        expected = sum(row[7] for row in rows[-25:-1])   # 24 closed, not 25
        entries = adapter.get_top_volume_symbols(10)
        self.assertEqual([e.symbol for e in entries], ["LIVE/USDT"])
        self.assertEqual(entries[0].volume, expected)

    def test_a_symbol_alive_at_T_but_delisted_later_still_participates(self):
        """The look-ahead half. Positioned inside the symbol's life it ranks
        normally; nothing about its later death is visible at `T`."""
        death = T0_MS - 100 * HOUR_MS
        datasets = {
            ("LIVEUSDT", "1h"): h1_series(4_000, qv=1.0),
            ("DOOMEDUSDT", "1h"): h1_series(4_000, qv=500.0, end_open_ms=death),
        }
        alive = self.positioned(datasets, name="alive",
                                boundary=(death // 1000) - 3600)
        self.assertIn("DOOMED/USDT",
                      [e.symbol for e in alive.get_top_volume_symbols(10)])
        after = self.positioned(datasets, name="after")
        self.assertNotIn("DOOMED/USDT",
                         [e.symbol for e in after.get_top_volume_symbols(10)])

    def test_the_newest_row_may_open_exactly_at_the_trailing_boundary(self):
        adapter = self.positioned({("EDGEUSDT", "1h"): h1_series(
            48, qv=5.0, end_open_ms=T0_MS - 24 * HOUR_MS)})
        self.assertEqual([e.symbol for e in adapter.get_top_volume_symbols(10)],
                         ["EDGE/USDT"])

    def test_one_hour_older_than_the_trailing_boundary_is_excluded(self):
        adapter = self.positioned({("EDGEUSDT", "1h"): h1_series(
            48, qv=5.0, end_open_ms=T0_MS - 25 * HOUR_MS)})
        self.assertEqual(adapter.get_top_volume_symbols(10), ())

    def test_a_gap_longer_than_a_day_suspends_ranking_until_data_resumes(self):
        """Stated rather than left implicit: staleness is staleness. A symbol
        with no candle in the trailing day has no trailing-day volume, whatever
        the reason, and live's ticker would report ~0 for it rather than a sum
        from before the outage."""
        adapter = self.positioned({("GAPUSDT", "1h"): h1_series(
            48, qv=5.0, end_open_ms=T0_MS - 30 * HOUR_MS)})
        self.assertEqual(adapter.get_top_volume_symbols(10), ())

    def test_the_test_reads_no_row_after_T(self):
        """Rows after `T` cannot influence the result — the same universe comes
        back whether or not the future exists in the repository."""
        past = h1_series(48, qv=7.0)
        future = h1_series(148, qv=7.0, end_open_ms=T0_MS + 100 * HOUR_MS)
        self.assertEqual(
            self.positioned({("AAAUSDT", "1h"): past}, name="p")
                .get_top_volume_symbols(10),
            self.positioned({("AAAUSDT", "1h"): future}, name="f")
                .get_top_volume_symbols(10))


@unittest.skipUnless((Path("data/stage1") / "canonical").is_dir(),
                     "requires the Stage 2 corpus at data/stage1")
class TestStaleSymbolsOnTheRealCorpus(unittest.TestCase):
    """The three instruments finding A1 named, at the shakedown's own `T_0`."""

    #: 2023-01-01T00:00:00Z — the coverage-validated replay start.
    T_0 = 1_672_531_200
    DEAD = ("LUNAUSDT", "ANCUSDT", "KEEPUSDT")

    def setUp(self) -> None:
        self.adapter = HistoricalMarketDataAdapter(
            KlineRepository(Path("data/stage1") / "canonical"))

    def test_none_of_the_delisted_symbols_rank_at_T0(self):
        self.adapter.set_time(self.T_0)
        ranked = {e.symbol for e in self.adapter.get_top_volume_symbols(30)}
        for native in self.DEAD:
            self.assertNotIn(f"{native[:-4]}/USDT", ranked, native)

    def test_the_universe_is_still_full(self):
        """The slots the dead symbols held go to live instruments, not nowhere."""
        self.adapter.set_time(self.T_0)
        self.assertEqual(30, len(self.adapter.get_top_volume_symbols(30)))

    def test_luna_ranked_while_it_was_still_trading(self):
        """Its exclusion is recency, not a blanket ban: at a boundary inside
        LUNA's life it ranks normally."""
        self.adapter.set_time(1_650_000_000 // 3600 * 3600)   # 2022-04-15
        ranked = {e.symbol for e in self.adapter.get_top_volume_symbols(30)}
        self.assertIn("LUNA/USDT", ranked)


# ==========================================================================
# Positioning, determinism, failure modes, lineage
# ==========================================================================

class TestPositioning(AdapterCase):

    def test_a_boundary_off_the_hour_is_refused(self):
        adapter = HistoricalMarketDataAdapter(self.repo)
        with self.assertRaises(ValueError):
            adapter.set_time(T0 + 1)

    def test_the_boundary_must_strictly_increase(self):
        for value in (T0, T0 - 3600):
            with self.assertRaises(ValueError):
                self.adapter.set_time(value)

    def test_using_the_adapter_before_positioning_raises_loudly(self):
        """A `ValueError`, not a `MarketDataError`: the orchestrator swallows
        `MarketDataError` per symbol, so a driver bug would disappear. This
        surfaces at the universe call, which is outside that isolation."""
        adapter = HistoricalMarketDataAdapter(self.repo)
        for call in (lambda: adapter.get_top_volume_symbols(30),
                     lambda: adapter.get_market_snapshot("BTC/USDT")):
            with self.assertRaises(ValueError):
                call()
        self.assertFalse(issubclass(ValueError, MarketDataError))

    def test_counters_reset_on_each_boundary(self):
        self.snapshot(include_m5=True)
        self.assertEqual(self.adapter.counters.snapshots_served, 1)
        self.adapter.set_time(T0 + 3600)
        self.assertEqual(self.adapter.counters.snapshots_served, 0)
        self.assertEqual(self.adapter.counters.m5_attached, 0)

    def test_the_boundary_is_readable(self):
        self.assertEqual(self.adapter.boundary, T0)


class TestDeterminism(AdapterCase):

    def test_repeated_calls_are_bit_identical(self):
        first = self.snapshot(include_m5=True)
        second = self.snapshot(include_m5=True)
        for name in ("d1", "h1", "m5"):
            pd.testing.assert_frame_equal(getattr(first, name),
                                          getattr(second, name),
                                          check_exact=True)

    def test_two_adapters_over_one_repository_agree(self):
        other = HistoricalMarketDataAdapter(self.repo)
        other.set_time(T0)
        pd.testing.assert_frame_equal(self.snapshot().h1,
                                      other.get_market_snapshot("BTC/USDT").h1,
                                      check_exact=True)

    def test_frames_are_independent_between_calls(self):
        """I-10: a consumer that mutates a frame must not affect the next call."""
        first = self.snapshot()
        first.h1.loc[0, "close"] = -999.0
        self.assertNotEqual(float(self.snapshot().h1.loc[0, "close"]), -999.0)

    def test_the_adapter_never_mutates_the_repository(self):
        before = np.array(self.repo.window("BTCUSDT", "1h", end_ms=T0_MS,
                                           count=10))
        self.snapshot(include_m5=True)
        after = self.repo.window("BTCUSDT", "1h", end_ms=T0_MS, count=10)
        self.assertTrue(np.array_equal(before, after))

    def test_a_later_boundary_serves_a_different_window(self):
        """No cache may return a frame built for another T (I-8)."""
        first = self.snapshot().h1["timestamp"].iloc[-1]
        self.adapter.set_time(T0 + 3600)
        self.assertNotEqual(self.snapshot().h1["timestamp"].iloc[-1], first)


class TestFailureModes(AdapterCase):

    def test_a_corrupt_dataset_raises_replay_data_error(self):
        dataset_path(self.root, "BTCUSDT", "1h").write_bytes(b"not an npy file")
        adapter = HistoricalMarketDataAdapter(KlineRepository(self.root))
        adapter.set_time(T0)
        with self.assertRaises(ReplayDataError):
            adapter.get_market_snapshot("BTC/USDT")

    def test_replay_data_error_is_a_market_data_error(self):
        """So the orchestrator's existing handling applies unchanged (§7)."""
        self.assertTrue(issubclass(ReplayDataError, MarketDataError))

    def test_absence_is_never_reported_as_corruption(self):
        self.assertIsNone(self.snapshot("GHOST/USDT"))


class TestLineage(AdapterCase):

    def test_the_snapshot_id_comes_from_the_repository(self):
        self.assertEqual(self.adapter.data_snapshot_id,
                         self.repo.data_snapshot_id)

    def test_lineage_carries_provenance_and_the_replay_instant(self):
        lineage = self.adapter.lineage
        self.assertEqual(lineage.data_snapshot_id, self.repo.data_snapshot_id)
        self.assertEqual(lineage.builder_version, "m4.2-test")
        self.assertEqual(lineage.replay_timestamp, T0)
        self.assertEqual(lineage.repository_root, str(self.root))

    def test_lineage_tracks_the_boundary(self):
        self.adapter.set_time(T0 + 7200)
        self.assertEqual(self.adapter.lineage.replay_timestamp, T0 + 7200)

    def test_lineage_holds_no_strategy_information(self):
        self.assertEqual(set(self.adapter.lineage.to_dict()),
                         {"data_snapshot_id", "builder_version",
                          "repository_root", "replay_timestamp"})


# ==========================================================================
# TASK 8 — boundaries
# ==========================================================================

def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, parsed rather than grepped — prose must not
    decide an architecture test."""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestBoundaries(unittest.TestCase):
    #: The adapter module specifically — not the whole `backtest.replay` package,
    #: which also holds the replay driver (M7) and legitimately depends on more.
    #: The invariant being asserted is the *adapter's* isolation.
    ADAPTER = Path("backtest") / "replay" / "adapter.py"

    #: Everything the adapter is allowed to depend on. An allow-list rather than
    #: a deny-list: I-9's "reads nothing downstream" is only provable by naming
    #: what it *may* read.
    ALLOWED = {
        "__future__", "dataclasses", "typing", "numpy", "pandas", "pandas_ta",
        "backtest.data.repository",
        "v2.domain.market", "v2.infra.config", "v2.infra.market_data",
    }

    def test_the_adapter_imports_only_the_repository_and_the_port_types(self):
        for module in imported_modules(self.ADAPTER):
            self.assertIn(module, self.ALLOWED, module)

    def test_production_never_imports_replay(self):
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"),
                                 f"{path} imports {module}")

    def test_the_adapter_touches_no_network_and_no_wall_clock(self):
        """I-8: no network, no wall clock. Neither `requests` nor `time` nor
        `datetime` appears in the adapter's import graph at all."""
        imported = imported_modules(self.ADAPTER)
        for banned in ("requests", "urllib", "http", "socket", "time",
                       "datetime", "random"):
            self.assertNotIn(banned, {m.split(".")[0] for m in imported})

    def test_the_adapter_holds_no_analysis_lifecycle_or_app_dependency(self):
        """C-7.2: the data boundary and nothing else."""
        for module in imported_modules(self.ADAPTER):
            for forbidden in ("v2.analysis", "v2.lifecycle", "v2.app",
                              "backtest.acquire", "backtest.canonical"):
                self.assertFalse(module.startswith(forbidden), module)

    def test_window_sizes_and_atr_period_are_not_constructor_parameters(self):
        """C-6.3 item 8 / C-6.5: both are part of the strategy definition, so a
        parameter here would let replay run a different strategy than live."""
        import inspect

        parameters = set(
            inspect.signature(HistoricalMarketDataAdapter.__init__).parameters)
        # `eligible` (M13) is the injected Universe Definition membership set.
        # It is admissible here for the same reason `static_universe` is: it
        # selects *which instruments* are ranked, and changes nothing about how
        # any instrument is analysed. A window size or `atr_period` would.
        self.assertEqual(
            parameters,
            {"self", "repository", "static_universe", "eligible"})


if __name__ == "__main__":
    unittest.main()
