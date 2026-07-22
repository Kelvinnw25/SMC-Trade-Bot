"""KilledThesisRecord — the record of a killed thesis (re-entry cooldown).

Approved extension (plan §10, proposed Decision #010): verified V1 behavior
absent from the frozen Architecture domain-object list.

V1 behavior refs (main.py):
- On KILL, V1 records the current wall-clock time under the key
  ``f"{symbol}|{direction}|{entry_mode}"``.
- Before inserting an actionable signal, a matching thesis younger than
  ``KILL_COOLDOWN_SECONDS`` (3600 = 1 closed H1 candle) blocks insertion and
  emits ``[🚫 KILL COOLDOWN]``.
- The store is pruned each cycle (entries older than the cooldown) and
  persisted atomically to ``killed_thesis.json`` as a flat
  ``{thesis_key: killed_ts}`` dict.

The cooldown THRESHOLD is not defined here — it is an operational constant
owned by config/lifecycle in later phases; the domain object only carries the
data. Key construction uses V1's exact f-string: no casing or whitespace
transforms are applied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class KilledThesisRecord:
    """One killed thesis: produced by Lifecycle on KILL, consulted before insert."""

    thesis_key: str
    killed_ts: float

    @staticmethod
    def make_key(symbol: str, direction: str, entry_mode: str) -> str:
        """V1's exact key format: ``f"{symbol}|{direction}|{entry_mode}"``."""
        return f"{symbol}|{direction}|{entry_mode}"

    def to_v1_item(self) -> Tuple[str, float]:
        """One ``killed_thesis.json`` dict item: ``(thesis_key, killed_ts)``."""
        return (self.thesis_key, self.killed_ts)

    @classmethod
    def from_v1_item(cls, thesis_key: str, killed_ts: object) -> "KilledThesisRecord":
        """Build from one JSON dict item; V1 coerces the timestamp via float()."""
        return cls(thesis_key=str(thesis_key), killed_ts=float(killed_ts))  # type: ignore[arg-type]
