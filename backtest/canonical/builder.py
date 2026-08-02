"""The Canonical Historical Dataset Builder (Milestone M4.2).

The bridge between the two tiers REPLAY_CONTRACT.md C-6.10 requires:

    immutable raw tier  ->  [ this module ]  ->  canonical tier
    (backtest.acquire)                          (read by backtest.data)

It is the **only** component that imports both. `backtest.acquire` still knows
nothing about the repository and `backtest.data` still knows nothing about
downloading; the dependency runs one way through here, which is what keeps
"re-acquire" and "re-build" independent operations.

## What it produces

Exactly the layout `backtest.data` reads, and nothing else::

    <root>/manifest.json
    <root>/gap_inventory.json
    <root>/klines/<SYMBOL>/<timeframe>.npy

`<SYMBOL>` is the Binance-native name. Format constants, the dataset path, and
`compute_snapshot_id` are **imported from the repository**, never restated — the
writer and the reader agree by construction rather than by review.

## Determinism

Two builds over identical raw artifacts produce byte-identical `.npy` files, a
byte-identical manifest, and the same `data_snapshot_id`. That is why no build
timestamp is written anywhere: a `built_at` field would make every rebuild
differ and destroy the one property that makes a snapshot id worth having.
Artifacts are processed in a fixed order (coverage start, then path), rows are
merged with a stable sort, and JSON is emitted with sorted keys.

## What it never does

No filling, no interpolation, no resampling, no repair, no reordering of rows
within an artifact, no synthesis of a candle that Binance did not serve
(C-6.3). Every violation of an invariant is an exception. Gaps are inventoried,
because a gap is history, not damage.

Deliberately absent: ATR. C-6.5 makes ATR the adapter's responsibility, computed
**after** slicing on exactly the served rows. Precomputing it here and slicing
later would silently change every indicator value in replay.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np

from backtest.acquire.storage import ArtifactRecord, RawStore, expected_member_for
from backtest.canonical.errors import BuildError
from backtest.canonical.parser import parse_kline_csv, parse_kline_json
from backtest.canonical.validation import (
    Gap, ValidationReport, check_ordering, validate_dataset,
)
from backtest.data.repository import (
    FORMAT_VERSION, INTERVAL_MS, KLINE_DTYPE, MANIFEST_NAME, TIMEFRAMES,
    compute_snapshot_id, dataset_path,
)

__all__ = [
    "BUILDER_VERSION",
    "BuildReport",
    "CanonicalBuilder",
    "ConflictingCandles",
    "CoverageMismatch",
    "DatasetBuild",
    "GAP_INVENTORY_NAME",
    "SourceUnusable",
    "drop_unfinished",
    "observation_ms",
]

#: Stamped into the manifest and hashed into `data_snapshot_id`. Bump whenever a
#: build over unchanged raw artifacts would produce different canonical rows —
#: that is the whole purpose of the field.
BUILDER_VERSION = "m4.2-1.0.0"

GAP_INVENTORY_NAME = "gap_inventory.json"

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class SourceUnusable(BuildError):
    """A raw artifact cannot be read as the kline source it claims to be."""


class ConflictingCandles(BuildError):
    """Two artifacts report **different values** for the same open time.

    Never resolved by preferring one source. Identical duplicates are the same
    evidence twice and collapse silently; a genuine disagreement means one of the
    two artifacts is wrong, and only discarding it explicitly (`RawStore.discard`)
    can say which.
    """


class CoverageMismatch(BuildError):
    """An artifact contains a candle outside the period its own name and sidecar
    claim it covers — the file is mis-filed, and trusting its name would file
    those rows under the wrong symbol, timeframe, or day."""


# ---------------------------------------------------------------------------
# Task 3 — the forming-candle policy
# ---------------------------------------------------------------------------

def observation_ms(record: ArtifactRecord) -> int:
    """The instant an artifact was observed, in epoch ms.

    This is the sidecar's `downloaded_at`, which M4.1 records at second
    precision. Truncation to the second makes the instant *earlier* than reality,
    so the completeness test below can only ever be stricter — it may drop a
    candle that had just closed, and can never keep one that had not.
    """
    try:
        moment = datetime.strptime(record.downloaded_at, _ISO_FORMAT)
    except ValueError as exc:
        raise BuildError(
            f"{record.artifact_path}: unreadable downloaded_at "
            f"{record.downloaded_at!r}: {exc}"
        ) from exc
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


def drop_unfinished(
    rows: np.ndarray, *, timeframe: str, observed_ms: int
) -> tuple[np.ndarray, int]:
    """Remove candles that had not fully closed when the artifact was observed.

    **The rule** (M4.1 finding V5). A candle is complete at instant `t` when::

        open_time_ms + INTERVAL_MS[timeframe] <= t

    which is C-6.2's selection rule with `t` = the observation instant. It is
    evaluated on `open_time + interval`, **never** on the stored `ct` column:
    `ct` is `open + interval - 1`, and comparing against it invites the
    off-by-one-millisecond error that decision R-4 exists to prevent.

    **Why this is not optional.** `fapi/v1/klines` returns the *in-progress*
    candle, with partial high, low, close and volume — measured live during the
    M4.1 gate: a `1h` request at 12:23 UTC returned the 12:00 candle whose close
    time was 12:59:59.999. Committing that row to the canonical tier would place
    a candle in history whose values were never the values the exchange finally
    printed. The repository's serve-time guard (C-6.2) does **not** save it: that
    guard only asks whether a candle's interval has elapsed at the scan boundary
    `T`, so once `T` passes 13:00 the partial row is served as a closed candle
    and every indicator computed over it is wrong. Worse, its `close` is the
    price at *download* time — information from inside the candle — which is
    look-ahead in the most literal sense.

    **Uniform application.** Applied to every artifact kind, not just REST. Bulk
    archives are published only for completed periods, so the rule drops nothing
    from them; making it unconditional means there is one rule to reason about
    rather than a per-source policy that could be wrong for a source added later.

    Returns the kept rows and how many were dropped.
    """
    if len(rows) == 0:
        return rows, 0
    keep = rows["timestamp"] + INTERVAL_MS[timeframe] <= observed_ms
    dropped = int((~keep).sum())
    return (rows if dropped == 0 else rows[keep]), dropped


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DatasetBuild:
    """One built `(symbol, timeframe)` dataset and where every row came from."""

    symbol: str
    timeframe: str
    rows: int
    first_open_ms: int
    last_open_ms: int
    gaps: tuple[Gap, ...]
    raw_sha256: tuple[str, ...]
    source_artifacts: tuple[str, ...]
    dropped_unfinished: int
    duplicates_collapsed: int

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}"

    @property
    def missing_candles(self) -> int:
        return sum(gap.missing for gap in self.gaps)

    def manifest_entry(self) -> dict:
        """The manifest record for this dataset.

        The first four keys are what `compute_snapshot_id` hashes and what
        `KlineRepository` validates; the rest is provenance the repository
        ignores by design, so adding to it can never change a snapshot id.
        """
        return {
            "rows": self.rows,
            "first_open_ms": self.first_open_ms,
            "last_open_ms": self.last_open_ms,
            "raw_sha256": list(self.raw_sha256),
            "source_artifacts": list(self.source_artifacts),
            "gap_count": len(self.gaps),
            "missing_candles": self.missing_candles,
            "dropped_unfinished": self.dropped_unfinished,
            "duplicates_collapsed": self.duplicates_collapsed,
        }


@dataclass(frozen=True)
class BuildReport:
    """The complete result of one build. Nothing is omitted or summarised away."""

    root: Path
    data_snapshot_id: str
    builder_version: str
    datasets: tuple[DatasetBuild, ...]
    empty: tuple[str, ...] = ()
    survivorship_incomplete: bool = True
    missing_symbols: tuple[str, ...] = ()

    @property
    def total_rows(self) -> int:
        return sum(d.rows for d in self.datasets)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({d.symbol for d in self.datasets}))

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "data_snapshot_id": self.data_snapshot_id,
            "builder_version": self.builder_version,
            "datasets": len(self.datasets),
            "symbols": len(self.symbols),
            "total_rows": self.total_rows,
            "empty": list(self.empty),
            "survivorship_incomplete": self.survivorship_incomplete,
            "missing_symbols": list(self.missing_symbols),
        }


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

class CanonicalBuilder:
    """Builds canonical datasets from a `RawStore`.

    Holds no state between builds: the same store and the same arguments always
    produce the same output, and two builders over one store cannot disagree.

    `verify_integrity` re-verifies every artifact's recorded SHA-256 and ZIP
    structure before parsing it (`RawStore.verify`). It defaults to on, because
    building from bytes that silently rotted is the failure this whole two-tier
    design exists to prevent; it costs one full read and hash per artifact and
    can be disabled for a rebuild that has just been verified.
    """

    def __init__(
        self,
        store: RawStore,
        *,
        builder_version: str = BUILDER_VERSION,
        verify_integrity: bool = True,
    ) -> None:
        self._store = store
        self._version = builder_version
        self._verify = verify_integrity

    # -- building ----------------------------------------------------------

    def build(
        self,
        root: Path | str,
        *,
        symbols: Optional[Iterable[str]] = None,
        timeframes: Iterable[str] = TIMEFRAMES,
        inventory: Optional[Sequence[str]] = None,
    ) -> BuildReport:
        """Build every `(symbol, timeframe)` the store has evidence for.

        `symbols` restricts the build; `inventory` is the symbol universe that
        *should* exist (M4.1 `discover_symbols`), used only to decide the
        manifest's survivorship declaration (C-6.9). Neither changes a single
        stored row.

        The manifest is authoritative: a `.npy` left behind by an earlier build
        and no longer listed is invisible to the repository. Files are never
        deleted here — removing data is an explicit operator action, not a side
        effect of rebuilding.

        **Two failure bases, deliberately.** `BuildError` means the evidence
        could not be turned into a canonical dataset (a format this parser does
        not know, an invariant violated, two artifacts disagreeing) — the remedy
        is here. `AcquisitionError` (raised by `RawStore.verify`) means the
        evidence itself is untrustworthy — the remedy is in the raw tier:
        `discard` the artifact and re-acquire it. Collapsing both into one type
        would hide which of the two tiers has to be fixed.

        **The manifest is the commit marker**, mirroring the raw tier's sidecar
        discipline. Datasets are written as they are built — holding a
        full-universe build in memory is not possible — so a build that fails
        part-way can leave `.npy` files on disk, but it writes no manifest. The
        repository reads only what the manifest lists, so a partial build is not
        a partial repository: it is no repository at all, and re-running the
        build overwrites those files deterministically.
        """
        root = Path(root)
        wanted_tf = tuple(timeframes)
        unknown = [tf for tf in wanted_tf if tf not in INTERVAL_MS]
        if unknown:
            raise BuildError(f"unknown timeframe(s) {unknown}; expected {TIMEFRAMES}")

        groups = self._group(symbols=symbols, timeframes=wanted_tf)
        builds: list[DatasetBuild] = []
        empty: list[str] = []
        for key in sorted(groups):
            built = self._build_dataset(root, key[0], key[1], groups[key])
            if built is None:
                empty.append(f"{key[0]}|{key[1]}")
            else:
                builds.append(built)

        return self._write_metadata(root, tuple(builds), tuple(empty), inventory)

    # -- one dataset -------------------------------------------------------

    def _build_dataset(
        self, root: Path, symbol: str, timeframe: str,
        records: Sequence[ArtifactRecord],
    ) -> Optional[DatasetBuild]:
        """Parse, merge, validate and store one dataset. `None` when it has no
        rows at all — the repository forbids recording an empty dataset (I-R3),
        and an all-empty symbol is a real answer for a delisted pair."""
        ordered = sorted(records, key=lambda r: (r.coverage_start_ms, r.artifact_path))
        pieces: list[np.ndarray] = []
        dropped = 0
        for record in ordered:
            rows = self._read_artifact(record)
            check_ordering(rows, source=record.artifact_path)
            self._check_coverage(rows, record)
            rows, unfinished = drop_unfinished(
                rows, timeframe=timeframe, observed_ms=observation_ms(record)
            )
            dropped += unfinished
            if len(rows):
                pieces.append(rows)

        merged, collapsed = _merge(pieces, where=f"{symbol}|{timeframe}")
        report = validate_dataset(merged, symbol=symbol, timeframe=timeframe)
        if report.rows == 0:
            return None

        _write_array(dataset_path(root, symbol, timeframe), merged)
        return DatasetBuild(
            symbol=symbol,
            timeframe=timeframe,
            rows=report.rows,
            first_open_ms=report.first_open_ms,
            last_open_ms=report.last_open_ms,
            gaps=report.gaps,
            raw_sha256=tuple(r.sha256 for r in ordered),
            source_artifacts=tuple(r.artifact_path for r in ordered),
            dropped_unfinished=dropped,
            duplicates_collapsed=collapsed,
        )

    def _read_artifact(self, record: ArtifactRecord) -> np.ndarray:
        """One artifact's rows, whatever wire format it holds."""
        if self._verify:
            self._store.verify(record.artifact_path)
        path = self._store.path_for(record.artifact_path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SourceUnusable(f"{record.artifact_path}: cannot read: {exc}") from exc

        if record.kind == "rest_json":
            return parse_kline_json(data, source=record.artifact_path)
        if record.kind in ("monthly_zip", "daily_zip"):
            return parse_kline_csv(
                self._csv_member(data, record), source=record.artifact_path
            )
        raise SourceUnusable(
            f"{record.artifact_path}: kind {record.kind!r} is not a kline source"
        )

    @staticmethod
    def _csv_member(data: bytes, record: ArtifactRecord) -> str:
        member = expected_member_for(record.artifact_path)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                raw = archive.read(member)
        except KeyError as exc:
            raise SourceUnusable(
                f"{record.artifact_path}: archive has no member {member!r}"
            ) from exc
        except zipfile.BadZipFile as exc:
            raise SourceUnusable(
                f"{record.artifact_path}: not a readable ZIP: {exc}"
            ) from exc
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceUnusable(
                f"{record.artifact_path}: member {member!r} is not UTF-8: {exc}"
            ) from exc

    @staticmethod
    def _check_coverage(rows: np.ndarray, record: ArtifactRecord) -> None:
        """Every row lies inside the half-open period the artifact claims.

        M4.1 derives coverage from the source URL without reading the bytes, on
        purpose (the raw tier records evidence it cannot interpret). This is the
        first moment the claim can be checked against the content, so it is
        checked here.
        """
        if len(rows) == 0:
            return
        times = rows["timestamp"]
        outside = np.nonzero(
            (times < record.coverage_start_ms) | (times >= record.coverage_end_ms)
        )[0]
        if outside.size:
            index = int(outside[0])
            raise CoverageMismatch(
                f"{record.artifact_path}: row {index} has open time "
                f"{int(times[index])}, outside the declared coverage "
                f"[{record.coverage_start_ms}, {record.coverage_end_ms})"
            )

    # -- grouping ----------------------------------------------------------

    def _group(
        self, *, symbols: Optional[Iterable[str]], timeframes: Sequence[str]
    ) -> dict[tuple[str, str], list[ArtifactRecord]]:
        """Committed kline artifacts, grouped by `(symbol, timeframe)`.

        Discovery payloads carry no symbol and are not kline sources; they are
        evidence for the universe, which is a later milestone's concern.
        """
        wanted = None if symbols is None else set(symbols)
        groups: dict[tuple[str, str], list[ArtifactRecord]] = {}
        for record in self._store.records():
            if record.kind == "discovery_json" or not record.symbol:
                continue
            if record.timeframe not in timeframes:
                continue
            if wanted is not None and record.symbol not in wanted:
                continue
            groups.setdefault((record.symbol, record.timeframe), []).append(record)
        return groups

    # -- metadata ----------------------------------------------------------

    def _write_metadata(
        self, root: Path, builds: tuple[DatasetBuild, ...],
        empty: tuple[str, ...], inventory: Optional[Sequence[str]],
    ) -> BuildReport:
        datasets = {b.key: b.manifest_entry() for b in builds}
        snapshot_id = compute_snapshot_id(
            builder_version=self._version, datasets=datasets
        )
        incomplete, missing, note = _survivorship(builds, inventory)

        manifest = {
            "format_version": FORMAT_VERSION,
            "builder_version": self._version,
            "data_snapshot_id": snapshot_id,
            "datasets": datasets,
            "survivorship_incomplete": incomplete,
            "survivorship_note": note,
            "missing_symbols": list(missing),
        }
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / MANIFEST_NAME, manifest)
        _write_json(root / GAP_INVENTORY_NAME, {
            "builder_version": self._version,
            "data_snapshot_id": snapshot_id,
            "gaps": {
                b.key: [gap.to_dict() for gap in b.gaps]
                for b in builds if b.gaps
            },
        })
        return BuildReport(
            root=root,
            data_snapshot_id=snapshot_id,
            builder_version=self._version,
            datasets=builds,
            empty=empty,
            survivorship_incomplete=incomplete,
            missing_symbols=missing,
        )


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def _merge(pieces: Sequence[np.ndarray], *, where: str) -> tuple[np.ndarray, int]:
    """Combine per-artifact rows into one ascending series.

    Overlap is normal: a day can be covered by a REST call made before its
    archive existed *and* by the monthly archive published later, and M4.1
    deliberately keeps both (they are separate evidence). Merging is therefore a
    real operation, not a concatenation:

    - each artifact is already verified ascending (`check_ordering`), so the
      stable sort here orders *artifacts* relative to each other and never
      reorders rows inside one;
    - rows sharing an open time must be **identical**, and collapse to one;
      a disagreement raises `ConflictingCandles` rather than picking a winner.

    Deduplication is on `open_time` and nothing else (C-6.3 item 6).
    """
    if not pieces:
        return np.empty(0, dtype=KLINE_DTYPE), 0
    combined = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
    order = np.argsort(combined["timestamp"], kind="stable")
    combined = combined[order]

    if len(combined) < 2:
        return np.ascontiguousarray(combined), 0

    same = combined["timestamp"][1:] == combined["timestamp"][:-1]
    if same.any():
        differing = np.nonzero(same & (combined[1:] != combined[:-1]))[0]
        if differing.size:
            index = int(differing[0])
            raise ConflictingCandles(
                f"{where}: two artifacts disagree about open time "
                f"{int(combined['timestamp'][index])}: {combined[index]!r} vs "
                f"{combined[index + 1]!r}. Raw storage is immutable — discard the "
                f"wrong artifact explicitly, never merge a preference."
            )

    keep = np.ones(len(combined), dtype=bool)
    keep[1:] = ~same
    collapsed = int((~keep).sum())
    return np.ascontiguousarray(combined[keep]), collapsed


