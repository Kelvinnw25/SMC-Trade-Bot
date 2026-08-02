"""The price walk — §4 of the contract, mechanised.

Given one `TradeIntent` and a kline repository, decide when (or whether) the trade
filled, and how it ended. Nothing else: no analysis, no signal generation, no
aggregation.

## The walk (C-4.0)

For a trade instantiated at scan boundary `T`, the walk is the H1 candles
`W0, W1, …` with `open_time(Wk) == T + k*3600`. `W0` is the candle **forming at
the scan instant** — not `C_ref`, which is already closed and was the analysis
input. `W0`'s open is the print at `T`, one second before the scan instant: a
bounded look-behind, never a look-ahead.

```
 ── C_ref ──────────┼── W0 ─────────────┼── W1 ─────────────┼──
 open T-3600        │  open T           │  open T+3600
                    ▲ T = scan boundary; fills/exits may occur from W0 onward
```

## Everything ambiguous resolves against the trade

That is the single principle behind C-4.3's tie-breaks, and it is why the engine
is trustworthy: every unresolvable ordering biases expectancy **downward**.

- both SL and TP touched in one H1 candle → sub-resolve on that candle's 12 M5
  candles (C-4.3 rule 2);
- both touched inside one M5 candle → **SL first** (rule 3);
- the M5 set missing or incomplete → **SL**, and the run counts it (rule 4);
- a limit fill candle that could also have hit SL → **LOSS** unless M5 proves TP
  came first *after* the fill (C-4.4).

## Event ordering inside a candle (C-4.13)

Intra-candle price events precede that candle's close-scan lifecycle events. So a
fill during `Wk` beats a cancellation produced by the scan at `Wk`'s close, and an
SL/TP touch inside the kill cycle's `C_ref` beats the kill.

## No future data

Every read is bounded by the candle currently being evaluated. The walk never
looks beyond the candle it is resolving, and it never reads through
`HistoricalMarketDataAdapter` — the adapter's 119/499/199 limits and its ATR
column are analysis concerns with no meaning here (C-9.5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from backtest.data.repository import (
    DatasetNotFound, INTERVAL_MS, KlineRepository, RepositoryError,
)
from backtest.outcome.config import OutcomeConfig
from backtest.outcome.errors import OutcomeDataError
from backtest.outcome.trade import (
    Cancellation, STATE_ACTIVE, STATE_EXPIRED, STATE_INVALIDATED_EXIT,
    STATE_INVALID_LEVELS, STATE_LOSS, STATE_NOT_FILLED, STATE_PENDING,
    STATE_WIN, TradeIntent,
)

__all__ = ["Resolution", "WalkCounters", "resolve_walk"]

_H1_MS = INTERVAL_MS["1h"]
_M5_MS = INTERVAL_MS["5m"]
_H1_SECONDS = 3600

#: C-4.3: the M5 set covering an H1 candle is complete **only** with 12 candles.
M5_PER_H1 = 12


@dataclass
class WalkCounters:
    """Run-level data-quality counters the metrics report must surface (C-4.3)."""

    m5_gap_resolutions: int = 0
    m5_inconsistent: int = 0
    excursion_degraded: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "m5_gap_resolutions": self.m5_gap_resolutions,
            "m5_inconsistent": self.m5_inconsistent,
            "excursion_degraded": self.excursion_degraded,
        }


@dataclass(frozen=True)
class Resolution:
    """What the walk determined. Turned into an `OutcomeRecord` by the engine."""

    state: str
    filled: bool = False
    fill_kind: Optional[str] = None
    fill_T: Optional[int] = None
    fill_price: Optional[float] = None
    candles_to_fill: Optional[int] = None
    cancel_reason: Optional[str] = None
    exit_reason: Optional[str] = None
    exit_T: Optional[int] = None
    exit_price: Optional[float] = None
    holding_candles: Optional[int] = None
    realized_r: Optional[float] = None
    realized_r_net: Optional[float] = None
    mae_r: Optional[float] = None
    mfe_r: Optional[float] = None
    excursion_degraded: bool = False
    structural_rr: Optional[float] = None


@dataclass(frozen=True)
class _Exit:
    state: str
    reason: str
    price: float
    open_time: int                       # H1 open time, epoch seconds
    #: Price ranges traversed within the exit candle, up to and including the exit.
    segments: tuple[tuple[float, float], ...] = ()
    degraded: bool = False


# ---------------------------------------------------------------------------

def resolve_walk(
    intent: TradeIntent,
    *,
    repository: KlineRepository,
    config: OutcomeConfig,
    counters: WalkCounters,
) -> Resolution:
    """Resolve one trade against stored prices."""
    return _Walk(intent, repository, config, counters).run()


class _Walk:
    def __init__(self, intent: TradeIntent, repository: KlineRepository,
                 config: OutcomeConfig, counters: WalkCounters) -> None:
        self._intent = intent
        self._repo = repository
        self._config = config
        self._counters = counters
        self._native = intent.native_symbol
        self._long = intent.is_long
        self._sl = float(intent.sl)
        self._tp = float(intent.tp)
        self._entry = float(intent.entry_price)

    # -- entry point -------------------------------------------------------

    def run(self) -> Resolution:
        if not self._levels_sane():
            return Resolution(state=STATE_INVALID_LEVELS)
        structural_rr = abs(self._tp - self._entry) / abs(self._entry - self._sl)

        data_end_open = self._data_end_open()
        cancel = self._effective_cancellation()

        fill = self._find_fill(cancel, data_end_open)
        if fill is None:
            if cancel is not None:
                return Resolution(
                    state=STATE_NOT_FILLED, cancel_reason=cancel.reason,
                    exit_T=cancel.boundary, structural_rr=structural_rr)
            # Data ran out with the order still resting: the `OPEN` label (C-4.10).
            return Resolution(
                state=STATE_PENDING, exit_reason="END_OF_DATA",
                exit_T=data_end_open, structural_rr=structural_rr)

        fill_open, fill_price, fill_kind = fill
        r_unit = abs(fill_price - self._sl)
        if not math.isfinite(r_unit) or r_unit == 0.0:
            # C-9.6: a fill sitting exactly on the stop has no risk unit.
            return Resolution(state=STATE_INVALID_LEVELS, filled=True,
                              fill_kind=fill_kind, fill_T=fill_open,
                              fill_price=fill_price,
                              candles_to_fill=self._candle_index(fill_open),
                              structural_rr=structural_rr)

        exit_event, segments, degraded = self._find_exit(
            fill_open, fill_price, fill_kind, data_end_open)

        holding = (exit_event.open_time - fill_open) // _H1_SECONDS + 1
        realized_r = self._realized_r(fill_price, exit_event.price, r_unit)
        mae_r, mfe_r = self._excursions(fill_price, segments, r_unit)
        if degraded:
            self._counters.excursion_degraded += 1

        return Resolution(
            state=exit_event.state,
            filled=True,
            fill_kind=fill_kind,
            fill_T=fill_open,
            fill_price=fill_price,
            candles_to_fill=self._candle_index(fill_open),
            exit_reason=exit_event.reason,
            exit_T=exit_event.open_time,
            exit_price=exit_event.price,
            holding_candles=int(holding),
            realized_r=realized_r,
            realized_r_net=self._net_r(realized_r, fill_price, r_unit),
            mae_r=mae_r,
            mfe_r=mfe_r,
            excursion_degraded=degraded,
            structural_rr=structural_rr,
        )

    # -- C-4.14 level sanity ----------------------------------------------

    def _levels_sane(self) -> bool:
        values = (self._entry, self._sl, self._tp)
        if not all(math.isfinite(value) for value in values):
            return False
        if not all(value > 0 for value in values):
            return False
        if self._long:
            return self._tp > self._entry > self._sl
        return self._tp < self._entry < self._sl

    # -- cancellation ------------------------------------------------------

    def _effective_cancellation(self) -> Optional[Cancellation]:
        """C-2.2: the two suppressed shadow classes have no memory record, so no
        lifecycle event can evict them — their only end is the fixed window."""
        if self._intent.has_fixed_shadow_window:
            return Cancellation(
                boundary=self._intent.boundary
                + self._config.shadow_pending_h1 * _H1_SECONDS,
                reason="SHADOW_WINDOW",
            )
        return self._intent.cancellation

    # -- fill (C-4.1 / C-4.2) ---------------------------------------------

    def _find_fill(
        self, cancel: Optional[Cancellation], data_end_open: Optional[int]
    ) -> Optional[tuple[int, float, str]]:
        """`(fill H1 open time, fill price, fill_kind)`, or None if never filled.

        C-4.13 sets the deadline: a cancellation produced by the scan at `T_c`
        acts at `T_c`, so every candle that *closed* by then still had its chance
        — candles with `open_time + 3600 <= T_c`.
        """
        last_open = data_end_open
        if cancel is not None:
            deadline = cancel.boundary - _H1_SECONDS
            last_open = deadline if last_open is None else min(last_open, deadline)
        if last_open is None or last_open < self._intent.boundary:
            return None

        candles = self._h1_range(self._intent.boundary, last_open)
        if len(candles) == 0:
            return None

        if self._intent.entry_mode == "CONTINUATION":
            origin = candles[0]
            if int(origin["timestamp"]) // 1000 != self._intent.boundary:
                # W0 itself is missing from history; the market-fill rule has no
                # price to use, so the order never became a position.
                return None
            price = (float(origin["open"]) if self._config.continuation_fill
                     == "w0_open" else float(origin["close"]))
            return self._intent.boundary, price, "MARKET_WALK_ORIGIN"

        if self._pre_crossed():
            origin = candles[0]
            if int(origin["timestamp"]) // 1000 != self._intent.boundary:
                return None
            return self._intent.boundary, float(origin["open"]), "IMMEDIATE"

        for candle in candles:
            open_time = int(candle["timestamp"]) // 1000
            price, kind = self._limit_fill(candle)
            if price is not None:
                return open_time, price, kind        # type: ignore[return-value]
        return None

    def _pre_crossed(self) -> bool:
        """C-4.1: a limit already at or through market fills at once.

        The reference is `close(C_ref)` — the last price the scan could see. An
        order resting on the reachable side of it was already crossed when it was
        placed, and modelling patience would fabricate a favourable entry.
        """
        reference = self._reference_close()
        if self._long:
            return self._entry >= reference
        return self._entry <= reference

    def _limit_fill(self, candle) -> tuple[Optional[float], Optional[str]]:
        """C-4.1: touch fills at the level, or better when the candle gaps
        through it."""
        open_, low, high = (float(candle["open"]), float(candle["low"]),
                           float(candle["high"]))
        if self._long:
            if open_ < self._entry:
                return open_, "LIMIT_GAP"
            if low <= self._entry:
                return self._entry, "LIMIT_TOUCH"
        else:
            if open_ > self._entry:
                return open_, "LIMIT_GAP"
            if high >= self._entry:
                return self._entry, "LIMIT_TOUCH"
        return None, None

    # -- exit (C-4.3 – C-4.5, C-4.9, C-4.10) ------------------------------

    def _find_exit(
        self, fill_open: int, fill_price: float, fill_kind: str,
        data_end_open: Optional[int],
    ) -> tuple[_Exit, list[tuple[float, float]], bool]:
        limit_open = fill_open + (self._config.max_holding_h1 - 1) * _H1_SECONDS
        if data_end_open is not None:
            limit_open = min(limit_open, data_end_open)
        candles = self._h1_range(fill_open, limit_open)

        segments: list[tuple[float, float]] = []
        degraded = False
        kill_candle_open = (None if self._intent.htf_kill_boundary is None
                            else self._intent.htf_kill_boundary - _H1_SECONDS)

        last_candle = None
        for index, candle in enumerate(candles):
            open_time = int(candle["timestamp"]) // 1000
            last_candle = candle
            first = index == 0

            if first and fill_kind in ("LIMIT_TOUCH", "LIMIT_GAP"):
                exit_event = self._exit_on_limit_fill_candle(candle, fill_price)
            else:
                exit_event = self._exit_in_candle(candle)

            if exit_event is not None:
                segments.extend(exit_event.segments)
                degraded = degraded or exit_event.degraded
                return exit_event, segments, degraded

            traversed, candle_degraded = self._candle_segments(
                candle, from_price=fill_price if first and
                fill_kind in ("LIMIT_TOUCH", "LIMIT_GAP") else None)
            segments.extend(traversed)
            degraded = degraded or candle_degraded

            # C-4.13: the scan at this candle's close acts after its price events.
            if kill_candle_open is not None and open_time == kill_candle_open:
                return (_Exit(state=STATE_INVALIDATED_EXIT, reason="HTF_KILL",
                              price=float(candle["close"]), open_time=open_time),
                        segments, degraded)
            if index + 1 >= self._config.max_holding_h1:
                return (_Exit(state=STATE_EXPIRED, reason="MAX_HOLDING",
                              price=float(candle["close"]), open_time=open_time),
                        segments, degraded)

        # Data ended with the position still open: the `OPEN` label (C-4.10),
        # marked to market at the last available close.
        if last_candle is None:                          # pragma: no cover
            raise OutcomeDataError(
                f"{self._native}: no candle at the fill boundary {fill_open}")
        return (_Exit(state=STATE_ACTIVE, reason="END_OF_DATA",
                      price=float(last_candle["close"]),
                      open_time=int(last_candle["timestamp"]) // 1000),
                segments, degraded)

    def _exit_in_candle(self, candle) -> Optional[_Exit]:
        """SL/TP for a candle entered at its open (C-4.3 – C-4.5)."""
        open_time = int(candle["timestamp"]) // 1000
        gap = self._gap_exit(candle, open_time)
        if gap is not None:
            return gap

        sl_hit, tp_hit = self._hits(candle)
        if not sl_hit and not tp_hit:
            return None
        if sl_hit and not tp_hit:
            return _Exit(STATE_LOSS, "SL", self._sl, open_time,
                         self._own_range(candle))
        if tp_hit and not sl_hit:
            return _Exit(STATE_WIN, "TP", self._tp, open_time,
                         self._own_range(candle))
        return self._sub_resolve(candle, open_time)

    def _exit_on_limit_fill_candle(self, candle, fill_price: float
                                   ) -> Optional[_Exit]:
        """C-4.4: on a limit fill candle, exits count only **after** the fill.

        Without M5 the ordering is unknowable, so worst case applies: a candle
        that could have hit the stop is a loss unless M5 proves the target came
        first after the fill.
        """
        open_time = int(candle["timestamp"]) // 1000
        sl_hit, tp_hit = self._hits(candle)
        if not sl_hit and not tp_hit:
            return None

        m5 = self._m5_set(open_time)
        if m5 is None:
            if sl_hit:
                self._counters.m5_gap_resolutions += 1
                return _Exit(STATE_LOSS, "SL_WORST_CASE_NO_M5", self._sl,
                             open_time, ((float(candle["high"]),
                                          float(candle["low"])),), True)
            return _Exit(STATE_WIN, "TP", self._tp, open_time,
                         ((float(candle["high"]), float(candle["low"])),), True)

        start = self._fill_index(m5, fill_price)
        return self._scan_m5(m5, start, open_time, first_is_fill_candle=True)

    def _sub_resolve(self, candle, open_time: int) -> _Exit:
        """C-4.3 rules 2–4: both levels touched in one H1 candle."""
        m5 = self._m5_set(open_time)
        if m5 is None:
            self._counters.m5_gap_resolutions += 1
            return _Exit(STATE_LOSS, "SL_WORST_CASE_NO_M5", self._sl, open_time,
                         ((float(candle["high"]), float(candle["low"])),), True)
        resolved = self._scan_m5(m5, 0, open_time, first_is_fill_candle=False)
        if resolved is not None:
            return resolved
        # The H1 candle says both levels were touched but its own M5 set says
        # neither was. The timeframes disagree; do not guess (C-4.3's spirit).
        self._counters.m5_inconsistent += 1
        self._counters.m5_gap_resolutions += 1
        return _Exit(STATE_LOSS, "SL_WORST_CASE_NO_M5", self._sl, open_time,
                     ((float(candle["high"]), float(candle["low"])),), True)

    def _scan_m5(self, m5, start: int, open_time: int, *,
                 first_is_fill_candle: bool) -> Optional[_Exit]:
        """Walk the M5 candles of one hour, applying the same per-candle rule."""
        segments: list[tuple[float, float]] = []
        for offset in range(start, len(m5)):
            candle = m5[offset]
            segments.append((float(candle["high"]), float(candle["low"])))
            is_fill = first_is_fill_candle and offset == start

            gap = self._gap_exit(candle, open_time)
            if gap is not None and not is_fill:
                return _Exit(gap.state, gap.reason, gap.price, open_time,
                             tuple(segments))

            sl_hit, tp_hit = self._hits(candle)
            if sl_hit and tp_hit:
                return _Exit(STATE_LOSS, "SL_WORST_CASE_M5", self._sl,
                             open_time, tuple(segments))
            if sl_hit:
                # On the fill's own M5 candle the two events cannot be ordered,
                # so the stop wins (C-4.4).
                reason = "SL_WORST_CASE_M5" if is_fill else "SL"
                return _Exit(STATE_LOSS, reason, self._sl, open_time,
                             tuple(segments))
            if tp_hit:
                return _Exit(STATE_WIN, "TP", self._tp, open_time,
                             tuple(segments))
        return None

    def _gap_exit(self, candle, open_time: int) -> Optional[_Exit]:
        """C-4.5: a candle that opens beyond a level exits at the open — worse
        than −1R through the stop, better than +RR through the target."""
        open_ = float(candle["open"])
        if self._long:
            if open_ <= self._sl:
                return _Exit(STATE_LOSS, "GAP_SL", open_, open_time,
                             ((float(candle["high"]), float(candle["low"])),))
            if open_ >= self._tp:
                return _Exit(STATE_WIN, "GAP_TP", open_, open_time,
                             ((float(candle["high"]), float(candle["low"])),))
        else:
            if open_ >= self._sl:
                return _Exit(STATE_LOSS, "GAP_SL", open_, open_time,
                             ((float(candle["high"]), float(candle["low"])),))
            if open_ <= self._tp:
                return _Exit(STATE_WIN, "GAP_TP", open_, open_time,
                             ((float(candle["high"]), float(candle["low"])),))
        return None

    def _hits(self, candle) -> tuple[bool, bool]:
        low, high = float(candle["low"]), float(candle["high"])
        if self._long:
            return low <= self._sl, high >= self._tp
        return high >= self._sl, low <= self._tp

    def _fill_index(self, m5, fill_price: float) -> int:
        """The first M5 candle in which the limit could have filled."""
        for offset in range(len(m5)):
            price, _ = self._limit_fill(m5[offset])
            if price is not None:
                return offset
        return 0

    # -- excursions (C-9.6) -----------------------------------------------

    def _candle_segments(self, candle, *, from_price: Optional[float]
                         ) -> tuple[list[tuple[float, float]], bool]:
        """The (high, low) ranges of a fully traversed candle, from M5 where the
        set is complete, and the H1 range otherwise (flagging degradation)."""
        open_time = int(candle["timestamp"]) // 1000
        m5 = self._m5_set(open_time)
        if m5 is None:
            return [(float(candle["high"]), float(candle["low"]))], True
        start = 0 if from_price is None else self._fill_index(m5, from_price)
        return [(float(row["high"]), float(row["low"]))
                for row in m5[start:]], False

    @staticmethod
    def _own_range(candle) -> tuple[tuple[float, float], ...]:
        """The candle's own (high, low). Used when the exit is unambiguous, so no
        M5 detail is needed to decide it."""
        return ((float(candle["high"]), float(candle["low"])),)

    def _excursions(self, fill_price: float,
                    segments: Sequence[tuple[float, float]], r_unit: float
                    ) -> tuple[Optional[float], Optional[float]]:
        """`mae_r <= 0` and `mfe_r >= 0` between fill and exit inclusive (C-9.6).

        The clamps hold the contract's declared signs even when the fill sits at
        an extreme of its own candle.
        """
        if not segments:
            return 0.0, 0.0
        highest = max(high for high, _ in segments)
        lowest = min(low for _, low in segments)
        if self._long:
            adverse, favourable = lowest - fill_price, highest - fill_price
        else:
            adverse, favourable = fill_price - highest, fill_price - lowest
        return min(0.0, adverse / r_unit), max(0.0, favourable / r_unit)

    # -- R arithmetic (C-9.6) ---------------------------------------------

    def _realized_r(self, fill_price: float, exit_price: float,
                    r_unit: float) -> float:
        if self._long:
            return (exit_price - fill_price) / r_unit
        return (fill_price - exit_price) / r_unit

    def _net_r(self, realized_r: float, fill_price: float,
               r_unit: float) -> float:
        fee = self._config.taker_fee_rate
        if fee == 0.0:
            return realized_r
        return realized_r - 2.0 * fee * fill_price / r_unit

    # -- repository access (C-9.5: direct, by time range) -----------------

    def _reference_close(self) -> float:
        rows = self._h1_range(self._intent.h1_last_ts, self._intent.h1_last_ts)
        if len(rows) == 0:
            raise OutcomeDataError(
                f"{self._native}: no H1 candle at h1_last_ts="
                f"{self._intent.h1_last_ts}; the reference close of C_ref is "
                f"required to decide whether the limit was pre-crossed (C-4.1)")
        return float(rows[0]["close"])

    def _h1_range(self, first_open: int, last_open: int) -> np.ndarray:
        if last_open < first_open:
            return np.empty(0, dtype=self._empty_dtype())
        try:
            return self._repo.range(
                self._native, "1h",
                start_ms=first_open * 1000, end_ms=last_open * 1000 + 1)
        except DatasetNotFound as exc:
            raise OutcomeDataError(
                f"{self._native}: no 1h dataset; a trade cannot be resolved "
                f"without prices") from exc
        except RepositoryError as exc:
            raise OutcomeDataError(f"{self._native} 1h: {exc}") from exc

    def _m5_set(self, h1_open: int) -> Optional[np.ndarray]:
        """The M5 candles covering one H1 candle, or None when the set is not
        exactly 12 — C-4.3's definition of complete."""
        try:
            rows = self._repo.range(
                self._native, "5m",
                start_ms=h1_open * 1000, end_ms=h1_open * 1000 + _H1_MS)
        except DatasetNotFound:
            return None
        except RepositoryError as exc:
            raise OutcomeDataError(f"{self._native} 5m: {exc}") from exc
        if len(rows) != M5_PER_H1:
            return None
        return rows

    def _data_end_open(self) -> Optional[int]:
        try:
            coverage = self._repo.coverage(self._native, "1h")
        except DatasetNotFound as exc:
            raise OutcomeDataError(
                f"{self._native}: no 1h dataset; a trade cannot be resolved "
                f"without prices") from exc
        return coverage.last_open_ms // 1000

    def _candle_index(self, open_time: int) -> int:
        return (open_time - self._intent.boundary) // _H1_SECONDS

    def _empty_dtype(self):
        from backtest.data.repository import KLINE_DTYPE
        return KLINE_DTYPE
