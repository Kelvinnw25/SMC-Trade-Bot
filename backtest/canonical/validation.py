"""Candle validation — every check REPLAY_CONTRACT.md C-6.10 requires, and no repair.

C-6.10 names what the builder MUST verify and record per `(symbol, timeframe)`:
row count; strictly increasing `open_time`; every gap written to a gap inventory;
`high >= max(open, close)`; `low <= min(open, close)`; `high >= low`; no duplicate
`open_time`; no `NaN` in OHLCV. It then states the consequence in one sentence:

    "A violation is a **hard error** — never a silent repair."

So this module raises. It has no repair path, no tolerance parameter, and no
"warn and continue" mode, because every one of those would let corrupt history
reach a strategy that cannot tell the difference.

## Gaps are not violations

A missing candle is ordinary: exchanges have outages, symbols delist mid-window,
low-liquidity pairs stop printing. C-6.3 forbids filling them and C-6.7 requires
windows to be served short rather than padded. So gaps are **inventoried, not
rejected** — recorded with start, end and missing count so a replay result can
be read against the holes in its own data.

## Why alignment and close-time are checked at all

Neither is in C-6.10's list. Both are checked because the repository's selection
rule (C-6.2) is arithmetic on `open_time + interval_ms`: a misaligned open time
would place a candle in a boundary it never belonged to, and a close time that
is not `open + interval - 1` would mean the row is not the timeframe it is filed
under. Both are exchange invariants that have held across every era measured
(2020-01 through 2026-03, 1d/1h/5m), so a violation means the artifact is not
what its name says — which is exactly the condition worth failing on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from backtest.canonical.errors import BuildError
from backtest.data.repository import INTERVAL_MS

__all__ = [
    "DuplicateCandle",
    "Gap",
    "InconsistentCloseTime",
    "InvalidValue",
    "MisalignedCandle",
    "OhlcInconsistent",
    "UnorderedCandles",
    "ValidationError",
    "ValidationReport",
    "check_ordering",
    "validate_dataset",
]


class ValidationError(BuildError):
    """A candle series violates an invariant the canonical tier guarantees."""


class UnorderedCandles(ValidationError):
    """Open times are not strictly increasing."""


class DuplicateCandle(ValidationError):
    """Two rows share an open time."""


class MisalignedCandle(ValidationError):
    """An open time is not a multiple of the timeframe interval."""


class InconsistentCloseTime(ValidationError):
    """`close_time != open_time + interval - 1`: the row is not the timeframe
    it is filed under."""


class InvalidValue(ValidationError):
    """A NaN, an infinity, a non-positive price, or a negative volume/count."""


class OhlcInconsistent(ValidationError):
    """`high`/`low` do not bound `open`/`close`, or `high < low`."""


@dataclass(frozen=True)
class Gap:
    """A run of missing candles: `missing` candles absent between two present
    ones. `start_ms` is the open time of the first absent candle and `end_ms`
    the open time of the last, both inclusive."""

    start_ms: int
    end_ms: int
    missing: int

    def to_dict(self) -> dict[str, int]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms,
                "missing": self.missing}


@dataclass(frozen=True)
class ValidationReport:
    """What a validated dataset contains. Everything here is measured from the
    rows, never taken from a manifest or a file name."""

    rows: int
    first_open_ms: int
    last_open_ms: int
    gaps: tuple[Gap, ...]

    @property
    def missing_candles(self) -> int:
        return sum(gap.missing for gap in self.gaps)


# ---------------------------------------------------------------------------

def check_ordering(rows: np.ndarray, *, source: str) -> None:
    """Open times are strictly increasing within one artifact.

    Run per artifact, *before* merging, so that a source which arrived out of
    order is reported against its own file rather than being absorbed into the
    merge and blamed on the dataset.
    """
    if len(rows) < 2:
        return
    times = rows["timestamp"]
    diffs = np.diff(times)
    bad = np.nonzero(diffs <= 0)[0]
    if bad.size:
        index = int(bad[0])
        if int(diffs[index]) == 0:
            raise DuplicateCandle(
                f"{source}: rows {index} and {index + 1} share open time "
                f"{int(times[index])}"
            )
        raise UnorderedCandles(
            f"{source}: open time decreases at row {index + 1}: "
            f"{int(times[index])} then {int(times[index + 1])}"
        )


def validate_dataset(
    rows: np.ndarray, *, symbol: str, timeframe: str
) -> ValidationReport:
    """Validate one merged `(symbol, timeframe)` series and inventory its gaps.

    Raises on the first violation, checked in a fixed order so the same corrupt
    input always produces the same error.
    """
    where = f"{symbol}|{timeframe}"
    interval = INTERVAL_MS[timeframe]
    if len(rows) == 0:
        return ValidationReport(rows=0, first_open_ms=0, last_open_ms=0, gaps=())

    times = rows["timestamp"]
    check_ordering(rows, source=where)

    misaligned = np.nonzero(times % interval != 0)[0]
    if misaligned.size:
        index = int(misaligned[0])
        raise MisalignedCandle(
            f"{where}: open time {int(times[index])} at row {index} is not a "
            f"multiple of the {timeframe} interval ({interval} ms)"
        )

    bad_close = np.nonzero(rows["ct"] != times + interval - 1)[0]
    if bad_close.size:
        index = int(bad_close[0])
        raise InconsistentCloseTime(
            f"{where}: row {index} (open {int(times[index])}) has close time "
            f"{int(rows['ct'][index])}, expected "
            f"{int(times[index]) + interval - 1}"
        )

    _check_values(rows, where=where)
    _check_ohlc(rows, where=where)

    return ValidationReport(
        rows=len(rows),
        first_open_ms=int(times[0]),
        last_open_ms=int(times[-1]),
        gaps=_find_gaps(times, interval=interval),
    )


# ---------------------------------------------------------------------------

#: Prices must be strictly positive. Volumes, quote volumes, taker volumes and
#: trade counts may legitimately be zero — a candle with no trades is real, and
#: rejecting it would delete history from illiquid symbols.
_PRICE_FIELDS = ("open", "high", "low", "close")
_NON_NEGATIVE_FIELDS = ("v", "qv", "nt", "tb", "tq")


def _check_values(rows: np.ndarray, *, where: str) -> None:
    for name in _PRICE_FIELDS + _NON_NEGATIVE_FIELDS + ("i",):
        column = rows[name]
        if column.dtype.kind != "f":
            continue
        bad = np.nonzero(~np.isfinite(column))[0]
        if bad.size:
            index = int(bad[0])
            raise InvalidValue(
                f"{where}: row {index} (open {int(rows['timestamp'][index])}) "
                f"has a non-finite {name!r}: {column[index]!r}"
            )

    for name in _PRICE_FIELDS:
        bad = np.nonzero(rows[name] <= 0)[0]
        if bad.size:
            index = int(bad[0])
            raise InvalidValue(
                f"{where}: row {index} (open {int(rows['timestamp'][index])}) "
                f"has a non-positive {name!r}: {rows[name][index]!r}"
            )

    for name in _NON_NEGATIVE_FIELDS:
        bad = np.nonzero(rows[name] < 0)[0]
        if bad.size:
            index = int(bad[0])
            raise InvalidValue(
                f"{where}: row {index} (open {int(rows['timestamp'][index])}) "
                f"has a negative {name!r}: {rows[name][index]!r}"
            )


def _check_ohlc(rows: np.ndarray, *, where: str) -> None:
    high, low = rows["high"], rows["low"]
    open_, close = rows["open"], rows["close"]

    for label, violated in (
        ("high < low", high < low),
        ("high < max(open, close)", high < np.maximum(open_, close)),
        ("low > min(open, close)", low > np.minimum(open_, close)),
    ):
        bad = np.nonzero(violated)[0]
        if bad.size:
            index = int(bad[0])
            raise OhlcInconsistent(
                f"{where}: row {index} (open {int(rows['timestamp'][index])}) "
                f"violates {label}: o={open_[index]!r} h={high[index]!r} "
                f"l={low[index]!r} c={close[index]!r}"
            )


def _find_gaps(times: Any, *, interval: int) -> tuple[Gap, ...]:
    """Every run of absent candles, in ascending order.

    Recorded, never filled (C-6.3 item 1). A gap is what makes a served window
    span more wall-clock time than its row count implies — which is precisely
    what live sees when Binance omits a kline.
    """
    if len(times) < 2:
        return ()
    diffs = np.diff(times)
    breaks = np.nonzero(diffs > interval)[0]
    gaps: list[Gap] = []
    for index in breaks:
        before = int(times[index])
        after = int(times[index + 1])
        gaps.append(Gap(
            start_ms=before + interval,
            end_ms=after - interval,
            missing=(after - before) // interval - 1,
        ))
    return tuple(gaps)