def _survivorship(
    builds: Sequence[DatasetBuild], inventory: Optional[Sequence[str]]
) -> tuple[bool, tuple[str, ...], str]:
    """The manifest's survivorship declaration (C-6.9).

    C-6.9 requires the manifest to declare `survivorship_incomplete` when the
    inventory is known to be incomplete. An undeclared inventory cannot be known
    to be complete, so the default is `true`: a bias flag that defaults to
    "clean" would be a flag that silently lies on the day someone forgets it.
    """
    if inventory is None:
        return True, (), (
            "no symbol inventory was declared to the builder, so completeness "
            "is unverified; pass the M4.1 discovery result to establish it"
        )
    built = {b.symbol for b in builds if b.timeframe == "1h"}
    missing = tuple(sorted(set(inventory) - built))
    if missing:
        return True, missing, (
            f"{len(missing)} inventory symbol(s) have no 1h dataset; the "
            f"universe reconstruction of C-6.9 cannot rank them"
        )
    return False, (), (
        "every symbol in the declared inventory has a 1h dataset"
    )


# ---------------------------------------------------------------------------
# Atomic, deterministic writes
# ---------------------------------------------------------------------------

def _write_array(path: Path, rows: np.ndarray) -> None:
    """Write one dataset as `.npy`, atomically.

    `np.save` is handed an open file object rather than a path: given a path it
    would append `.npy` to the temporary name and defeat the rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        with open(tmp, "wb") as handle:
            np.save(handle, rows, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_json(path: Path, payload: Mapping) -> None:
    """Deterministic JSON: sorted keys, fixed indent, trailing newline."""
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        with open(tmp, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
