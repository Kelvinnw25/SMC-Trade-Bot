"""The Reference Tier — instrument facts the kline corpus does not hold.

Milestone M12 (Priority 0: irreplaceable data). REPLAY_CONTRACT.md C-6.10 pattern,
applied to non-kline series.

## What this is for

Two series describe the *instruments* rather than their prices, and neither fits
the kline repository:

- **Instrument metadata** — `exchangeInfo`: status, contract type, onboard and
  delivery dates, tick size, step size, minimum notional. **No historical archive
  of this exists anywhere.** Binance serves the current state and nothing else, so
  a day not captured is a day lost permanently, and a symbol delisted before it was
  ever captured leaves no record that it existed. This is the only dataset in the
  repository whose acquisition cannot be deferred.
- **Funding rate history** — the periodic payment that dominates the cost of
  holding a perpetual. Bulk-archived by Binance back to 2020 and therefore
  *recoverable* later; captured here because it shares the acquisition path and
  because a cost model without it is not a cost model.

## What this is NOT for

**Nothing reads this tier.** No strategy input, no analysis, no lifecycle, no
replay, no outcome resolution, no metric. `OutcomeConfig.funding_rate_model`
remains `"none"`. This module captures and serves; deciding what a funding rate
*costs* belongs to the cost model, which is a later milestone with its own
justification.

The read-only rule is the same one that governs macro events: the strategy must
produce identical decisions whether this tier exists or not.

## Why it is a separate tier, and not another timeframe

`compute_snapshot_id` hashes the repository's `datasets` map. Adding funding as a
pseudo-timeframe would change `data_snapshot_id`, and two runs with different
`data_snapshot_id` are **never comparable** (C-6.10) — every replay run ever
produced would become retroactively incomparable with every future one. The
reference tier therefore carries its own `reference_snapshot_id` and touches
nothing the kline repository owns.

## Content-addressed snapshots

`exchangeInfo` is ~1 MB and changes rarely. Storing one file per capture would
produce thousands of near-identical artifacts. Instead the artifact path is derived
from the payload digest, so re-capturing unchanged metadata is a no-op commit
(`RawStore.commit` returns the existing record), and an append-only observation log
records *when* each distinct state was seen. The tier therefore stores instrument
**changes**, which is both compact and the form research actually asks for: "when
did this symbol's tick size change?" and "when did it stop being listed?"
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from backtest.acquire.storage import (
    AcquisitionError, ArtifactRecord, RAW_SCHEMA_VERSION, RawStore,
    expected_member_for, parse_binance_checksum, sha256_hex, verify_archive,
)
from backtest.acquire.transport import HttpTransport, TransportError, TransportNotFound
from backtest.data.repository import canonical_json

__all__ = [
    "FUNDING_DTYPE",
    "INSTRUMENT_FIELDS",
    "OBSERVATIONS_NAME",
    "REFERENCE_DIRNAME",
    "REFERENCE_MANIFEST_NAME",
    "REFERENCE_SCHEMA_VERSION",
    "FundingBuild",
    "InstrumentObservation",
    "ReferenceError",
    "ReferenceReport",
    "acquire_funding",
    "build_reference",
    "capture_instruments",
    "instrument_history",
    "read_funding",
    "reference_root",
    "verify_reference",
]

REFERENCE_SCHEMA_VERSION = 1

#: The tier lives beside the canonical klines, never inside them.
REFERENCE_DIRNAME = "reference"
REFERENCE_MANIFEST_NAME = "reference.json"
OBSERVATIONS_NAME = "instrument_observations.jsonl"

_BULK_BASE = "https://data.binance.vision/data/futures/um"
_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

#: Binance publishes a month's archive some days after it ends (the kline planner
#: uses the same allowance). Funding for the current month is simply not yet
#: available; nothing consumes it, so the gap is harmless and declared.
_MONTHLY_LAG_DAYS = 5

#: One funding settlement. `calc_time` is the **settlement** instant in epoch
#: milliseconds — deliberately not named `timestamp`, because it is not a candle
#: open time and must never be joined to one as though it were.
FUNDING_DTYPE = np.dtype([
    ("calc_time", "<i8"),
    ("interval_hours", "<i8"),
    ("funding_rate", "<f8"),
])

_FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"

#: The instrument attributes worth a permanent record. Chosen, not scraped: every
#: one either changes over time or disappears when a symbol is delisted.
INSTRUMENT_FIELDS = (
    "status", "contractType", "quoteAsset", "baseAsset",
    "onboardDate", "deliveryDate",
    "pricePrecision", "quantityPrecision",
    "tickSize", "stepSize", "minNotional",
)


class ReferenceError(Exception):
    """The reference tier cannot be built, read, or trusted."""


def reference_root(corpus_root: Path | str) -> Path:
    return Path(corpus_root) / REFERENCE_DIRNAME


# ---------------------------------------------------------------------------
# Instrument metadata — forward-only, content-addressed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstrumentObservation:
    """One capture of the exchange's instrument state."""

    observed_at: str          # ISO-8601 UTC, second precision
    sha256: str
    symbol_count: int
    artifact_path: str
    changed: bool             # False when identical to the previous observation

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "sha256": self.sha256,
            "symbol_count": self.symbol_count,
            "artifact_path": self.artifact_path,
            "changed": self.changed,
        }


