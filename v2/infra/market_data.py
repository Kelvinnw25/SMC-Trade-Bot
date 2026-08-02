"""infra.market_data — native exchange access + the market-data adapter.

Architecture ref: 02-software-architecture.md §3 (`infra.market_data`:
"fetch OHLCV+ATR and top-volume universe … strip forming candle to produce
closed-candle data … Must NOT know anything about SMC"), §5 (per-symbol fetch),
§8 (MarketSnapshot is the infra→analysis contract).

This adapter is the ONLY V2 entry point to market data. It fetches via
`BinanceMarketDataSource` (native, below) and translates the results into the
immutable `MarketSnapshot` the analysis layer consumes. No SMC detection,
scoring, filtering, or trading decision happens here.

Standalone note (Step 3 of the V2-standalone work): the two exchange methods V2
uses — `get_top_volume_coins` and `fetch_data_with_atr` — previously came from
V1's `BinanceHandler` (`src/api_handler.py`). They are moved here **verbatim** as
`BinanceMarketDataSource`: identical endpoints, request headers, response
parsing, column names/order, dtypes, NaN handling, and the SAME ATR computation
(`pandas_ta.atr(length=ATR_PERIOD)` — copied, not reinvented), so every
DataFrame is byte-for-byte identical to V1's. V1's unused order-execution
scaffolding (the `ccxt` client, `LockedBinance`, `_load_exchange_info_manually`,
`get_active_positions`) is NOT reproduced — V2's adapter never called it; those
served V1's private trading path, not the radar.

Behavioral fidelity: the fetch limits, the forming-candle strip (`iloc[:-1]`),
the timestamp normalization, the live-price field, and the M5 attachment guards
mirror V1's `run()` (main.py) exactly — see the inline references.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

import pandas as pd
import pandas_ta as ta
import requests
import urllib3

from v2.domain.market import MarketSnapshot
from v2.domain.universe import (
    InstrumentMetadata, is_eligible, is_plausible_name,
)
from v2.infra.config import ATR_PERIOD

# V1 `src/api_handler.py:5,8`: silence the InsecureRequestWarning emitted by the
# `verify=False` fetches below. Reproduced so console output matches V1 exactly.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__all__ = [
    "MarketDataAdapter",
    "MarketDataError",
    "MarketDataPort",
    "SymbolVolume",
    "BinanceMarketDataSource",
]

# Fetch parameters — mirror V1 run() exactly (main.py:281-283, 354).
_D1_TIMEFRAME = "1d"
_D1_LIMIT = 120
_H1_TIMEFRAME = "1h"
_H1_LIMIT = 500
_M5_TIMEFRAME = "5m"
_M5_LIMIT = 200
_M5_MIN_RAW_ROWS = 20  # V1 guard: len(df_m5_raw) >= 20 (main.py:356)

# Columns the adapter itself reads (to compute snapshot fields). Analysis
# validates its own richer requirements later (V1's `_safe_df`), so the adapter
# validates only what it uses.
_REQUIRED_COLUMNS = ("timestamp", "close")


class MarketDataError(Exception):
    """A market-data request could not be fulfilled or normalized.

    Raised in place of leaking a lower-level exception (a network error from
    V1, or a malformed DataFrame) so consumers depend on a stable adapter-level
    failure type rather than `src/` internals.
    """


@dataclass(frozen=True)
class SymbolVolume:
    """One top-volume universe entry — a faithful, immutable translation of
    V1's `{'symbol': ..., 'volume': ...}` dict (`get_top_volume_coins`)."""

    symbol: str
    volume: float


def _to_epoch(value: Any) -> float:
    """Normalize a timestamp value to a POSIX epoch float.

    Mirrors V1's `_to_epoch_ts` (main.py) exactly, including its tz-naive
    semantics (a naive candle timestamp is treated as UTC by
    `pandas.Timestamp.timestamp()`) and its 0.0-on-failure fallback. Kept
    behaviorally identical so `last_miss_h1_ts` / decay gating compare the same
    way they did in V1.
    """
    try:
        if value is None:
            return 0.0
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return 0.0
        return float(ts.timestamp())
    except Exception:
        return 0.0


