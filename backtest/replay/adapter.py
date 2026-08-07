"""HistoricalMarketDataAdapter — the replay side of the market-data boundary.

REPLAY_CONTRACT.md §7 and its invariants I-1 … I-10; C-6.2 … C-6.9 for what a
window is, C-5.3 for the snapshot's time fields.

## The one idea

This is the **only** component replay substitutes for live. Everything above it —
orchestrator, analyzer, scoring, entry, risk, lifecycle, delivery — runs
unchanged, because it receives a `MarketSnapshot` it cannot distinguish from a
live one::

    Orchestrator -> MarketDataPort -> MarketDataAdapter          (live)
    Orchestrator -> MarketDataPort -> HistoricalMarketDataAdapter (replay)

"Indistinguishable" is not an aspiration here. The frames must match live's
column order, dtypes, index, ATR values and NaN warmup exactly, because analysis
reads frames positionally (the OB scan window, the swing loop, the EMA seeds) and
any difference in shape silently changes every downstream result.

## What it does

Given a scan boundary `T` (C-5.1, the instant an H1 candle closes), it serves the
rows the live bot could have seen one second later, and nothing else:

- windows of **119 D1 / 499 H1 / 199 M5**, which is live's 120/500/200 request
  minus the forming candle it strips (C-6.2);
- selection on `open_time + interval <= T`, so a candle that had not fully
  elapsed at `T` cannot appear (C-6.3 item 7);
- **row-based** windows: a gap in history yields a shorter frame spanning more
  wall-clock time, never a padded or interpolated one — exactly what live's
  `limit=N` request returns, because Binance omits klines that do not exist.

## What it deliberately does not do

No sufficiency guards (`D1 < 10`, `H1 < 50`) — those belong to the orchestrator
and applying them here would silently change which symbols are skipped (C-6.8).
No indicator except ATR (C-6.5). No signal, lifecycle or outcome logic. No
network, no wall clock, no writes: it reads a repository and returns frames.

## Time

`set_time` is **driver-only**. The orchestrator never learns that time can be
positioned — a `MarketDataPort` with a `set_time` in its Protocol would leak
replay into production (C-7, "the orchestrator never sees it"). Calling a data
method before positioning raises `ValueError`, not `MarketDataError`, on purpose:
`MarketDataError` is caught and swallowed per-symbol by the orchestrator, so a
driver bug would vanish; a `ValueError` propagates out of `run_cycle` and stops
the run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest.data.repository import (
    CorruptDataset, DatasetNotFound, KlineRepository, RepositoryError,
)
from v2.domain.market import MarketSnapshot
from v2.infra.config import ATR_PERIOD
from v2.infra.market_data import MarketDataError, SymbolVolume

__all__ = [
    "D1_ROWS",
    "DataLineage",
    "H1_ROWS",
    "HistoricalMarketDataAdapter",
    "M5_MIN_CLOSED_ROWS",
    "M5_ROWS",
    "ReplayDataError",
    "UNIVERSE_TRAILING_ROWS",
]

#: Closed-candle window sizes (C-6.2). Live requests 120/500/200 *including* the
#: forming candle and strips the last row; these are what remains. Not
#: configurable: the window shape is part of the strategy definition (C-6.3
#: item 8), so a parameter here would be a silent strategy change.
D1_ROWS = 119
H1_ROWS = 499
M5_ROWS = 199

#: Live requires >= 20 raw M5 rows before stripping the forming candle
#: (market_data.py), i.e. >= 19 closed rows (C-6.7).
M5_MIN_CLOSED_ROWS = 19

#: C-6.9 ranks by quote volume summed over the trailing 24 closed H1 candles.
UNIVERSE_TRAILING_ROWS = 24

_SECOND_MS = 1000
_H1_SECONDS = 3600

#: The seven columns analysis sees, in this order (C-6.4 / I-1).
FRAME_COLUMNS = ("timestamp", "open", "high", "low", "close", "v", "atr")

#: The six raw columns live keeps; `ct, qv, nt, tb, tq, i` are a storage concern
#: and MUST NOT reach analysis (C-6.4).
_OHLCV_COLUMNS = ("open", "high", "low", "close", "v")


class ReplayDataError(MarketDataError):
    """The repository could not be read or contradicts itself.

    A subclass of `MarketDataError` so the orchestrator's existing handling
    applies unchanged (§7). Raised **only** for corruption — never for ordinary
    missing data, which is `None` (C-6.7). The distinction matters: the
    orchestrator swallows per-symbol failures silently (finding A51), so a
    corrupt repository that raised something ordinary would look like a quiet
    stretch of skipped symbols rather than a broken run.
    """


@dataclass(frozen=True)
class DataLineage:
    """Where the data a cycle saw came from. Provenance only — no strategy
    information, no signal, no decision (Task 6).

    `replay_timestamp` is the scan boundary `T` in epoch seconds, so a snapshot
    is traceable to the exact instant it represents as well as to the bytes it
    was cut from.
    """

    data_snapshot_id: str
    builder_version: str
    repository_root: str
    replay_timestamp: Optional[int]

    def to_dict(self) -> dict:
        return {
            "data_snapshot_id": self.data_snapshot_id,
            "builder_version": self.builder_version,
            "repository_root": self.repository_root,
            "replay_timestamp": self.replay_timestamp,
        }


@dataclass
class CycleCounters:
    """Per-cycle observability, reset by `set_time` (C-8.3).

    `m5_guard_failures` should stay at zero: at `T + 1s` the hour-alignment guard
    always passes (C-5.5.2), so a non-zero count means the repository's M5 and H1
    coverage disagree — worth surfacing rather than absorbing.

    `data_errors` exists because the orchestrator catches **every** exception per
    symbol and returns silently ([orchestrator.py:293](v2/app/orchestrator.py#L293)).
    A `ReplayDataError` raised for a corrupt dataset therefore never reaches the
    driver by propagation: it would look exactly like a symbol the strategy
    skipped. Counting it here is what lets a driver stop a run on corruption
    (C-15.8, finding A51).
    """

    universe_size: int = 0
    snapshots_served: int = 0
    snapshots_none: int = 0
    m5_attached: int = 0
    m5_guard_failures: int = 0
    m5_too_short: int = 0
    data_errors: int = 0
    #: The exception type name if `get_top_volume_symbols` raised, else None.
    #: `run_cycle` swallows `MarketDataError` and returns False, so a corrupt
    #: repository would otherwise look like an empty market (C-8.3).
    universe_error: Optional[str] = None
    #: `(symbol, closed_d1_rows, closed_h1_rows)` for every snapshot served this
    #: cycle, in service order.
    #:
    #: C-8.3 asks the adapter for a `guard_skips` **count**, which C-15.8 then
    #: needs to tell "skipped for want of history" apart from "the analysis
    #: raised and was swallowed". Producing that count here would require the
    #: orchestrator's two thresholds, and C-7.2 forbids this module from
    #: depending on `v2.app` at all. So the adapter reports the *shape* — which
    #: C-6.8 says it legitimately owns — and the driver, which already holds the
    #: thresholds, applies the guard. See the M9 findings.
    served_shapes: tuple[tuple[str, int, int], ...] = ()

    def reset(self) -> None:
        self.universe_size = 0
        self.snapshots_served = 0
        self.snapshots_none = 0
        self.m5_attached = 0
        self.m5_guard_failures = 0
        self.m5_too_short = 0
        self.data_errors = 0
        self.universe_error = None
        self.served_shapes = ()

    def to_dict(self) -> dict:
        return {
            "universe_size": self.universe_size,
            "snapshots_served": self.snapshots_served,
            "snapshots_none": self.snapshots_none,
            "m5_attached": self.m5_attached,
            "m5_guard_failures": self.m5_guard_failures,
            "m5_too_short": self.m5_too_short,
            "data_errors": self.data_errors,
            "universe_error": self.universe_error,
        }


class HistoricalMarketDataAdapter:
    """A `MarketDataPort` served from a `KlineRepository`.

    Structurally identical to the live adapter from the orchestrator's side: the
    same two methods, the same return types, the same `None`-means-skip
    convention. It satisfies `MarketDataPort` by duck typing, exactly as
    `MarketDataAdapter` does — neither inherits from the Protocol, and a
    `runtime_checkable` assertion in the tests keeps it that way.

    Stateless apart from the current boundary and its counters: the same
    `(repository, T, symbol, include_m5)` always produces bit-identical frames
    (I-8). Nothing is cached, so no frame built for one `T` can ever be served
    at another.
    """

    def __init__(
        self,
        repository: KlineRepository,
        *,
        static_universe: Optional[tuple[str, ...]] = None,
        eligible: Optional[frozenset[str]] = None,
    ) -> None:
        """`static_universe` pins the universe to a fixed set of Binance-native
        names for a controlled experiment (C-6.9 `universe_mode: "static"`); it
        is a constructor parameter rather than a conditional inside a method
        (C-7.2), and a run that uses it must declare its own bias.

        `eligible` is the set of Binance-native names satisfying the canonical
        Universe Definition, **resolved elsewhere and injected**. This adapter
        performs no classification: `backtest.universe` owns that decision, and
        computing it here would create a second eligibility rule free to drift
        from the one live applies.

        `None` means unfiltered, which is the pre-M13 behaviour and is retained
        only for tests that exercise unrelated adapter mechanics. Every
        production path resolves and injects it, and the run manifest records
        the resulting `universe_id`.

        There is deliberately no `atr_period` parameter, and no window-size
        parameter: `ATR_PERIOD` and 119/499/199 are part of the strategy
        definition (C-6.5, C-6.3 item 8), so exposing them here would let a
        replay silently run a different strategy than live.
        """
        self._repository = repository
        self._static_universe = static_universe
        self._eligible = eligible
        self._boundary: Optional[int] = None
        self.counters = CycleCounters()
        #: The universe this adapter served for the current cycle, in the order
        #: it served it. C-15.2.1 resolves `universe_rank` from here at capture
        #: flush time — never from the order of `record_decision` calls, which
        #: omits every symbol skipped before `reconcile` ran.
        self.universe: tuple[str, ...] = ()

    # -- positioning (driver-only) -----------------------------------------

    def set_time(self, boundary: int) -> None:
        """Position the adapter at scan boundary `T` (epoch **seconds**).

        `T` must satisfy `T mod 3600 == 0` (C-5.1) and be strictly greater than
        the previous boundary (C-5.6.6). Both are driver errors, so both raise
        `ValueError` rather than a data error — see the module docstring.
        """
        value = int(boundary)
        if value % _H1_SECONDS != 0:
            raise ValueError(
                f"scan boundary {value} is not on an hour: T mod 3600 must be 0 "
                f"(C-5.1)"
            )
        if self._boundary is not None and value <= self._boundary:
            raise ValueError(
                f"scan boundary must strictly increase (C-5.6.6): {value} does "
                f"not follow {self._boundary}"
            )
        self._boundary = value
        self.counters.reset()
        self.universe = ()

    @property
    def boundary(self) -> Optional[int]:
        """The current scan boundary `T`, or None before positioning."""
        return self._boundary

    @property
    def lineage(self) -> DataLineage:
        """Provenance for the data this adapter serves (Task 6)."""
        return DataLineage(
            data_snapshot_id=self._repository.data_snapshot_id,
            builder_version=self._repository.builder_version,
            repository_root=str(self._repository.root),
            replay_timestamp=self._boundary,
        )

    @property
    def data_snapshot_id(self) -> str:
        """The repository identity stamped on every ledger row built from this
        adapter's data (C-6.10, C-16.1)."""
        return self._repository.data_snapshot_id

    # -- the port ----------------------------------------------------------

    def get_top_volume_symbols(self, limit: int) -> tuple[SymbolVolume, ...]:
        """The universe as of `T`, reconstructed per C-6.9.

        Every symbol in the inventory is ranked by quote volume summed over its
        trailing **24 closed H1 candles** — not the symbols that happen to rank
        today, which would bake survivorship and look-ahead into every result.

        **Eligibility.** A symbol participates only with a full 24 rows at `T`
        **whose newest row opens inside the trailing day**. A partial sum would
        systematically under-rank new listings and make their entry into the
        universe depend on when the run window starts; they enter naturally once
        a full day exists.

        The recency half of that test is finding A1. `KlineRepository.window` is
        C-6.3 verbatim and therefore **row-based, never time-based**: it returns
        the last 24 *stored* rows at or before `T`, however old they are. That is
        right for frames, where a gap must not be padded, but for ranking it
        silently turns "no data at `T`" into "an arbitrary sum from the past". A
        delisted instrument kept returning 24 rows frozen at its final candle and
        so held a top-30 slot forever: measured on the step 7 shakedown,
        LUNAUSDT ranked 1-5 at all 744 cycles on volume 5,585-7,868 hours stale,
        with ANCUSDT and KEEPUSDT behind it. Live cannot do this — it ranks from
        `/fapi/v1/ticker/24hr`, which lists only trading instruments — so the
        divergence was undeclared and unbounded.

        The test constrains where the window **ends**, and only that: 24 rows
        spanning a gap still cover more than a day, so the sum is "the trailing 24
        candles", not "exactly 24 hours". That residue is pre-existing and
        measured — across the whole declared replay window (2023-01-01 ->
        2026-07-05) the corpus holds **three** gaps longer than an hour
        (TLMUSDT 12 h, MINAUSDT 11 h, AERGOUSDT 11 h) and **none** of 24 h or
        more, so no live symbol is ever suspended by this test and no window
        stretches by more than half a day.

        **Tie-break.** Live sorts the exchange's ticker list with Python's stable
        sort, so equal volumes keep the API's response order — which is not
        reproducible. Candidates are therefore ordered by **name ascending**
        first and then sorted by descending volume, stably: equal volumes come
        out in name order and the universe is a pure function of the data.
        """
        boundary_ms = self._require_boundary() * _SECOND_MS
        # Closed-candle arithmetic only: the earliest open time that still lies
        # in the trailing day at `T`. No row after `T` is read, and no property
        # of the future is consulted, so the test is causal by construction.
        trailing_start_ms = (
            boundary_ms - UNIVERSE_TRAILING_ROWS * _H1_SECONDS * _SECOND_MS)
        candidates: list[tuple[str, float]] = []
        try:
            names = self._universe_candidates()
            for native in names:
                rows = self._window(
                    native, "1h", boundary_ms, UNIVERSE_TRAILING_ROWS)
                if rows is None or len(rows) < UNIVERSE_TRAILING_ROWS:
                    continue
                if int(rows["timestamp"][-1]) < trailing_start_ms:
                    # Stale: the newest closed candle predates the trailing day,
                    # so there is no trailing-24h volume to rank on (A1).
                    continue
                candidates.append((native, float(rows["qv"].sum())))
        except MarketDataError as exc:
            # `run_cycle` swallows this and returns False, so the failure would
            # otherwise be indistinguishable from a genuinely empty market
            # (C-8.3). Named here so the driver can fail the run.
            self.counters.universe_error = type(exc).__name__
            raise

        candidates.sort(key=lambda item: item[0])
        candidates.sort(key=lambda item: item[1], reverse=True)
        universe = tuple(
            SymbolVolume(symbol=_to_live_symbol(native), volume=volume)
            for native, volume in candidates[: max(int(limit), 0)]
        )
        self.counters.universe_size = len(universe)
        self.universe = tuple(entry.symbol for entry in universe)
        return universe

    def get_market_snapshot(
        self, symbol: str, *, include_m5: bool = False
    ) -> Optional[MarketSnapshot]:
        """The C-6.6 snapshot as of `T`, or `None` to skip the symbol.

        `None` is ordinary, not an error: the symbol is not in the repository,
        or `T` precedes its listing, or no H1/D1 candle had closed by `T`. Live
        returns `None` in exactly those situations and the orchestrator skips the
        symbol either way (C-6.7).
        """
        boundary = self._require_boundary()
        boundary_ms = boundary * _SECOND_MS
        native = _to_native_symbol(symbol)

        d1_rows = self._window(native, "1d", boundary_ms, D1_ROWS)
        h1_rows = self._window(native, "1h", boundary_ms, H1_ROWS)
        if d1_rows is None or h1_rows is None or len(d1_rows) == 0 or len(h1_rows) == 0:
            self.counters.snapshots_none += 1
            return None

        d1 = self._frame(d1_rows)
        h1 = self._frame(h1_rows)

        # C-5.3: the open time of the reference candle. Read from the frame, as
        # live does, rather than computed as `T - 3600`: the two agree whenever
        # the candle exists, and when history has a hole at the boundary the
        # frame is the truth. Fabricating a timestamp for a candle no one holds
        # would break I-4 and put a phantom instant into the ledger.
        h1_last_ts_dt = pd.to_datetime(h1["timestamp"].iloc[-1])
        h1_last_ts = float(h1_last_ts_dt.timestamp())
        live_price = float(h1["close"].iloc[-1])

        m5 = self._closed_m5(native, boundary_ms, h1_last_ts_dt) if include_m5 else None

        self.counters.snapshots_served += 1
        # The shape of what was served (C-6.8). The orchestrator owns the guard
        # that reads it, and the driver evaluates C-15.8's identity from it.
        self.counters.served_shapes += ((symbol, len(d1_rows), len(h1_rows)),)
        return MarketSnapshot(
            symbol=symbol,
            d1=d1,
            h1=h1,
            h1_last_ts=h1_last_ts,
            h1_last_ts_dt=h1_last_ts_dt,
            live_price=live_price,
            m5=m5,
        )

    # -- internals ---------------------------------------------------------

    def _universe_candidates(self) -> tuple[str, ...]:
        """Binance-native names eligible for ranking.

        Two filters, and the distinction between them is the whole of C-6.9:

        - **The name test** — ends with `USDT`, contains no `_` — removes dated
          delivery contracts, which share the archive with perpetuals.
        - **The injected eligibility set** applies the canonical Universe
          Definition. Until M13 this was missing here, and the ranking admitted
          every dataset in the corpus: on the Stage 1 inventory that meant 121
          tokenised equities, commodities and index products, occupying up to 13
          of the 30 universe slots. Live has excluded them since the Phase 0
          freeze; the adapter had never been brought along.

        What is deliberately *not* filtered is current listing status, and that
        remains right: a symbol delisted today was tradeable at its own
        boundaries, the corpus already says so by holding rows for it, and
        excluding delisted instruments *here* would reintroduce exactly the
        survivorship bias C-6.9 exists to prevent. Listing status is a fact about
        **now**; candidate selection may not consult it.

        This docstring used to add that "a symbol with no rows at `T` fails the
        24-row gate above and cannot rank". **That was false, and finding A1 is
        what it cost.** `KlineRepository.window` is row-based (C-6.3), so a dead
        symbol keeps returning its final 24 candles at every later `T`, passes the
        row gate, and ranks on frozen volume — LUNAUSDT held rank 1-5 at all 744
        cycles of the step 7 shakedown. Staleness is a fact about the **data at
        `T`**, not about listing status, so it is tested where the volume is
        actually computed: `get_top_volume_symbols` requires the newest of those
        24 rows to open at or after `T - 24h`. The two filters are complementary,
        and neither substitutes for the other.
        """
        if self._static_universe is not None:
            return tuple(self._static_universe)
        try:
            names = self._repository.symbols("1h")
        except RepositoryError as exc:
            self.counters.data_errors += 1
            raise ReplayDataError(f"cannot enumerate the repository: {exc}") from exc
        return tuple(
            name for name in names
            if name.endswith("USDT") and "_" not in name
            and (self._eligible is None or name in self._eligible)
        )

    def _window(
        self, native: str, timeframe: str, boundary_ms: int, count: int
    ) -> Optional[np.ndarray]:
        """The last `count` rows fully elapsed at `boundary_ms`, or `None` when
        this repository holds no such dataset.

        `DatasetNotFound` is absence, not corruption, and must not escape: the
        orchestrator's per-symbol isolation would swallow it invisibly and the
        symbol would look skipped for an unknown reason (M3's stub-adapter
        finding, A51). Corruption is the opposite and is raised.
        """
        try:
            return self._repository.window(
                native, timeframe, end_ms=boundary_ms, count=count
            )
        except DatasetNotFound:
            return None
        except (CorruptDataset, RepositoryError) as exc:
            # Counted before raising: the orchestrator swallows this per symbol,
            # so the counter is the only channel by which a driver can learn the
            # repository is corrupt (see `CycleCounters.data_errors`).
            self.counters.data_errors += 1
            raise ReplayDataError(
                f"{native} {timeframe} at {boundary_ms}ms: {exc}"
            ) from exc

    def _frame(self, rows: np.ndarray) -> pd.DataFrame:
        """Build the seven-column frame analysis consumes (C-6.4 / I-1).

        Mirrors the live construction step for step
        (`BinanceMarketDataSource.fetch_data_with_atr`): the six kept columns are
        cast to float, the timestamp is converted from epoch ms **through that
        float**, and ATR is appended last. Following live's float path rather
        than converting the stored `int64` directly is deliberate — every epoch
        millisecond in range is exact in float64, and matching the live sequence
        removes the question of whether the two could ever round differently.

        ATR is computed **here, on exactly these rows** (C-6.5). Live computes it
        on the raw frame and then strips the forming row; because ATR's RMA is
        causal, the two are bit-identical, and that equivalence is pinned by a
        test rather than assumed.
        """
        frame = pd.DataFrame({
            "timestamp": rows["timestamp"].astype(float),
            **{name: rows[name].astype(float) for name in _OHLCV_COLUMNS},
        })
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms")
        frame["atr"] = ta.atr(
            frame["high"], frame["low"], frame["close"], length=ATR_PERIOD
        )
        return frame

    def _closed_m5(
        self, native: str, boundary_ms: int, h1_last_ts_dt: pd.Timestamp
    ) -> Optional[pd.DataFrame]:
        """Closed M5 candles, subject to live's two attachment guards.

        Live requires >= 20 raw rows before the strip (>= 19 closed) and then
        `m5_last_open.floor("h") <= h1_last_open.floor("h")`. Both are reproduced
        rather than reasoned away: at `T + 1s` the hour guard always passes
        (C-5.5.2), so a failure means the repository's M5 and H1 coverage
        disagree, and that is counted rather than hidden.
        """
        rows = self._window(native, "5m", boundary_ms, M5_ROWS)
        if rows is None or len(rows) < M5_MIN_CLOSED_ROWS:
            self.counters.m5_too_short += 1
            return None
        m5 = self._frame(rows)
        m5_last = pd.to_datetime(m5["timestamp"].iloc[-1])
        if m5_last.floor("h") <= h1_last_ts_dt.floor("h"):
            self.counters.m5_attached += 1
            return m5
        self.counters.m5_guard_failures += 1
        return None

    def _require_boundary(self) -> int:
        if self._boundary is None:
            raise ValueError(
                "the adapter has no scan boundary: the driver must call "
                "set_time(T) before each cycle (C-8.3)"
            )
        return self._boundary

    def __repr__(self) -> str:   # pragma: no cover - diagnostics only
        return (
            f"HistoricalMarketDataAdapter(snapshot={self.data_snapshot_id!r}, "
            f"T={self._boundary})"
        )


# ---------------------------------------------------------------------------
# Symbol naming — the two spellings, translated in one place
# ---------------------------------------------------------------------------

def _to_live_symbol(native: str) -> str:
    """`BTCUSDT` -> `BTC/USDT`, the form live emits and the orchestrator passes
    around (market_data.py). A slash cannot appear in a path, which is why the
    repository stores the native spelling and the translation lives here."""
    return f"{native[:-4]}/USDT"


def _to_native_symbol(symbol: str) -> str:
    """`BTC/USDT` -> `BTCUSDT`, matching live's `symbol.replace("/", "")`."""
    return symbol.replace("/", "")