def capture_instruments(
    corpus_root: Path | str,
    *,
    transport: HttpTransport,
    now: Optional[str] = None,
) -> InstrumentObservation:
    """Capture the exchange's current instrument state. Idempotent by content.

    Safe and cheap to run on a schedule. Because the artifact path is the payload
    digest, an unchanged exchange costs one no-op commit and one observation line;
    only a genuine change adds bytes.

    **This is the one acquisition in the repository that cannot be deferred.**
    Every other dataset can be back-filled from an archive; instrument state is
    served only as *now*, so the history is exactly what was captured and nothing
    more.
    """
    root = reference_root(corpus_root)
    store = RawStore(root)
    try:
        payload = transport.get(_EXCHANGE_INFO_URL)
    except TransportError as exc:
        raise ReferenceError(f"could not read exchangeInfo: {exc}") from exc
    if not payload:
        raise ReferenceError("exchangeInfo returned no bytes")

    try:
        parsed = json.loads(payload)
        symbols = parsed["symbols"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReferenceError(f"exchangeInfo is not the expected shape: {exc}") from exc

    digest = sha256_hex(payload)
    stamp = now if now is not None else _now_iso()
    relative = f"instruments/exchangeInfo-{digest[:16]}.json"

    previous = _observations(root)
    already = any(entry.sha256 == digest for entry in previous)

    store.commit(relative, payload, record=ArtifactRecord(
        schema_version=RAW_SCHEMA_VERSION,
        downloader_version=f"m12-reference-{REFERENCE_SCHEMA_VERSION}",
        downloaded_at=stamp,
        source_url=_EXCHANGE_INFO_URL,
        kind="exchange_info_json",
        symbol="", timeframe="",
        coverage_start_ms=0, coverage_end_ms=0,
        sha256=digest, size_bytes=len(payload),
        checksum_source="computed", artifact_path=relative,
    ))

    observation = InstrumentObservation(
        observed_at=stamp, sha256=digest, symbol_count=len(symbols),
        artifact_path=relative, changed=not already)
    path = root / OBSERVATIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(observation.to_dict(), sort_keys=True) + "\n")
    return observation