class BinanceMarketDataSource:
    """Native Binance USDT-M futures data source — the two public REST methods
    V2 uses, moved verbatim from V1's `BinanceHandler` (`src/api_handler.py`).

    Only `get_top_volume_coins` and `fetch_data_with_atr` are reproduced (the
    methods `MarketDataAdapter` calls); the response parsing, column names/order,
    dtypes, NaN handling, and the ATR computation are byte-for-byte V1. The
    constructor performs no network I/O (V1's `__init__` built a `ccxt` client and
    loaded exchange info only for the private order path, which V2 never uses)."""

    #: Eligible native symbols from `exchangeInfo`, cached for the process.
    #: The ticker endpoint carries no `contractType`/`underlyingType`, so the
    #: universe rule needs a second call — but instrument metadata changes on a
    #: scale of days while the scan loop runs every ~74 s, so fetching it once
    #: is both sufficient and the only affordable option.
    _eligible: Optional[frozenset] = None

    def eligible_symbols(self) -> Optional[frozenset]:
        """Native symbols satisfying the canonical Universe Definition.

        `None` when `exchangeInfo` could not be read and nothing was cached —
        which the caller must treat as "cannot determine the universe", never as
        "no restriction". Returning the unfiltered list on failure would let a
        transient network error silently widen the traded universe to include
        tokenized equities, and nothing downstream would report it.
        """
        if BinanceMarketDataSource._eligible is not None:
            return BinanceMarketDataSource._eligible
        try:
            response = requests.get(
                "https://fapi.binance.com/fapi/v1/exchangeInfo",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10, verify=False)
            payload = response.json()
            entries = payload.get("symbols") or []
            eligible = frozenset(
                str(entry.get("symbol"))
                for entry in entries
                if is_eligible(InstrumentMetadata.from_exchange_info(entry))
            )
            if not eligible:
                return None
            BinanceMarketDataSource._eligible = eligible
            return eligible
        except Exception as exc:
            # ASCII only: this runs on an error path, and a console whose code page
            # cannot encode the message would raise *inside* the handler.
            print(f"   [WARN] Failed to fetch exchangeInfo: {exc}")
            return None

    def get_top_volume_coins(self, limit: int = 30) -> Any:
        """Top-`limit` eligible USDT-M perpetuals by 24h quote volume, descending,
        as ``[{'symbol': 'BASE/USDT', 'volume': float}, …]``; `[]` on failure.

        V1 (`src/api_handler.py:77-101`) filtered the ticker list by name alone.
        That admits tokenized equities, index and commodity products — measured
        at 150 of 851 instruments on a real snapshot — so Phase 0 replaced the
        name test with the exchange-declared rule in `v2/domain/universe.py`.
        This is a deliberate production behaviour change, recorded in
        `PHASE0_FREEZE.md` and reflected in `STRATEGY_VERSION`.

        The name test survives as a pre-filter only, to drop dated delivery
        contracts before the metadata lookup.
        """
        try:
            eligible = self.eligible_symbols()
            if eligible is None:
                print("   [WARN UNIVERSE] exchangeInfo unavailable; skipping this "
                      "cycle rather than trading an undetermined universe")
                return []

            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            tickers = response.json()

            if isinstance(tickers, dict) and "code" in tickers:
                print(
                    f"   [⚠️ API LIMIT] Binance: {tickers.get('msg', 'Too many requests')}"
                )
                return []

            usdt_pairs = [
                t
                for t in tickers
                if is_plausible_name(t["symbol"]) and t["symbol"] in eligible
            ]
            usdt_pairs.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)

            top_coins = []
            for t in usdt_pairs[:limit]:
                base = t["symbol"][:-4]
                volume = float(t["quoteVolume"])
                top_coins.append({"symbol": f"{base}/USDT", "volume": volume})

            return top_coins

        except Exception as e:
            print(f"⚠️ System: Failed to fetch Top Volume Coins: {e}")
            return []

    def fetch_data_with_atr(
        self, symbol: str, timeframe: str = "15m", limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """V1 `BinanceHandler.fetch_data_with_atr` (src/api_handler.py:103-121),
        verbatim: fetch klines, build the ``[timestamp,open,high,low,close,v]``
        float frame, `to_datetime(unit='ms')`, and append the
        `pandas_ta.atr(length=ATR_PERIOD)` column. `None` on failure."""
        try:
            clean_symbol = symbol.replace("/", "")
            url = (
                f"https://fapi.binance.com/fapi/v1/klines?symbol={clean_symbol}"
                f"&interval={timeframe}&limit={limit}"
            )
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            if response.status_code != 200:
                return None
            bars = response.json()
            df = pd.DataFrame(
                bars,
                columns=[
                    "timestamp", "open", "high", "low", "close", "v",
                    "ct", "qv", "nt", "tb", "tq", "i",
                ],
            )

            df = df[["timestamp", "open", "high", "low", "close", "v"]].astype(float)

            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_PERIOD)

            return df
        except Exception as e:
            print(f"⚠️ System: Failed fetching {symbol} ({timeframe}): {e}")
            return None


