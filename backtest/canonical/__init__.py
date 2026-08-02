"""backtest.canonical — the Canonical Historical Dataset Builder (Milestone M4.2).

Turns the immutable raw artifacts of `backtest.acquire` into the canonical
datasets `backtest.data` serves::

    raw artifacts  ->  parser  ->  validation  ->  canonical .npy + manifest

Three collaborators, one responsibility each:

- `parser`     — bytes to typed rows; both Binance CSV eras and REST JSON
- `validation` — every invariant C-6.10 requires, plus the gap inventory
- `builder`    — the forming-candle policy, merging, storage, manifest, identity

This is the **only** package importing both tiers. Neither tier imports it, so
acquisition and serving remain independent of each other and of this builder.

The output must represent exactly what the live bot could have observed at that
historical moment — no candle it could not yet have seen (the forming-candle
policy in `builder.drop_unfinished`), and no candle it would have seen filled in
or repaired (the prohibitions of C-6.3, enforced by raising instead).
"""
from backtest.canonical.builder import (
    BUILDER_VERSION,
    BuildReport,
    CanonicalBuilder,
    ConflictingCandles,
    CoverageMismatch,
    DatasetBuild,
    GAP_INVENTORY_NAME,
    SourceUnusable,
    drop_unfinished,
    observation_ms,
)
from backtest.canonical.errors import BuildError
from backtest.canonical.parser import (
    CSV_HEADER_FIELDS,
    MalformedRow,
    ParseError,
    RAW_COLUMN_COUNT,
    UnknownFormat,
    has_header,
    parse_kline_csv,
    parse_kline_json,
)
from backtest.canonical.validation import (
    DuplicateCandle,
    Gap,
    InconsistentCloseTime,
    InvalidValue,
    MisalignedCandle,
    OhlcInconsistent,
    UnorderedCandles,
    ValidationError,
    ValidationReport,
    check_ordering,
    validate_dataset,
)

__all__ = [
    "BUILDER_VERSION",
    "BuildError",
    "BuildReport",
    "CSV_HEADER_FIELDS",
    "CanonicalBuilder",
    "ConflictingCandles",
    "CoverageMismatch",
    "DatasetBuild",
    "DuplicateCandle",
    "GAP_INVENTORY_NAME",
    "Gap",
    "InconsistentCloseTime",
    "InvalidValue",
    "MalformedRow",
    "MisalignedCandle",
    "OhlcInconsistent",
    "ParseError",
    "RAW_COLUMN_COUNT",
    "SourceUnusable",
    "UnknownFormat",
    "UnorderedCandles",
    "ValidationError",
    "ValidationReport",
    "check_ordering",
    "drop_unfinished",
    "has_header",
    "observation_ms",
    "parse_kline_csv",
    "parse_kline_json",
    "validate_dataset",
]
