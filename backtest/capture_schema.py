"""The lifecycle-capture wire format — C-15.3 and C-15.4, and nothing else.

## Why this is its own module

§10 fixes the dependency direction: `driver -> {adapter, capture} -> v2` and
`outcome, metrics -> data + v2.domain`. The Outcome Engine's trade builder
(C-9.2) **reads** what the capture layer **writes**, so both need the schema — but
`outcome` may not import `capture`. So the schema lives here, in a leaf that
imports nothing from either side (and nothing from `v2` at all): the writer
depends on it, the reader depends on it, and neither depends on the other.

This mirrors `backtest/lineage.py`, created in M8 for the same reason.

## What a captured event is

One JSON line per **emitted lifecycle event**, flattened from a
`LifecycleDecision` (C-15.3). The complete record of a mutation is
`mutation_payload`; `evict_reason` and `target_symbol` are convenience
projections of it, present because every reader of this file wants them and
none should have to know the shape of five mutation dataclasses to get them.

`schema_version` is 1. The rules of C-16.5 apply here as they do to the ledger:
never rewrite a written line, never repurpose a key, never reuse the version
number for a different field set.

## What it is not

Not a log. A rendered line is presentation, and C-15.5 is explicit that the
writer channel is a *validation* channel only, never a data source. This file is
the data source, and it is a schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = [
    "ALERTS_FILENAME",
    "ATTRIBUTIONS",
    "CAPTURE_SCHEMA_VERSION",
    "CapturedEvent",
    "EVENT_KINDS",
    "FIELD_ORDER",
    "KIND_ALERT",
    "KIND_KILLED_THESIS",
    "KIND_LOG",
    "KIND_MUTATION",
    "LIFECYCLE_EVENTS_FILENAME",
    "MEMORY_SNAPSHOTS_FILENAME",
    "MUTATION_TYPES",
    "MemorySnapshot",
    "PHASES",
    "PHASE_END_OF_CYCLE",
    "PHASE_SYMBOL",
    "read_events",
    "read_snapshots",
]

CAPTURE_SCHEMA_VERSION = 1

LIFECYCLE_EVENTS_FILENAME = "lifecycle_events.jsonl"
MEMORY_SNAPSHOTS_FILENAME = "memory_snapshots.jsonl"

#: C-15.7's cross-check channel. Declared here for the reason the module
#: docstring gives for the other two: `backtest.audit` reads it and
#: `backtest.replay.capture` writes it, and a reader that had to import the
#: writer to learn a filename would couple the audit to the thing it audits.
#: `capture` re-exports it, so every existing import keeps working.
ALERTS_FILENAME = "alerts.jsonl"

KIND_MUTATION = "MUTATION"
KIND_LOG = "LOG"
KIND_ALERT = "ALERT"
KIND_KILLED_THESIS = "KILLED_THESIS"

#: C-15.3 `kind`.
EVENT_KINDS = frozenset({KIND_MUTATION, KIND_LOG, KIND_ALERT, KIND_KILLED_THESIS})

PHASE_SYMBOL = "symbol"
PHASE_END_OF_CYCLE = "end_of_cycle"

#: C-15.3 `phase`. End-of-cycle events sort after every per-symbol event of the
#: same cycle (C-5.6.4).
PHASES = frozenset({PHASE_SYMBOL, PHASE_END_OF_CYCLE})

#: C-15.3 `attribution`. `inferred_ttl` is the C-15.5 residue: the store is not
#: injectable, so its evictions are attributed by elimination rather than
#: observed. The distinction is preserved in the data because a reader must be
#: able to tell a captured fact from a derived one.
ATTRIBUTIONS = frozenset({"reconciler", "inferred_ttl"})

#: C-15.3 `mutation_type` — the five members of the `Mutation` union
#: ([lifecycle.py:162-168](v2/domain/lifecycle.py#L162)).
MUTATION_TYPES = frozenset({
    "InsertMutation",
    "ReplaceMutation",
    "PartialRefreshMutation",
    "IncrementMissMutation",
    "EvictMutation",
})

#: The logical field order of C-15.3, for documentation and for the round-trip
#: test. Lines are emitted with `sort_keys=True` (as every other JSONL artifact
#: in this project is) because byte-determinism is the property that matters and
#: key order carries no meaning in JSON.
FIELD_ORDER = (
    "run_id", "source", "cycle_index", "scan_T", "h1_last_ts", "seq", "symbol",
    "universe_rank", "phase", "kind", "mutation_type", "mutation_payload",
    "evict_reason", "target_symbol", "log_event_type", "log_details",
    "alert_kind", "alert_tier", "alert_payload", "attribution",
    "schema_version",
)


@dataclass(frozen=True)
class CapturedEvent:
    """One line of `lifecycle_events.jsonl` (C-15.3).

    Immutable, like every record in this project: a captured fact is not
    editable. The ordering key is `(run_id, cycle_index, seq)`.
    """

    run_id: str
    source: str
    cycle_index: int
    scan_T: int
    h1_last_ts: float
    seq: int
    symbol: Optional[str]
    universe_rank: Optional[int]
    phase: str
    kind: str
    mutation_type: Optional[str] = None
    mutation_payload: Optional[dict] = None
    evict_reason: Optional[str] = None
    target_symbol: Optional[str] = None
    log_event_type: Optional[str] = None
    log_details: Optional[dict] = None
    alert_kind: Optional[str] = None
    alert_tier: Optional[str] = None
    alert_payload: Optional[dict] = None
    attribution: str = "reconciler"
    schema_version: int = CAPTURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in FIELD_ORDER}

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapturedEvent":
        """Tolerant in exactly the way C-16.5 prescribes: unknown keys are
        ignored (so an older reader survives a future field) and missing keys
        fall back to the dataclass defaults."""
        known = {name: data[name] for name in FIELD_ORDER if name in data}
        return cls(**known)  # type: ignore[arg-type]

    # -- projections a reader always wants ---------------------------------

    @property
    def order_key(self) -> tuple[str, int, int]:
        return (self.run_id, self.cycle_index, self.seq)

    @property
    def is_insertion(self) -> bool:
        """An insertion of an `ActiveSignal` — the mutation C-2.1 builds a trade
        from (either kind: a plain insert or the insert half of a REPLACE)."""
        return self.mutation_type in ("InsertMutation", "ReplaceMutation")

    @property
    def is_alert_bearing(self) -> bool:
        """A `new`/`upgrade` alert. A `kill` alert is not trade-instantiating —
        it tells the operator to leave, not to enter (C-2.1)."""
        return self.kind == KIND_ALERT and self.alert_kind in ("new", "upgrade")


@dataclass(frozen=True)
class MemorySnapshot:
    """One line of `memory_snapshots.jsonl` (C-15.4).

    `active` maps symbol to the `ActiveSignal.to_v1_dict()` form; `killed` maps
    thesis key to `killed_ts`. Recorded once per **processed** cycle, after the
    TTL sweep ([orchestrator.py:397](v2/app/orchestrator.py#L397)). A cycle with
    an empty universe produces no snapshot, and a reader MUST treat a missing
    snapshot as "unchanged" (C-5.6.5).
    """

    run_id: str
    source: str
    cycle_index: int
    scan_T: int
    active: dict[str, dict]
    killed: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "cycle_index": self.cycle_index,
            "scan_T": self.scan_T,
            "active": self.active,
            "killed": self.killed,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemorySnapshot":
        return cls(
            run_id=data["run_id"],
            source=data["source"],
            cycle_index=int(data["cycle_index"]),
            scan_T=int(data["scan_T"]),
            active=dict(data.get("active") or {}),
            killed={key: float(value)
                    for key, value in (data.get("killed") or {}).items()},
        )


# -- reading ----------------------------------------------------------------

def _lines(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_events(path: Path | str) -> tuple[CapturedEvent, ...]:
    """Every captured event, in file order.

    File order **is** `(run_id, cycle_index, seq)` order by construction, and the
    builder asserts that rather than re-sorting: a file that is not in order is a
    corrupt capture, and silently sorting it would hide the corruption.
    """
    return tuple(CapturedEvent.from_dict(row) for row in _lines(Path(path)))


def read_snapshots(path: Path | str) -> tuple[MemorySnapshot, ...]:
    return tuple(MemorySnapshot.from_dict(row) for row in _lines(Path(path)))