@runtime_checkable
class MarketDataPort(Protocol):
    """The market-data contract the scan loop depends on (REPLAY_CONTRACT.md §7).

    Declared here, beside its production implementation, exactly as `Clock` and
    `Notifier` are — so a consumer depends on this Protocol and never on
    `MarketDataAdapter`. Two methods, because the orchestrator calls two
    (`orchestrator.py` universe fetch + per-symbol snapshot); nothing else belongs.

    Implementations must honour these invariants (they are the contract, not
    suggestions):

    - `get_top_volume_symbols` returns the universe **in descending volume order**.
      That order is the scan's processing order and therefore decides slot-cap and
      REPLACE outcomes (C-5.6.2) — it must never be re-sorted. An empty universe is
      an empty tuple, not None and not an exception.
    - `get_market_snapshot` returns None when the data needed to build a snapshot
      is absent — the caller skips the symbol. None is *ordinary*, not an error.
    - Both raise `MarketDataError` (or a subclass) and nothing else when a request
      cannot be fulfilled or normalized. Any other exception type would be
      swallowed invisibly by the caller's per-symbol isolation (finding A51).
    - The sufficiency guards (D1 < 10 rows, H1 < 50 rows) belong to the caller, not
      here (C-6.8): a short-but-present frame is returned, not suppressed.

    Deliberately absent: `set_time`. A historical implementation is positioned in
    time by its driver, and the orchestrator must never learn that time can be
    positioned (C-7: "driver-only; the orchestrator never sees it").

    Implementations: `MarketDataAdapter` (live, below) is the only one today.
    """

    def get_top_volume_symbols(self, limit: int) -> tuple["SymbolVolume", ...]:
        """The top-`limit` symbols by volume, descending; `()` when none qualify."""
        ...

    def get_market_snapshot(
        self, symbol: str, *, include_m5: bool = False
    ) -> Optional[MarketSnapshot]:
        """The closed-candle snapshot for `symbol`, or None to skip it."""
        ...


