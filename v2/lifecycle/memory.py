"""MemoryView — the read-only snapshot the reconciler reasons over.

Architecture ref: 02-software-architecture.md §8 (MemoryView: "read-only
snapshot of current ActiveSignals keyed by symbol … Immutable view; reconciler
cannot mutate it directly."). The store hands one of these to the reconciler;
the reconciler reads it and returns a LifecycleDecision — it never writes back.

Also carries the killed-thesis store (Decision #010), which the reconciler
consults for the KILL re-entry cooldown before any insert.
"""
from __future__ import annotations

from typing import Mapping, Optional

from v2.domain import ActiveSignal, KilledThesisRecord


class MemoryView:
    """An immutable read view of the active-signal + killed-thesis memory.

    Copies of the store's dicts are taken at construction, so a later mutation
    of the store cannot leak into a view already handed out. Iteration order
    preserves the store's insertion order — the same order V1's ``min(...)``
    weakest-slot selection depended on for tie-breaking.
    """

    __slots__ = ("_active", "_killed")

    def __init__(
        self,
        active: Mapping[str, ActiveSignal],
        killed: Mapping[str, KilledThesisRecord],
    ) -> None:
        self._active: dict[str, ActiveSignal] = dict(active)
        self._killed: dict[str, KilledThesisRecord] = dict(killed)

    @property
    def active(self) -> Mapping[str, ActiveSignal]:
        return self._active

    @property
    def killed(self) -> Mapping[str, KilledThesisRecord]:
        return self._killed

    def get(self, symbol: str) -> Optional[ActiveSignal]:
        return self._active.get(symbol)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._active

    def __len__(self) -> int:
        return len(self._active)

    def killed_ts(self, thesis_key: str) -> float:
        """The kill timestamp for a thesis key, or 0.0 if absent (V1's
        ``killed_thesis.get(key, 0.0)``)."""
        record = self._killed.get(thesis_key)
        return float(record.killed_ts) if record is not None else 0.0

    def weakest_symbol(self) -> Optional[str]:
        """The lowest-scoring symbol (V1 ``min(active, key=score)``); None when
        empty. Ties resolve to the first inserted, matching Python's ``min``
        over an insertion-ordered dict — exactly V1's behavior."""
        if not self._active:
            return None
        return min(self._active, key=lambda s: self._active[s].score)
