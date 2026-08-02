"""Immutable raw artifact storage — downloaded bytes are evidence.

REPLAY_CONTRACT.md C-6.10 splits the data layer into an **immutable raw tier**
(exactly what was downloaded, plus a SHA-256 per artifact) and a canonical tier
derived from it. This module is the raw tier.

## The one rule

A committed artifact is never modified, rewritten, or repaired in place. If its
bytes are wrong, it is *explicitly* discarded and re-acquired — never patched.
From the downloader's perspective this store is append-only.

## Commit protocol (why a crash cannot corrupt the store)

An artifact is committed in two atomic steps:

1. bytes  -> `<name>.part` -> fsync -> `os.replace` to `<name>`
2. sidecar -> `<name>.meta.json.part` -> fsync -> `os.replace` to `<name>.meta.json`

**The sidecar is the commit marker.** An artifact without its sidecar is not
committed, no matter what bytes are on disk. So:

- crash during (1)  -> a `.part` file, no artifact, no sidecar -> re-download.
- crash between (1) and (2) -> artifact bytes present, no sidecar -> treated as
  *not committed*, and re-downloading overwrites bytes that were never evidence.
- crash during (2)  -> a `.part` sidecar -> same as above.

There is no window in which a committed artifact is half-written.

## What this module does NOT do

It never parses a kline, never builds a dataset, never orders symbols by anything
but name, and never imports the repository (`backtest.data`) — the raw tier and
the canonical tier are separate by construction, and M4.2 is what bridges them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "AcquisitionError",
    "ArchiveCorrupt",
    "ArchiveIncomplete",
    "ArtifactNameMismatch",
    "ArtifactRecord",
    "ChecksumMismatch",
    "CoverageGap",
    "DuplicateArtifact",
    "IntegrityError",
    "MANIFEST_NAME",
    "RAW_SCHEMA_VERSION",
    "RawStore",
    "TruncatedArtifact",
    "expected_member_for",
    "sha256_hex",
]

#: Bumped only if the sidecar schema changes incompatibly.
RAW_SCHEMA_VERSION = 1

#: The derived, deterministic view over the sidecars (Task 6).
MANIFEST_NAME = "raw_manifest.json"

_PART_SUFFIX = ".part"
_META_SUFFIX = ".meta.json"


# ---------------------------------------------------------------------------
# Failures — one dedicated type per integrity condition (Task 5)
# ---------------------------------------------------------------------------

class AcquisitionError(Exception):
    """Base for every failure raised by the acquisition layer."""


class IntegrityError(AcquisitionError):
    """Base for every artifact-integrity failure. Catching this means
    'the bytes on disk cannot be trusted as evidence'."""


class ChecksumMismatch(IntegrityError):
    """The artifact's SHA-256 does not match the expected value — either the
    checksum Binance published, or the one recorded when it was committed."""


class ArchiveCorrupt(IntegrityError):
    """The bytes are not a readable ZIP, or a member fails its CRC."""


class ArchiveIncomplete(IntegrityError):
    """The ZIP is readable but does not contain the member it must contain."""


class ArtifactNameMismatch(IntegrityError):
    """The archive's member name is not the name the source URL implies."""


class TruncatedArtifact(IntegrityError):
    """The artifact is empty, or shorter than the length recorded for it."""


class DuplicateArtifact(IntegrityError):
    """A commit was attempted over an already-committed artifact whose bytes
    differ. Raw storage is immutable: this is never resolved by overwriting."""


class StorageTransient(AcquisitionError):
    """A filesystem operation failed for a reason that may not persist.

    **Deliberately not an `IntegrityError`.** A locked or briefly-unavailable file
    is not corrupt bytes, and conflating the two would let a genuine checksum
    failure be reported as a filesystem hiccup — or, worse, let an operator read a
    real corruption as noise. The two are retried the same way but mean opposite
    things, and only one of them says the evidence cannot be trusted.

    Why this type exists at all: `os.replace` raises `PermissionError` on Windows
    when a scanner, indexer or backup agent holds the destination open. That is an
    `OSError`, not an `AcquisitionError`, so it escaped `_retry`'s catch tuple,
    escaped `acquire()`, escaped `run()`, and terminated the entire acquisition —
    losing a multi-hour job to a condition that clears in milliseconds. The
    probability scales with file-creation rate, which is exactly what the parallel
    downloader raises.

    A *persistent* failure (a full disk, a genuinely revoked permission) still
    exhausts its retries and fails its item. Retrying does not make it succeed; it
    only ensures a transient cause is not mistaken for a permanent one.
    """