class MarketDataAdapter:
    """Obtains and normalizes market data via a `BinanceMarketDataSource`.

    Construct with an explicit source (the injection point used by tests), or
    use `create()` in production to build the native source — so that no other
    V2 module imports the exchange client directly.
    """

    def __init__(self, handler: "BinanceMarketDataSource") -> None:
        self._handler = handler

    @classmethod
    def create(cls) -> "MarketDataAdapter":
        """Build an adapter over the native `BinanceMarketDataSource`.

        This is production wiring; tests inject a fake source instead."""
        return cls(BinanceMarketDataSource())

    # -- universe -----------------------------------------------------------

    def get_top_volume_symbols(self, limit: int) -> tuple[SymbolVolume, ...]:
        """Return the top-`limit` symbols by 24h volume, in V1's descending
        order. Empty universe (or a V1 failure that yields no coins) → empty
        tuple; a malformed entry → `MarketDataError`."""
        try:
            coins = self._handler.get_top_volume_coins(limit=limit)
        except Exception as exc:  # V1 normally returns [] on failure; defensive.
            raise MarketDataError(
                f"failed obtaining top-volume universe: {exc}"
            ) from exc

        if not coins:
            return ()

        entries: list[SymbolVolume] = []
        for entry in coins:
            try:
                entries.append(
                    SymbolVolume(
                        symbol=str(entry["symbol"]),
                        volume=float(entry["volume"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataError(
                    f"malformed universe entry {entry!r}: {exc}"
                ) from exc
        return tuple(entries)

    # -- snapshot -----------------------------------------------------------

    def get_market_snapshot(
        self, symbol: str, *, include_m5: bool = False
    ) -> Optional[MarketSnapshot]:
        """Build a `MarketSnapshot` for `symbol` from closed D1/H1 candles.

        Returns None when the data needed to construct a snapshot is absent
        (V1 returned None/empty for D1 or H1, or there is no closed H1 candle
        after the forming-candle strip) — the caller skips the symbol, matching
        V1's `continue`. Raises `MarketDataError` on a malformed response.

        `include_m5=True` additionally attaches closed M5 candles when V1's
        attachment guards pass (see `_closed_m5`); the *decision* to request M5
        (V1's score≥8) belongs to the orchestrator/analysis, not here.
        """
        raw_d1 = self._fetch(symbol, _D1_TIMEFRAME, _D1_LIMIT)
        raw_h1 = self._fetch(symbol, _H1_TIMEFRAME, _H1_LIMIT)

        if raw_d1 is None or raw_h1 is None or raw_d1.empty or raw_h1.empty:
            return None

        self._require_columns(raw_h1, symbol, _H1_TIMEFRAME)

        # Live price = close of the forming H1 candle (main.py:307). Read before
        # the strip, from the raw frame's last row.
        live_price = float(raw_h1["close"].iloc[-1])

        # Drop the forming candle to get closed-candle data (main.py:288-289).
        d1 = raw_d1.iloc[:-1].copy()
        h1 = raw_h1.iloc[:-1].copy()
        if h1.empty:
            return None  # no closed H1 candle -> cannot derive the reference ts

        h1_last_value = h1["timestamp"].iloc[-1]
        h1_last_ts_dt = pd.to_datetime(h1_last_value)
        h1_last_ts = _to_epoch(h1_last_value)

        m5 = self._closed_m5(symbol, h1_last_ts_dt) if include_m5 else None

        return MarketSnapshot(
            symbol=symbol,
            d1=d1,
            h1=h1,
            h1_last_ts=h1_last_ts,
            h1_last_ts_dt=h1_last_ts_dt,
            live_price=live_price,
            m5=m5,
        )

    # -- internals ----------------------------------------------------------

    def _fetch(
        self, symbol: str, timeframe: str, limit: int
    ) -> Optional[pd.DataFrame]:
        """Delegate one OHLCV+ATR fetch to V1, translating any raised error."""
        try:
            return self._handler.fetch_data_with_atr(
                symbol=symbol, timeframe=timeframe, limit=limit
            )
        except Exception as exc:  # V1 returns None on failure; defensive.
            raise MarketDataError(
                f"failed fetching {symbol} {timeframe}: {exc}"
            ) from exc

    def _closed_m5(
        self, symbol: str, h1_last_ts_dt: pd.Timestamp
    ) -> Optional[pd.DataFrame]:
        """Fetch and normalize closed M5 candles, applying V1's attachment
        guards (main.py:352-360): raw length >= 20, forming-candle strip, and
        the M5-must-not-be-newer-than-H1 hour alignment. Returns None when any
        guard fails.

        (V1 also re-checks `not df_m5.empty` after the strip; the >= 20 guard
        makes the stripped frame have >= 19 rows, so that check is always true
        and is omitted here — behavior is identical.)
        """
        raw = self._fetch(symbol, _M5_TIMEFRAME, _M5_LIMIT)
        if raw is None or raw.empty or len(raw) < _M5_MIN_RAW_ROWS:
            return None

        self._require_columns(raw, symbol, _M5_TIMEFRAME)

        m5 = raw.iloc[:-1].copy()
        m5_last_ts = pd.to_datetime(m5["timestamp"].iloc[-1])
        if m5_last_ts.floor("h") <= h1_last_ts_dt.floor("h"):
            return m5
        return None

    @staticmethod
    def _require_columns(df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        """Guard against a malformed response missing a column the adapter reads."""
        missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise MarketDataError(
                f"malformed {symbol} {timeframe} response: missing column(s) {missing}"
            )
