"""Raw artifact parsing — bytes that Binance served become typed kline rows.

Two wire formats exist and **both are live in the archive today**, so neither may
be assumed (M4.1 finding V4, measured):

- **Headerless CSV** — every futures archive up to roughly 2023-05.
- **Headed CSV** — `open_time,open,high,low,close,volume,close_time,quote_volume,
  count,taker_buy_volume,taker_buy_quote_volume,ignore`, from roughly 2023-06 on.

The same symbol and timeframe has both forms across its history, so the format is
**detected per artifact**, never inferred from the symbol, the period, or the
first artifact seen. Assuming a header would silently drop the first candle of
every old file; assuming none would fail loudly on every new one — the first
mistake is the dangerous one, which is why detection is explicit and a
first line that is neither is a hard error rather than a guess.

REST responses (`fapi/v1/klines`) are a third shape: a JSON array of 12-element
arrays whose numbers arrive as a mix of `int` and `str`.

## What a parser must never do

Never skip a row, never reorder rows, never repair a value, never invent a
candle. Anything a parser cannot turn into exactly one row raises. Row *order is
preserved exactly as served* — ordering is verified later (`validation`), not
imposed here, so a source that is out of order is reported rather than quietly
sorted.
"""
from __future__ import annotations

import json
from typing import Sequence

import numpy as np

from backtest.canonical.errors import BuildError
from backtest.data.repository import COLUMNS, KLINE_DTYPE

__all__ = [
    "CSV_HEADER_FIELDS",
    "MalformedRow",
    "ParseError",
    "RAW_COLUMN_COUNT",
    "UnknownFormat",
    "parse_kline_csv",
    "parse_kline_json",
]


class ParseError(BuildError):
    """A raw artifact could not be turned into kline rows."""


class UnknownFormat(ParseError):
    """The artifact's shape is not one this parser knows. Never guessed at."""


class MalformedRow(ParseError):
    """One row is not twelve parseable fields. Named with its line number, so a
    2 GB archive with one bad row says which row."""


#: Binance kline files have exactly twelve columns in both eras (verified against
#: 2020-01 and 2026-03 archives for 1d/1h/5m).
RAW_COLUMN_COUNT = len(COLUMNS)

#: The header Binance writes since ~2023-06. Only the first field is used for
#: detection — the rest is recorded here because a *different* header would mean
#: the format changed again, and that must fail rather than be tolerated.
CSV_HEADER_FIELDS: tuple[str, ...] = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume",
    "ignore",
)

#: Which columns are integral, derived from the repository's dtype rather than
#: restated — so the parser cannot drift from the storage schema it feeds.
_INT_INDEXES = frozenset(
    index for index, name in enumerate(COLUMNS) if KLINE_DTYPE[name].kind == "i"
)


def _empty() -> np.ndarray:
    return np.empty(0, dtype=KLINE_DTYPE)


def has_header(first_line: str, *, source: str) -> bool:
    """Whether `first_line` is a header row, decided explicitly.

    A line whose first field parses as an integer is data. A line whose first
    field is `open_time` is the known header. **Anything else raises** — a third
    format must stop the build, not be absorbed by a heuristic.
    """
    fields = first_line.split(",")
    if len(fields) != RAW_COLUMN_COUNT:
        raise UnknownFormat(
            f"{source}: first line has {len(fields)} field(s), expected "
            f"{RAW_COLUMN_COUNT}: {first_line[:120]!r}"
        )
    try:
        int(fields[0])
    except ValueError:
        pass
    else:
        return False

    if tuple(f.strip() for f in fields) == CSV_HEADER_FIELDS:
        return True
    raise UnknownFormat(
        f"{source}: first line is neither a kline row nor the known Binance "
        f"header; refusing to guess. Got {first_line[:200]!r}"
    )


def parse_kline_csv(text: str, *, source: str) -> np.ndarray:
    """Parse a Binance kline CSV, headed or headless, into `KLINE_DTYPE` rows.

    `source` names the artifact in every error message. Row order is preserved.
    """
    lines = text.splitlines()
    if not lines:
        return _empty()

    start = 1 if has_header(lines[0], source=source) else 0
    rows: list[tuple] = []
    for offset, line in enumerate(lines[start:]):
        line_no = start + offset + 1
        if not line.strip():
            # Not a candle, and not silently skippable: a blank line inside a
            # kline file means the artifact is not what it claims to be.
            raise MalformedRow(f"{source}: line {line_no} is blank")
        rows.append(_parse_fields(line.split(","), source=source, line_no=line_no))
    return _to_array(rows)


def parse_kline_json(payload: bytes, *, source: str) -> np.ndarray:
    """Parse a `fapi/v1/klines` response into `KLINE_DTYPE` rows.

    An **empty array is a legitimate answer**, not a failure: M4.1 commits it as
    evidence that a period was requested and the exchange had nothing (finding
    F3). It parses to zero rows, and zero rows contribute nothing to a dataset.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{source}: not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise UnknownFormat(
            f"{source}: expected a JSON array of klines, got "
            f"{type(data).__name__}"
        )
    rows: list[tuple] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, list):
            raise MalformedRow(
                f"{source}: element {index} is {type(entry).__name__}, "
                f"expected an array of {RAW_COLUMN_COUNT} values"
            )
        rows.append(_parse_fields(entry, source=source, line_no=index))
    return _to_array(rows)


# ---------------------------------------------------------------------------

def _parse_fields(fields: Sequence, *, source: str, line_no: int) -> tuple:
    """Twelve raw fields to one typed row. Integral columns are parsed as
    integers so a fractional epoch or trade count fails here rather than being
    silently truncated into the dtype."""
    if len(fields) != RAW_COLUMN_COUNT:
        raise MalformedRow(
            f"{source}: line {line_no} has {len(fields)} field(s), expected "
            f"{RAW_COLUMN_COUNT}"
        )
    values: list = []
    for index, field in enumerate(fields):
        try:
            values.append(int(field) if index in _INT_INDEXES else float(field))
        except (TypeError, ValueError) as exc:
            raise MalformedRow(
                f"{source}: line {line_no}, column {index} "
                f"({COLUMNS[index]!r}): cannot parse {field!r}: {exc}"
            ) from exc
    return tuple(values)


def _to_array(rows: list[tuple]) -> np.ndarray:
    if not rows:
        return _empty()
    return np.array(rows, dtype=KLINE_DTYPE)
