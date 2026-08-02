"""The canonical universe resolver — one eligibility rule, two metadata sources.

## The problem this exists to solve

`v2.domain.universe.is_eligible` needs three `exchangeInfo` fields:
`contractType`, `underlyingType`, `quoteAsset`. Live always has them, because it
asks the exchange about instruments the exchange is currently listing. Replay
often does not: **Binance serves only the current state**, so an instrument
delisted before this project began committing snapshots (2026-07-31) has no
surviving authoritative record anywhere.

Measured on the Stage 1 corpus: 766 of 797 datasets are answered by a committed
snapshot. The remaining 31 are not, and they are not a rounding error — 23 of
them entered the top-30 universe, and one (LUNAUSDT) held a top-30 slot for 79%
of the corpus span.

## Why the table stores metadata and never a verdict

A table of verdicts would be a *second* eligibility rule, free to drift from
`is_eligible` forever without anything noticing. So the table supplies the
missing metadata triple and nothing else, and eligibility is computed by the
same function live uses. There is exactly one rule in the system.

## Resolution precedence

    1. committed exchangeInfo snapshots     authoritative, automatic
    2. reference/universe_table.json        curated, evidenced, closed-ended
    3. hard failure                         never a default

Rule 3 is load-bearing. Defaulting an unknown symbol to *excluded* is
survivorship bias; defaulting it to *included* admits tokenised equities,
commodities and index products into the traded universe. Neither default is
safe, so there is no default: an unclassifiable instrument stops the run and
names itself.

Two snapshots that disagree about an instrument's type are also a hard failure.
Never newest-wins, never majority vote: disagreement means either the exchange
re-purposed a ticker or a snapshot is corrupt, and both must be looked at by a
human rather than silently resolved.

## No network

Everything resolves from bytes already committed inside the corpus. This module
performs no HTTP, and a coverage or replay run is reproducible from the corpus
plus this repository alone.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from backtest.data.repository import canonical_json
from v2.domain.universe import (
    UNIVERSE_DEFINITION_VERSION,
    InstrumentMetadata,
    is_eligible,
)

__all__ = [
    "Classification",
    "SOURCE_SNAPSHOT",
    "SOURCE_TABLE",
    "SnapshotDisagreement",
    "UNIVERSE_TABLE_SCHEMA_VERSION",
    "UniverseError",
    "UniverseResolution",
    "UniverseTable",
    "UniverseTableError",
    "UnresolvedInstruments",
    "default_table_path",
    "load_table",
    "read_snapshots",
    "resolve",
]

UNIVERSE_TABLE_SCHEMA_VERSION = 1

#: Where a classification came from. Recorded per symbol so a reader can tell
#: exchange-authoritative metadata from curated backfill at a glance.
SOURCE_SNAPSHOT = "snapshot"
SOURCE_TABLE = "table"

#: Committed `exchangeInfo` payloads live in two places, both write-once:
#: the acquisition provenance tier and the Reference Tier. Both are read, because
#: an instrument captured by either is one fewer row a human has to curate.
_SNAPSHOT_GLOBS = (
    "raw/discovery/*exchange_info.json",
    "reference/instruments/exchangeInfo-*.json",
)

#: `RawStore` writes `<artifact>.meta.json` beside every payload as its commit
#: marker. It matches the globs above and is not a snapshot.
_SIDECAR_SUFFIX = ".meta.json"


class UniverseError(Exception):
    """The historical universe cannot be determined."""


class UniverseTableError(UniverseError):
    """The classification table is malformed, stale, or self-inconsistent."""


class UnresolvedInstruments(UniverseError):
    """Instruments that no snapshot and no table row classifies.

    Carries the names, because the whole point of failing is to hand the
    operator the exact worklist rather than a count.
    """

    def __init__(self, symbols: Iterable[str]) -> None:
        self.symbols = tuple(sorted(symbols))
        listed = ", ".join(self.symbols[:20])
        more = "" if len(self.symbols) <= 20 else f" (+{len(self.symbols) - 20} more)"
        super().__init__(
            f"{len(self.symbols)} instrument(s) cannot be classified: {listed}"
            f"{more}. Every symbol must resolve from a committed exchangeInfo "
            f"snapshot or from reference/universe_table.json. There is no "
            f"default: excluding an unknown symbol would be survivorship bias "
            f"and including it would admit non-crypto instruments."
        )


class SnapshotDisagreement(UniverseError):
    """Two committed snapshots classify the same instrument differently."""

    def __init__(self, symbol: str, variants: Iterable[tuple]) -> None:
        self.symbol = symbol
        self.variants = tuple(sorted(variants))
        super().__init__(
            f"committed snapshots disagree about {symbol}: {list(self.variants)}. "
            f"This is never resolved by precedence, recency or majority — it "
            f"means the exchange re-purposed a ticker or a snapshot is corrupt, "
            f"and the historical universe is undetermined until a human says "
            f"which is right."
        )


def default_table_path() -> Path:
    """`<repo>/reference/universe_table.json`."""
    return Path(__file__).resolve().parent.parent / "reference" / "universe_table.json"


# ---------------------------------------------------------------------------
# One instrument
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    """One instrument's metadata triple, and where it came from.

    Carries no eligibility field, deliberately. `eligible` is a property that
    calls `is_eligible`, so a stored verdict cannot exist to go stale.
    """

    symbol: str
    contract_type: Optional[str]
    underlying_type: Optional[str]
    quote_asset: Optional[str]
    source: str

    @property
    def metadata(self) -> InstrumentMetadata:
        return InstrumentMetadata(
            symbol=self.symbol, contract_type=self.contract_type,
            underlying_type=self.underlying_type, quote_asset=self.quote_asset)

    @property
    def eligible(self) -> bool:
        """The one rule, the same one live applies."""
        return is_eligible(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "contract_type": self.contract_type,
                "underlying_type": self.underlying_type,
                "quote_asset": self.quote_asset, "source": self.source,
                "eligible": self.eligible}


# ---------------------------------------------------------------------------
# The curated table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UniverseTable:
    """`reference/universe_table.json`, validated."""

    version: int
    sha256: str
    rows: dict[str, Classification]

    def __len__(self) -> int:
        return len(self.rows)


def _table_digest(rows: list[dict[str, Any]]) -> str:
    """sha256 over the canonically encoded row array.

    Covers the whole row — evidence, URLs, notes — so that any edit to the table
    is visible as a changed digest. Order-independent because the rows are sorted
    by symbol first, and formatting-independent because the file is parsed and
    re-encoded canonically before hashing.
    """
    ordered = sorted(rows, key=lambda row: str(row.get("symbol", "")))
    return hashlib.sha256(canonical_json(ordered).encode("utf-8")).hexdigest()


def load_table(path: Optional[Path | str] = None) -> UniverseTable:
    """Read and validate the classification table.

    Every check here is a refusal to proceed on a table that might be wrong:
    an unknown schema, a digest that does not match the rows, a duplicated
    symbol, a missing field, or a row whose `expected_classification` disagrees
    with what `is_eligible` computes from its own triple. The last is a
    transcription guard — a typo in `underlying_type` silently flips a symbol's
    eligibility, and the redundant field is what makes it loud.
    """
    location = Path(path) if path is not None else default_table_path()
    try:
        document = json.loads(location.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UniverseTableError(f"cannot read {location}: {exc}") from exc
    except ValueError as exc:
        raise UniverseTableError(f"{location} is not valid JSON: {exc}") from exc

    schema = document.get("schema_version")
    if schema != UNIVERSE_TABLE_SCHEMA_VERSION:
        raise UniverseTableError(
            f"{location} declares schema_version {schema!r}; this build "
            f"understands {UNIVERSE_TABLE_SCHEMA_VERSION}")

    raw_rows = document.get("rows")
    if not isinstance(raw_rows, list):
        raise UniverseTableError(f"{location} has no 'rows' array")

    declared = document.get("universe_table_sha256")
    actual = _table_digest(raw_rows)
    if declared != actual:
        raise UniverseTableError(
            f"{location} declares universe_table_sha256 {declared!r} but its "
            f"rows hash to {actual!r}. The table was edited without updating "
            f"its digest, so no run can cite it.")

    count = document.get("symbol_count")
    if count != len(raw_rows):
        raise UniverseTableError(
            f"{location} declares symbol_count {count!r} but holds "
            f"{len(raw_rows)} row(s)")

    version = document.get("table_version")
    if not isinstance(version, int):
        raise UniverseTableError(
            f"{location} has no integer table_version")

    rows: dict[str, Classification] = {}
    for row in raw_rows:
        symbol = row.get("symbol")
        if not symbol:
            raise UniverseTableError(f"{location} holds a row with no symbol")
        if symbol in rows:
            raise UniverseTableError(
                f"{location} classifies {symbol} more than once")
        for field in ("contract_type", "underlying_type", "quote_asset",
                      "evidence", "evidence_kind", "confidence", "source",
                      "decided_by", "decided_at", "expected_classification"):
            if row.get(field) in (None, ""):
                raise UniverseTableError(
                    f"{location}: {symbol} is missing required field {field!r}")
        entry = Classification(
            symbol=str(symbol), contract_type=row["contract_type"],
            underlying_type=row["underlying_type"],
            quote_asset=row["quote_asset"], source=SOURCE_TABLE)
        expected = row["expected_classification"]
        if expected not in ("CRYPTO_PERPETUAL", "NOT_CRYPTO"):
            raise UniverseTableError(
                f"{location}: {symbol} has unknown expected_classification "
                f"{expected!r}")
        if entry.eligible != (expected == "CRYPTO_PERPETUAL"):
            raise UniverseTableError(
                f"{location}: {symbol} declares expected_classification "
                f"{expected!r} but its metadata triple "
                f"({entry.contract_type}, {entry.underlying_type}, "
                f"{entry.quote_asset}) computes eligible={entry.eligible}. One "
                f"of the two is a typo, and which one is not for this code to "
                f"guess.")
        rows[str(symbol)] = entry

    return UniverseTable(version=version, sha256=actual, rows=rows)


# ---------------------------------------------------------------------------
# Committed snapshots
# ---------------------------------------------------------------------------

def read_snapshots(
    corpus_root: Path | str,
) -> dict[str, tuple[str, str, str]]:
    """Every instrument any committed `exchangeInfo` snapshot classifies.

    Reads *all* snapshots, not the newest. A symbol delisted between two
    captures is classified by the earlier one, automatically and forever, which
    is what makes the curated table closed-ended rather than a growing debt.

    An entry missing any of the three fields is skipped rather than recorded as
    a partial triple: a `None` would compute `eligible=False` and silently
    exclude the instrument, which is the survivorship bias this module exists to
    prevent. Skipping lets it fall through to the table, or fail loudly.
    """
    root = Path(corpus_root)
    seen: dict[str, set[tuple[str, str, str]]] = {}
    for pattern in _SNAPSHOT_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.name.endswith(_SIDECAR_SUFFIX):
                # The store's commit marker, not a payload. It sits beside every
                # artifact and matches the same glob.
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UniverseError(
                    f"cannot read committed snapshot {path}: {exc}. A snapshot "
                    f"that cannot be parsed is not treated as absent, because "
                    f"that would silently move instruments into the curated "
                    f"table's jurisdiction.") from exc
            entries = payload.get("symbols")
            if not isinstance(entries, list):
                raise UniverseError(
                    f"committed snapshot {path} has no 'symbols' array")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                symbol = str(entry.get("symbol", ""))
                triple = (entry.get("contractType"), entry.get("underlyingType"),
                          entry.get("quoteAsset"))
                if not symbol or any(value is None for value in triple):
                    continue
                seen.setdefault(symbol, set()).add(triple)   # type: ignore[arg-type]

    resolved: dict[str, tuple[str, str, str]] = {}
    for symbol, variants in seen.items():
        if len(variants) > 1:
            raise SnapshotDisagreement(symbol, variants)
        resolved[symbol] = next(iter(variants))
    return resolved


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UniverseResolution:
    """The classification of every instrument in a corpus, and its identity."""

    universe_id: str
    definition_version: int
    table_version: int
    table_sha256: str
    classifications: tuple[Classification, ...]

    @property
    def eligible_symbols(self) -> frozenset[str]:
        """The Binance-native names replay and coverage may rank."""
        return frozenset(c.symbol for c in self.classifications if c.eligible)

    def by_symbol(self) -> dict[str, Classification]:
        return {c.symbol: c for c in self.classifications}

    def provenance(self) -> dict[str, int]:
        counts = {SOURCE_SNAPSHOT: 0, SOURCE_TABLE: 0}
        for entry in self.classifications:
            counts[entry.source] = counts.get(entry.source, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_id": self.universe_id,
            "universe_definition_version": self.definition_version,
            "table_version": self.table_version,
            "universe_table_sha256": self.table_sha256,
            "classified": len(self.classifications),
            "eligible": len(self.eligible_symbols),
            "provenance": self.provenance(),
        }


def _universe_id(
    *, table_version: int, table_sha256: str,
    classifications: tuple[Classification, ...],
) -> str:
    """`sha256(definition version + table identity + every resolved triple)[:16]`.

    **What is hashed.** The universe definition version, the classification
    table's version and digest, and for every symbol its resolved
    `(contract_type, underlying_type, quote_asset, source)`.

    **Ordering.** `canonical_json` sorts keys, and `resolved` is a mapping keyed
    by symbol, so the digest cannot depend on the order rows appear in the table,
    the order snapshots are globbed, or the iteration order of any dict.

    **Normalisation.** Every input is parsed from JSON and re-encoded through the
    project's single canonical encoder (C-9.7: sorted keys, no whitespace, ASCII,
    no NaN), so indentation, key order and line endings in the source files
    cannot reach the digest.

    **Why it stays stable.** It contains no timestamp, no path, no row ordering
    and no corpus content. Re-running against the same corpus with the same
    table gives the same value forever. It changes exactly when the *classified
    metadata* changes — which is precisely when the ranked candidate set could
    change.

    **Why it is separate from `corpus_id`.** `_corpus_id` excludes
    `_PROVENANCE_KINDS = {"discovery_json"}`, so two corpora with identical
    `corpus_id` may hold different snapshots. That exclusion is correct — two
    downloads of the same history must produce the same corpus — but it means
    `corpus_id` alone no longer determines what replay ranks. `universe_id` is
    the narrower identity that does.
    """
    payload = {
        "universe_definition_version": UNIVERSE_DEFINITION_VERSION,
        "table_version": table_version,
        "universe_table_sha256": table_sha256,
        "resolved": {
            entry.symbol: [entry.contract_type, entry.underlying_type,
                           entry.quote_asset, entry.source]
            for entry in classifications
        },
    }
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def resolve(
    symbols: Iterable[str],
    *,
    corpus_root: Path | str,
    table_path: Optional[Path | str] = None,
) -> UniverseResolution:
    """Classify every symbol, or fail naming the ones that cannot be.

    Each symbol resolves exactly once: the input is de-duplicated and sorted, so
    the output holds one `Classification` per distinct name regardless of how
    many times the caller supplied it.
    """
    table = load_table(table_path)
    snapshots = read_snapshots(corpus_root)

    classified: list[Classification] = []
    unresolved: list[str] = []
    for symbol in sorted(set(symbols)):
        triple = snapshots.get(symbol)
        if triple is not None:
            classified.append(Classification(
                symbol=symbol, contract_type=triple[0],
                underlying_type=triple[1], quote_asset=triple[2],
                source=SOURCE_SNAPSHOT))
            continue
        row = table.rows.get(symbol)
        if row is not None:
            classified.append(row)
            continue
        unresolved.append(symbol)

    if unresolved:
        raise UnresolvedInstruments(unresolved)

    entries = tuple(classified)
    return UniverseResolution(
        universe_id=_universe_id(
            table_version=table.version, table_sha256=table.sha256,
            classifications=entries),
        definition_version=UNIVERSE_DEFINITION_VERSION,
        table_version=table.version,
        table_sha256=table.sha256,
        classifications=entries,
    )