def _observations(root: Path) -> tuple[InstrumentObservation, ...]:
    path = Path(root) / OBSERVATIONS_NAME
    if not path.exists():
        return ()
    out: list[InstrumentObservation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(InstrumentObservation(**json.loads(line)))
    return tuple(out)


def instrument_history(corpus_root: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Per-symbol attribute history, derived from the captured snapshots.

    One entry per *change*, in observation order, so the answer to "when did this
    change, and what was it before?" is a list rather than a diff of two files. A
    symbol that disappears from a later snapshot gets a synthetic `absent` entry —
    delisting is not announced in `exchangeInfo`, it is simply an omission, and an
    omission nobody recorded is indistinguishable from a symbol that never existed.
    """
    root = reference_root(corpus_root)
    store = RawStore(root)
    history: dict[str, list[dict[str, Any]]] = {}
    previous: dict[str, dict[str, Any]] = {}

    for observation in _observations(root):
        if not observation.changed:
            continue
        payload = json.loads(
            store.path_for(observation.artifact_path).read_text(encoding="utf-8"))
        current = {entry["symbol"]: _attributes(entry)
                   for entry in payload.get("symbols", [])}
        for symbol, attributes in sorted(current.items()):
            if previous.get(symbol) != attributes:
                history.setdefault(symbol, []).append(
                    {"observed_at": observation.observed_at, **attributes})
        for symbol in sorted(set(previous) - set(current)):
            if previous[symbol].get("status") != "ABSENT":
                history.setdefault(symbol, []).append(
                    {"observed_at": observation.observed_at, "status": "ABSENT"})
                current[symbol] = {"status": "ABSENT"}
        previous = current
    return history


def _attributes(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project one `exchangeInfo` symbol onto `INSTRUMENT_FIELDS`.

    Filter values (`tickSize`, `stepSize`, `minNotional`) are nested inside the
    `filters` array rather than being top-level, and are flattened here so a
    change in any of them is visible as a change in one flat record.
    """
    out: dict[str, Any] = {}
    filters = {f.get("filterType"): f for f in entry.get("filters", [])}
    lookup = {
        "tickSize": ("PRICE_FILTER", "tickSize"),
        "stepSize": ("LOT_SIZE", "stepSize"),
        "minNotional": ("MIN_NOTIONAL", "notional"),
    }
    for name in INSTRUMENT_FIELDS:
        if name in lookup:
            group, key = lookup[name]
            value = filters.get(group, {}).get(key)
        else:
            value = entry.get(name)
        if value is not None:
            out[name] = value
    return out


# ---------------------------------------------------------------------------
# Funding rate history — bulk archives, back-fillable
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FundingBuild:
    """One symbol's canonical funding series."""

    symbol: str
    rows: int
    first_calc_ms: int
    last_calc_ms: int
    raw_sha256: tuple[str, ...]

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "first_calc_ms": self.first_calc_ms,
            "last_calc_ms": self.last_calc_ms,
            "raw_sha256": list(self.raw_sha256),
        }


def acquire_funding(
    corpus_root: Path | str,
    *,
    transport: HttpTransport,
    symbols: Sequence[str],
    start: date,
    end: date,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[int, int, tuple[str, ...]]:
    """Fetch monthly funding archives. Resumable; returns `(new, present, missing)`.

    Resume is structural, exactly as in the kline tier: a committed artifact is
    skipped, so re-running acquires only what is absent.

    A month with no archive is **not** an error. Binance publishes a month some
    days after it ends, and a symbol has no archive before it was listed; both are
    legitimately absent rather than failures, and both resolve by re-running later.
    """
    say = progress or (lambda _m: None)
    store = RawStore(reference_root(corpus_root))
    acquired = present = 0
    missing: list[str] = []

    for symbol in sorted(set(symbols)):
        for first_day in _months(start, end):
            period = f"{first_day.year:04d}-{first_day.month:02d}"
            name = f"{symbol}-fundingRate-{period}"
            relative = f"bulk/um/monthly/fundingRate/{symbol}/{name}.zip"
            if store.is_committed(relative):
                present += 1
                continue
            url = f"{_BULK_BASE}/monthly/fundingRate/{symbol}/{name}.zip"
            try:
                data = transport.get(url)
            except TransportNotFound:
                missing.append(f"{symbol}/{period}")
                continue
            except TransportError as exc:
                raise ReferenceError(f"{name}: {exc}") from exc

            expected = None
            try:
                expected = parse_binance_checksum(
                    transport.get(url + ".CHECKSUM").decode("utf-8"))
            except (TransportError, ValueError, UnicodeDecodeError):
                expected = None      # published checksums are best-effort
            member = expected_member_for(name + ".zip")
            verify_archive(data, expected_member=member)

            store.commit(relative, data, expected_sha256=expected,
                         record=ArtifactRecord(
                             schema_version=RAW_SCHEMA_VERSION,
                             downloader_version=f"m12-reference-{REFERENCE_SCHEMA_VERSION}",
                             downloaded_at=_now_iso(),
                             source_url=url,
                             kind="funding_monthly_zip",
                             symbol=symbol, timeframe="funding",
                             coverage_start_ms=_epoch_ms(first_day),
                             coverage_end_ms=_epoch_ms(_next_month(first_day)),
                             sha256=sha256_hex(data), size_bytes=len(data),
                             checksum_source="binance" if expected else "computed",
                             artifact_path=relative))
            acquired += 1
            say(f"funding {symbol} {period}")
    return acquired, present, tuple(missing)


def parse_funding_csv(text: str, *, source: str) -> np.ndarray:
    """Parse a Binance funding CSV into `FUNDING_DTYPE` rows.

    The archive carries a header (`calc_time,funding_interval_hours,
    last_funding_rate`). Row order is preserved; nothing is repaired.
    """
    rows: list[tuple[int, int, float]] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("calc_time"):
            if number == 1:
                continue
            raise ReferenceError(f"{source}: header at line {number}")
        parts = line.split(",")
        if len(parts) != 3:
            raise ReferenceError(
                f"{source}: line {number} has {len(parts)} fields, expected 3")
        try:
            rows.append((int(parts[0]), int(float(parts[1])), float(parts[2])))
        except ValueError as exc:
            raise ReferenceError(f"{source}: line {number}: {exc}") from exc
    return np.array(rows, dtype=FUNDING_DTYPE)


def build_reference(corpus_root: Path | str) -> "ReferenceReport":
    """Build the canonical reference tier from committed raw artifacts.

    Deterministic: artifacts are processed in sorted order, rows merged with a
    stable sort, duplicates on `calc_time` dropped, and the result validated as
    strictly increasing. No build timestamp is written — a `built_at` field would
    make every rebuild differ and destroy the identity.
    """
    root = reference_root(corpus_root)
    store = RawStore(root)
    by_symbol: dict[str, list[ArtifactRecord]] = {}
    for record in store.records():
        if record.kind == "funding_monthly_zip":
            by_symbol.setdefault(record.symbol, []).append(record)

    builds: list[FundingBuild] = []
    out_dir = root / "funding"
    for symbol in sorted(by_symbol):
        records = sorted(by_symbol[symbol],
                         key=lambda r: (r.coverage_start_ms, r.artifact_path))
        pieces = []
        for record in records:
            data = store.path_for(record.artifact_path).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = archive.namelist()[0]
                pieces.append(parse_funding_csv(
                    archive.read(member).decode("utf-8"), source=member))
        merged = _merge_funding(pieces, where=symbol)
        if not len(merged):
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_array(out_dir / f"{symbol}.npy", merged)
        builds.append(FundingBuild(
            symbol=symbol, rows=int(len(merged)),
            first_calc_ms=int(merged["calc_time"][0]),
            last_calc_ms=int(merged["calc_time"][-1]),
            raw_sha256=tuple(r.sha256 for r in records)))

    observations = _observations(root)
    report = ReferenceReport(
        root=root, funding=tuple(builds), observations=observations)
    _write_reference_manifest(root, report)
    return report


def _merge_funding(pieces: Sequence[np.ndarray], *, where: str) -> np.ndarray:
    if not pieces:
        return np.empty(0, dtype=FUNDING_DTYPE)
    merged = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
    order = np.argsort(merged["calc_time"], kind="stable")
    merged = merged[order]
    keep = np.ones(len(merged), dtype=bool)
    keep[1:] = merged["calc_time"][1:] != merged["calc_time"][:-1]
    duplicates = merged[~keep]
    for value in np.unique(duplicates["calc_time"]):
        same = merged[merged["calc_time"] == value]
        if len(np.unique(same["funding_rate"])) > 1:
            raise ReferenceError(
                f"{where}: two artifacts disagree about the funding rate at "
                f"calc_time {int(value)} — the raw tier is inconsistent and "
                f"must be re-acquired, never silently reconciled")
    merged = merged[keep]
    if len(merged) > 1 and not np.all(np.diff(merged["calc_time"]) > 0):
        raise ReferenceError(f"{where}: calc_time is not strictly increasing")
    return merged


def read_funding(corpus_root: Path | str, symbol: str) -> np.ndarray:
    """One symbol's funding series, read-only. Never consumed by the strategy."""
    path = reference_root(corpus_root) / "funding" / f"{symbol}.npy"
    if not path.exists():
        raise ReferenceError(f"no funding series for {symbol} at {path}")
    rows = np.load(path, allow_pickle=False)
    rows.flags.writeable = False
    return rows


# ---------------------------------------------------------------------------
# Identity, manifest, verification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferenceReport:
    """What the reference tier holds. Counts and identity, never values."""

    root: Path
    funding: tuple[FundingBuild, ...]
    observations: tuple[InstrumentObservation, ...] = ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(build.symbol for build in self.funding)

    @property
    def funding_rows(self) -> int:
        return sum(build.rows for build in self.funding)

    @property
    def distinct_instrument_states(self) -> int:
        return len({o.sha256 for o in self.observations})

    @property
    def reference_snapshot_id(self) -> str:
        """Identity of the tier. **Deliberately separate from `data_snapshot_id`.**

        Mixing the two would change the kline repository's identity every time a
        funding archive was added, and two runs with different `data_snapshot_id`
        are never comparable (C-6.10) — every existing replay run would become
        retroactively incomparable with every future one.

        Instrument observations enter by *content digest*, never by capture time,
        so re-capturing an unchanged exchange leaves the identity untouched.
        """
        payload = {
            "reference_schema_version": REFERENCE_SCHEMA_VERSION,
            "funding": {b.symbol: b.manifest_entry() for b in self.funding},
            "instrument_states": sorted({o.sha256 for o in self.observations}),
        }
        return hashlib.sha256(
            canonical_json(payload).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_schema_version": REFERENCE_SCHEMA_VERSION,
            "reference_snapshot_id": self.reference_snapshot_id,
            "funding_symbols": len(self.funding),
            "funding_rows": self.funding_rows,
            "funding": {b.symbol: b.manifest_entry() for b in self.funding},
            "instrument_observations": len(self.observations),
            "distinct_instrument_states": self.distinct_instrument_states,
            "consumed_by": [],
        }


def _write_reference_manifest(root: Path, report: ReferenceReport) -> Path:
    path = Path(root) / REFERENCE_MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(report.to_dict())
    payload["note"] = (
        "Research reference data. Read-only. Consumed by no strategy, analysis, "
        "lifecycle, entry, exit or risk path; the trading system produces "
        "identical decisions whether this tier exists or not.")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def load_reference(corpus_root: Path | str) -> dict[str, Any]:
    path = reference_root(corpus_root) / REFERENCE_MANIFEST_NAME
    if not path.exists():
        raise ReferenceError(f"no {REFERENCE_MANIFEST_NAME} at {path.parent}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_reference(corpus_root: Path | str, *, deep: bool = True
                     ) -> tuple[bool, tuple[str, ...]]:
    """Re-verify the tier against its own manifest. Returns `(ok, problems)`."""
    root = reference_root(corpus_root)
    problems: list[str] = []
    try:
        manifest = load_reference(corpus_root)
    except ReferenceError as exc:
        return False, (str(exc),)

    store = RawStore(root)
    if deep:
        try:
            store.verify_all()
        except AcquisitionError as exc:
            problems.append(f"raw tier: {exc}")

    rebuilt = build_reference(corpus_root)
    if rebuilt.reference_snapshot_id != manifest.get("reference_snapshot_id"):
        problems.append(
            f"reference_snapshot_id {rebuilt.reference_snapshot_id} != manifest "
            f"{manifest.get('reference_snapshot_id')}")
    for observation in _observations(root):
        if not store.is_committed(observation.artifact_path):
            problems.append(
                f"observation {observation.observed_at} references missing "
                f"artifact {observation.artifact_path}")
    return not problems, tuple(problems)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _months(start: date, end: date) -> list[date]:
    """Every whole month in `[start, end]` whose archive should exist."""
    out: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        last = _next_month(cursor) - timedelta(days=1)
        if last + timedelta(days=_MONTHLY_LAG_DAYS) <= end:
            out.append(cursor)
        cursor = _next_month(cursor)
    return out


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), (day.month % 12) + 1, 1)


def _epoch_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _write_array(path: Path, rows: np.ndarray) -> None:
    temp = path.with_suffix(path.suffix + ".part")
    with open(temp, "wb") as handle:
        np.save(handle, rows, allow_pickle=False)
    temp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
