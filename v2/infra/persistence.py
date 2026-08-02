"""infra.persistence — the anti-corruption layer over V1's file storage.

Architecture ref: 02-software-architecture.md §3 (`infra.persistence`: "load
and atomically save the signal memory … validate schema on load with
optional-field defaults"), §6 (the mechanical serializer; `killed_thesis.json`
is a second store, Decision #010), §8 (PersistenceRecord). Roadmap Phase 2:
wrap V1's persistence *without changing the storage format*.

This adapter is the ONLY V2 entry point to durable storage; no other V2 module
reads or writes these files. It reproduces V1's file *mechanics* exactly —
atomic temp-file + `os.replace`, `indent=4` JSON, `ensure_ascii=False` JSONL,
missing-file-means-empty, the exact filenames — while delegating every
*record's* serialization to the Phase-1 domain contracts
(`ActiveSignal.to_v1_dict`/`from_v1_dict`, `KilledThesisRecord`,
`OBAuditRecord`), which were built and validated to match V1 byte-for-byte.

Why the file mechanics are reproduced rather than V1's functions called: V1's
`load_memory`/`save_memory`/… live in `main.py` with hardcoded relative paths,
so they cannot be pointed at an injected directory (untestable, and importing
`main.py` drags in the whole app). The storage *format* — the migration's
actual contract — is preserved exactly; only the path-bound plumbing is
re-expressed here so it is injectable and testable.

Error handling (per the Phase 2.3 brief): a missing file behaves exactly as V1
(empty result); an individual malformed record is skipped exactly as V1's
per-record `continue`; but a structurally malformed *file* (unparseable JSON, a
non-object top level) and any filesystem failure are translated into a
deterministic `PersistenceError` instead of being silently swallowed as V1
did — a deliberate, documented divergence in *error surfacing only* (the format
and missing-file semantics are unchanged). The orchestrator's load-failure
policy is decided later, in Phase 5.

This module knows nothing about trading logic; it only serializes what it is
given and reconstructs immutable domain objects.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from v2.domain import (
    ActiveSignal,
    KilledThesisRecord,
    OBAuditRecord,
    SignalLedgerRecord,
)

__all__ = ["PersistenceAdapter", "PersistenceError", "PersistencePort"]

# V1 filenames (config.ACTIVE_POIS_FILE / main.STATE_FILE, KILLED_THESIS_FILE,
# OB_AUDIT_FILE). Preserved exactly so existing V1 files load without migration.
_ACTIVE_POIS_FILE = "active_pois.json"
_KILLED_THESIS_FILE = "killed_thesis.json"
_OB_AUDIT_FILE = "retracement_ob_audit.jsonl"
# V2 evaluation dataset (Phase 7.1) — new, append-only; not a V1 file.
_SIGNAL_LEDGER_FILE = "signal_ledger.jsonl"


class PersistenceError(Exception):
    """A durable-storage operation failed or the file was structurally
    malformed. Raised in place of leaking an OSError / JSONDecodeError so
    consumers depend on a stable adapter-level failure type."""


@runtime_checkable
class PersistencePort(Protocol):
    """The durable-storage contract the scan loop depends on.

    Declared here, beside its production implementation, exactly as `Clock` and
    `Notifier` are — so a consumer depends on this Protocol and never on
    `PersistenceAdapter`. Six methods, because the orchestrator calls six: two
    loads at startup, two saves at end of cycle, and two append-only observability
    sinks. Nothing else belongs (there is deliberately no reader for either sink —
    Decision #010: observability records are never read back at runtime).

    Implementations must honour these invariants:

    - **Loads never fail soft into garbage.** Absent storage yields an empty dict;
      an individually malformed record is skipped; a structurally malformed store
      raises `PersistenceError`. The caller's policy for that error is its own
      (it starts empty).
    - `default_timestamp` is threaded into record reconstruction for the rare
      record lacking a timestamp, so this layer needs no clock of its own.
    - **Saves are atomic** from the caller's point of view: a save either replaces
      the stored state or leaves the previous state intact — never a partial write.
    - **Appends are append-only.** A written line is never rewritten or reordered.
    - Every method raises `PersistenceError` (or a subclass) and nothing else on
      failure; the caller catches exactly that type and continues best-effort.

    Implementations: `PersistenceAdapter` (live, below) is the only one today.
    """

    def load_active_signals(
        self, *, default_timestamp: Optional[float] = None
    ) -> dict[str, ActiveSignal]:
        """The persisted signal memory as `{symbol: ActiveSignal}`; `{}` if absent."""
        ...

    def load_killed_theses(self) -> dict[str, KilledThesisRecord]:
        """The killed-thesis cooldown store; `{}` if absent."""
        ...

    def save_active_signals(self, signals: Mapping[str, ActiveSignal]) -> None:
        """Replace the stored signal memory atomically."""
        ...

    def save_killed_theses(self, theses: Mapping[str, KilledThesisRecord]) -> None:
        """Replace the stored killed-thesis store atomically."""
        ...

    def append_ob_audit(self, record: OBAuditRecord) -> None:
        """Append one OB-audit line (write-only observability)."""
        ...

    def append_signal_ledger(self, record: SignalLedgerRecord) -> None:
        """Append one Signal Ledger line (write-only observability)."""
        ...


class PersistenceAdapter:
    """Loads and saves V2 signal memory in V1's exact on-disk format.

    Files live under an injected ``base_dir`` (which must already exist, as V1
    relied on the current working directory existing). Tests point this at a
    temporary directory; production points it at the configured data directory.
    """

    def __init__(self, base_dir: Path | str) -> None:
        base = Path(base_dir)
        self._active_pois_path = base / _ACTIVE_POIS_FILE
        self._killed_thesis_path = base / _KILLED_THESIS_FILE
        self._ob_audit_path = base / _OB_AUDIT_FILE
        self._signal_ledger_path = base / _SIGNAL_LEDGER_FILE

    # -- active signals -----------------------------------------------------

    def load_active_signals(
        self, *, default_timestamp: Optional[float] = None
    ) -> dict[str, ActiveSignal]:
        """Load the active-signal memory as ``{symbol: ActiveSignal}``.

        Missing file → empty dict (V1). Individual records that fail the V1
        required-field gate or coercion are skipped (V1's per-record
        ``continue``). ``default_timestamp`` is threaded to
        ``ActiveSignal.from_v1_dict`` for the rare record lacking a
        ``timestamp`` (V1 used the wall clock there); the caller — later, the
        orchestrator holding the clock — supplies it, keeping this adapter free
        of a clock dependency. When it is absent, such a record is skipped.
        """
        raw = self._read_json_object(self._active_pois_path)
        if raw is None:
            return {}

        signals: dict[str, ActiveSignal] = {}
        for symbol, data in raw.items():
            try:
                signals[str(symbol)] = ActiveSignal.from_v1_dict(
                    data, default_timestamp=default_timestamp
                )
            except (ValueError, TypeError):
                # Mirrors V1 load_memory's per-record `except ... continue`.
                continue
        return signals

    def save_active_signals(self, signals: Mapping[str, ActiveSignal]) -> None:
        """Atomically persist ``{symbol: ActiveSignal}`` in V1's format."""
        payload = {symbol: signal.to_v1_dict() for symbol, signal in signals.items()}
        self._atomic_write_json(self._active_pois_path, payload)

    # -- killed theses ------------------------------------------------------

    def load_killed_theses(self) -> dict[str, KilledThesisRecord]:
        """Load the killed-thesis cooldown store as
        ``{thesis_key: KilledThesisRecord}``. Missing file → empty dict (V1);
        an entry whose timestamp will not coerce to float is skipped."""
        raw = self._read_json_object(self._killed_thesis_path)
        if raw is None:
            return {}

        theses: dict[str, KilledThesisRecord] = {}
        for key, killed_ts in raw.items():
            try:
                theses[str(key)] = KilledThesisRecord.from_v1_item(key, killed_ts)
            except (ValueError, TypeError):
                continue
        return theses

    def save_killed_theses(
        self, theses: Mapping[str, KilledThesisRecord]
    ) -> None:
        """Atomically persist the killed-thesis store as V1's flat
        ``{thesis_key: killed_ts}`` dict."""
        payload = {key: record.killed_ts for key, record in theses.items()}
        self._atomic_write_json(self._killed_thesis_path, payload)

    # -- OB audit trail -----------------------------------------------------

    def append_ob_audit(self, record: OBAuditRecord) -> None:
        """Append one OB audit record as a JSON line (creates the file if
        missing, exactly as V1's append). Write-only observability — there is
        deliberately no read method (Decision #010: never read back)."""
        line = json.dumps(record.to_v1_dict(), ensure_ascii=False)
        try:
            with open(self._ob_audit_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise PersistenceError(
                f"failed appending to {self._ob_audit_path.name}: {exc}"
            ) from exc

    # -- signal ledger (Phase 7.1 evaluation dataset) -----------------------

    def append_signal_ledger(self, record: SignalLedgerRecord) -> None:
        """Append one Signal Ledger record as a JSON line (creates the file if
        missing). Append-only observability: one line per completed analysis,
        every decision tier. Never read back by the runtime — evaluation tooling
        reads it offline via ``SignalLedgerRecord.from_jsonl_dict``."""
        line = json.dumps(record.to_jsonl_dict(), ensure_ascii=False)
        try:
            with open(self._signal_ledger_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise PersistenceError(
                f"failed appending to {self._signal_ledger_path.name}: {exc}"
            ) from exc

    # -- file mechanics -----------------------------------------------------

    @staticmethod
    def _read_json_object(path: Path) -> Optional[dict[str, Any]]:
        """Read a JSON object from ``path``. Missing file → None (the caller
        maps that to an empty result, as V1 did). Unparseable JSON, a non-object
        top level, or an OS read error → ``PersistenceError``."""
        if not path.exists():
            return None
        try:
            with open(path, "r") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise PersistenceError(f"malformed JSON in {path.name}: {exc}") from exc
        except OSError as exc:
            raise PersistenceError(f"failed reading {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PersistenceError(
                f"malformed {path.name}: expected a JSON object, "
                f"got {type(raw).__name__}"
            )
        return raw

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        """Write ``payload`` as ``indent=4`` JSON via a temp file + atomic
        ``os.replace`` — V1's crash-safe pattern (``f"{file}.tmp"`` then
        replace). OS errors → ``PersistenceError``."""
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            with open(tmp_path, "w") as handle:
                json.dump(payload, handle, indent=4)
            os.replace(tmp_path, path)
        except OSError as exc:
            raise PersistenceError(f"failed writing {path.name}: {exc}") from exc
