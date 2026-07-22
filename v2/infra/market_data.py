"""infra.market_data — the anti-corruption layer over V1's exchange access.

Architecture ref: 02-software-architecture.md §3 (`infra.market_data`:
"fetch OHLCV+ATR and top-volume universe … strip forming candle to produce
closed-candle data … Must NOT know anything about SMC"), §5 (per-symbol fetch),
§8 (MarketSnapshot is the infra→analysis contract). Roadmap Phase 2: wrap V1's
proven I/O behind a clean contract *without changing what it does*.

This adapter is the ONLY V2 entry point to market data. It delegates every
network call to V1's `BinanceHandler` (`src/api_handler.py`) — it does not
re-implement Binance communication — and translates the results into the
immutable `MarketSnapshot` the analysis layer consumes. No SMC detection,
scoring, filtering, or trading decision happens here.

Behavioral fidelity: the fetch limits, the forming-candle strip (`iloc[:-1]`),
the timestamp normalization, the live-price field, and the M5 attachment guards
mirror V1's `run()` (main.py) exactly — see the inline references. What V1 does
in `run()` that is *not* data access — the D1<10 / H1<50 data-sufficiency
guards (Architecture §5: "orchestrator-level"), the score≥8 decision to request
M5 (analysis), the 0.20s rate-limit sleeps (orchestration cadence, and absent
from `BinanceHandler` itself) — is intentionally left to later phases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from src.api_handler import BinanceHandler
from v2.domain.market import MarketSnapshot

__all__ = ["MarketDataAdapter", "MarketDataError", "SymbolVolume"]

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


class MarketDataAdapter:
    """Obtains and normalizes market data by wrapping V1's `BinanceHandler`.

    Construct with an explicit handler (the injection point used by tests), or
    use `create()` in production to build the real V1 handler — so that no other
    V2 module ever imports `src/api_handler.py` directly.
    """

    def __init__(self, handler: BinanceHandler) -> None:
        self._handler = handler

    @classmethod
    def create(cls) -> "MarketDataAdapter":
        """Build an adapter over a real V1 `BinanceHandler`.

        This is the only place in V2 that constructs `BinanceHandler`; note the
        handler's constructor performs live exchange-info loading, so `create()`
        is for production wiring, not tests (tests inject a fake handler).
        """
        return cls(BinanceHandler())

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