class CoverageGap(AcquisitionError):
    """A period that acquisition was asked to cover has no committed artifact."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The sidecar record (Task 6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactRecord:
    """What one committed artifact is, and where it came from.

    This is the *raw acquisition* manifest entry. It is deliberately not the
    replay manifest and not the repository manifest: it describes bytes and their
    provenance, never datasets or windows.

    `coverage_start_ms` / `coverage_end_ms` are the half-open period the artifact
    was *requested* to cover, taken from the source URL or REST query. They are
    not derived from the file's contents — parsing klines is M4.2's job, and the
    raw tier must be able to record evidence it cannot interpret.
    """

    schema_version: int
    downloader_version: str
    downloaded_at: str            # ISO-8601 UTC, second precision
    source_url: str
    kind: str                     # monthly_zip | daily_zip | rest_json | discovery_json
    symbol: str                   # "" for artifacts that are not symbol-scoped
    timeframe: str                # "" likewise
    coverage_start_ms: int
    coverage_end_ms: int
    sha256: str
    size_bytes: int
    checksum_source: str          # "binance" (published) | "computed" (ours)
    artifact_path: str            # POSIX-style, relative to the store root

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ArtifactRecord":
        known = {f for f in cls.__dataclass_fields__}      # type: ignore[attr-defined]
        missing = known - set(data)
        if missing:
            raise IntegrityError(
                f"sidecar is missing field(s) {sorted(missing)}"
            )
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class RawStore:
    """Immutable, append-only storage for downloaded artifacts.

    Layout — the artifact's path mirrors its source, so the mapping from a URL to
    a file is total and needs no lookup table::

        <root>/bulk/um/monthly/klines/<SYMBOL>/<TF>/<SYMBOL>-<TF>-<YYYY-MM>.zip
        <root>/bulk/um/daily/klines/<SYMBOL>/<TF>/<SYMBOL>-<TF>-<YYYY-MM-DD>.zip
        <root>/rest/<SYMBOL>/<TF>/<START_MS>-<END_MS>.json
        <root>/discovery/<ISO8601>-exchange_info.json
        <root>/discovery/<ISO8601>-archive_listing.json
        <root>/raw_manifest.json          (derived; see `write_manifest`)

    Every artifact carries a `<name>.meta.json` sidecar, which is what makes it
    committed. The store holds no mutable state beyond the filesystem: two
    instances over the same directory answer every question identically.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    # -- paths -------------------------------------------------------------

    def path_for(self, relative: str) -> Path:
        """The absolute path of an artifact identified by its store-relative
        POSIX path. Rejects anything that would escape the store."""
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise AcquisitionError(f"unsafe artifact path: {relative!r}")
        return self._root / rel

    def meta_path(self, relative: str) -> Path:
        path = self.path_for(relative)
        return path.with_name(path.name + _META_SUFFIX)

    # -- state -------------------------------------------------------------

    def is_committed(self, relative: str) -> bool:
        """True when both the artifact and its sidecar exist. The sidecar alone
        is the commit marker: bytes without one were never evidence."""
        return self.meta_path(relative).is_file() and self.path_for(relative).is_file()

    def record(self, relative: str) -> ArtifactRecord:
        """The sidecar of a committed artifact."""
        path = self.meta_path(relative)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise IntegrityError(f"no sidecar for {relative}") from exc
        except json.JSONDecodeError as exc:
            raise IntegrityError(f"malformed sidecar {path}: {exc}") from exc
        if data.get("schema_version") != RAW_SCHEMA_VERSION:
            raise IntegrityError(
                f"{path}: unsupported schema_version {data.get('schema_version')!r}"
            )
        return ArtifactRecord.from_dict(data)

    def records(self) -> tuple[ArtifactRecord, ...]:
        """Every committed artifact, ordered by artifact path.

        Sorted so that the manifest, and anything derived from it, is identical
        for identical storage regardless of filesystem enumeration order.
        """
        found: list[ArtifactRecord] = []
        for meta in self._root.rglob("*" + _META_SUFFIX):
            relative = meta.relative_to(self._root).as_posix()[: -len(_META_SUFFIX)]
            if self.path_for(relative).is_file():
                found.append(self.record(relative))
        return tuple(sorted(found, key=lambda r: r.artifact_path))

    # -- writing -----------------------------------------------------------

    def commit(
        self,
        relative: str,
        data: bytes,
        *,
        record: ArtifactRecord,
        expected_sha256: Optional[str] = None,
    ) -> ArtifactRecord:
        """Store `data` as an immutable artifact, or fail.

        Verifies before writing (`expected_sha256` when the source published one),
        then writes bytes and sidecar atomically. Re-committing byte-identical
        data is a no-op that returns the existing record — that is what makes a
        re-run of the same plan free rather than destructive. Re-committing
        *different* data raises `DuplicateArtifact`; the store never overwrites.
        """
        if not data:
            raise TruncatedArtifact(f"{relative}: refusing to commit empty bytes")

        digest = sha256_hex(data)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ChecksumMismatch(
                f"{relative}: sha256 {digest} does not match the published "
                f"checksum {expected_sha256}"
            )
        if record.sha256 != digest:
            raise ChecksumMismatch(
                f"{relative}: record declares sha256 {record.sha256} but the "
                f"bytes hash to {digest}"
            )
        if record.size_bytes != len(data):
            raise TruncatedArtifact(
                f"{relative}: record declares {record.size_bytes} bytes but "
                f"{len(data)} were supplied"
            )

        if self.is_committed(relative):
            existing = self.record(relative)
            if existing.sha256 == digest:
                return existing
            raise DuplicateArtifact(
                f"{relative} is already committed with sha256 {existing.sha256}; "
                f"refusing to overwrite it with {digest}. Raw storage is "
                f"immutable — discard the artifact explicitly to re-acquire it."
            )

        path = self.path_for(relative)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Same condition, one line earlier than the traced failure: a lock or
            # a briefly-unavailable path here would otherwise escape as a bare
            # OSError and kill the run, which is the whole defect this type fixes.
            raise StorageTransient(f"{relative}: {exc}") from exc
        _atomic_write(path, data)
        _atomic_write(
            self.meta_path(relative),
            (json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        return record

    def discard(self, relative: str) -> None:
        """Explicitly remove a committed artifact so it can be re-acquired.

        The only sanctioned way to get rid of bad evidence: the sidecar goes
        first, so an interrupted discard leaves an *uncommitted* artifact rather
        than a committed one with no sidecar.
        """
        self.meta_path(relative).unlink(missing_ok=True)
        self.path_for(relative).unlink(missing_ok=True)

    # -- verification (Task 5) --------------------------------------------

    def verify(self, relative: str) -> ArtifactRecord:
        """Re-verify a committed artifact against its sidecar.

        Checks, in order: the sidecar exists and parses; the artifact exists; its
        size matches; its SHA-256 matches; and, for a ZIP, that the archive opens,
        every member passes its CRC, and it contains exactly the CSV member its
        name implies.
        """
        rec = self.record(relative)
        path = self.path_for(relative)
        if not path.is_file():
            raise TruncatedArtifact(f"{relative}: sidecar exists but the artifact does not")
        data = path.read_bytes()
        if len(data) != rec.size_bytes:
            raise TruncatedArtifact(
                f"{relative}: {len(data)} bytes on disk, sidecar records "
                f"{rec.size_bytes}"
            )
        digest = sha256_hex(data)
        if digest != rec.sha256:
            raise ChecksumMismatch(
                f"{relative}: sha256 {digest} does not match the recorded "
                f"{rec.sha256}"
            )
        if relative.endswith(".zip"):
            verify_archive(data, expected_member=expected_member_for(relative))
        return rec

    def verify_all(self) -> tuple[ArtifactRecord, ...]:
        """Verify every committed artifact. Raises on the first failure, in
        deterministic (path) order, so a repeated run reports the same one."""
        checked = []
        for rec in self.records():
            checked.append(self.verify(rec.artifact_path))
        return tuple(checked)

    def verify_coverage(self, expected: Iterable[str]) -> None:
        """Every relative path in `expected` must be committed.

        `CoverageGap` names the missing periods rather than the count, so the
        report says which day is absent, not merely that one is.
        """
        missing = sorted(rel for rel in expected if not self.is_committed(rel))
        if missing:
            preview = ", ".join(missing[:10])
            more = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
            raise CoverageGap(f"{len(missing)} uncovered artifact(s): {preview}{more}")

    # -- manifest (Task 6) -------------------------------------------------

    def manifest(self) -> dict:
        """The raw-acquisition manifest: a deterministic view over the sidecars.

        Derived, never authoritative — the sidecars are the truth. Regenerating it
        from unchanged storage produces byte-identical JSON.
        """
        records = self.records()
        return {
            "schema_version": RAW_SCHEMA_VERSION,
            "artifact_count": len(records),
            "artifacts": [rec.to_dict() for rec in records],
        }

    def write_manifest(self) -> Path:
        """Write `manifest()` to `<root>/raw_manifest.json`, atomically."""
        path = self._root / MANIFEST_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.manifest(), indent=2, sort_keys=True) + "\n"
        _atomic_write(path, payload.encode("utf-8"))
        return path


# ---------------------------------------------------------------------------
# Archive integrity — structural only; kline content is M4.2's concern
# ---------------------------------------------------------------------------

def verify_archive(data: bytes, *, expected_member: str) -> None:
    """The bytes are a sound ZIP containing exactly `expected_member`.

    Structural verification only: `testzip` walks every member's CRC, which
    catches truncation and bit-rot without interpreting a single kline row.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if not names:
                raise ArchiveIncomplete("archive contains no members")
            bad = archive.testzip()
            if bad is not None:
                raise ArchiveCorrupt(f"CRC failure in member {bad!r}")
    except zipfile.BadZipFile as exc:
        raise ArchiveCorrupt(f"not a readable ZIP: {exc}") from exc

    if expected_member not in names:
        raise ArtifactNameMismatch(
            f"archive contains {names!r}, expected a member named "
            f"{expected_member!r}"
        )


def expected_member_for(archive_name: str) -> str:
    """The CSV a Binance kline archive must contain.

    The convention is invariant — `X.zip` holds `X.csv` — and this is the single
    place it is written down. The planner uses it to state what a candidate must
    contain; `RawStore.verify` uses it to re-check a stored artifact. One rule,
    one implementation.
    """
    return Path(archive_name).name[: -len(".zip")] + ".csv"


def parse_binance_checksum(text: str) -> str:
    """The SHA-256 out of Binance's `.CHECKSUM` sidecar (`"<sha256>  <file>"`)."""
    parts = text.split()
    if not parts or len(parts[0]) != 64:
        raise ChecksumMismatch(f"unreadable CHECKSUM payload: {text[:80]!r}")
    return parts[0].lower()


# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` via a temp file, fsync, and `os.replace`.

    `os.replace` is atomic on POSIX and on Windows, so a reader never observes a
    half-written file, and an interrupted write leaves only the temp file.

    The temp name carries the writing process's pid **and thread id**: two
    operators running the downloader over the same store would otherwise share one
    `.part` path and interleave their bytes into it, and two worker threads inside
    one process share a pid, so the pid alone stops being a discriminator the
    moment acquisition is concurrent. They still cannot corrupt a *committed*
    artifact — `os.replace` is atomic and `commit` refuses to overwrite — but
    without both identifiers they could corrupt each other's in-flight write.

    Today the plan gives every item a distinct path, so no two writers target one
    temp file anyway. The thread id is here because that disjointness is a
    property of the planner, and this function should not depend on it.

    Any `OSError` — from the open, the write, the fsync or the replace — is
    re-raised as `StorageTransient` so the downloader's existing retry can see it.
    The commit protocol is unchanged: the temp file is still removed on every
    failure path, and a caller that gives up leaves either nothing or an
    uncommitted artifact, never a committed one.
    """
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}{_PART_SUFFIX}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            # A temp file we cannot remove must not mask the failure that put us
            # here. It is inert: nothing reads a `.part`, and `records()` globs
            # only `*.meta.json`.
            pass
        if isinstance(exc, OSError):
            raise StorageTransient(f"{path.name}: {exc}") from exc
        raise
