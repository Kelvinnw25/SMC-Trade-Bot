"""ActiveSignal — a Signal that lives in memory. The one persistable object.

Architecture ref: 02-software-architecture.md §2 (ActiveSignal), §8
(PersistenceRecord). Owned exclusively by the Lifecycle layer; never
constructed in Analysis.

V1 behavior refs (the serialization contract is V1's ACTUAL schema — the full
17-field shape, per the approved plan §4/§10 and doc priority #2):
- write shape: ``signal_payload`` construction in main.py (16 keys in literal
  order, then ``signal_id`` appended last before insertion);
- read shape: ``load_memory`` in main.py — required-field gate, type
  coercions, optional-field defaults, and its (intentional, lossy) dropping of
  ``entry_source`` and ``signal_id``.

Field-semantics notes locked by the Decision Log:
- ``miss_count`` (Decisions #006/#007): increments ONLY after a new H1 candle
  closes (H1-gated via ``last_miss_h1_ts``), and intentionally serves TWO
  consumers — decay eviction (>= MISS_COUNT_LIMIT) AND TTL tiering
  (``miss_count > 0`` as the tapped-proxy). One writer (lifecycle store);
  neither consumer recomputes tapped state.
- ``last_miss_h1_ts``: reset-to-current-H1 on full refresh (NOT to 0.0) —
  V1 sets it to ``h1_last_ts`` when a payload is (re)inserted.
- ``last_ob_audit_h1_ts``: persisted and carried over by V1 but never updated
  by the current audit path (vestigial; actual audit dedup is the in-memory
  ``ob_audit_seen`` set).
- ``status``: persisted string ("WATCH" / "MANUAL_ACTION") — distinct from the
  runtime LifecycleState enum.

Clock injection (Arch Principle 10): V1's ``load_memory`` defaults a missing
``timestamp`` to the current wall-clock time. The domain layer must not read the clock, so
``from_v1_dict`` takes an injectable ``default_timestamp``; the Phase 2
persistence adapter passes its clock's now() to reproduce V1 exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

#: V1 load_memory required fields — records missing any of these are invalid
#: (V1 silently skips them; here ``from_v1_dict`` raises ValueError and the
#: Phase 2 adapter reproduces the silent skip).
V1_REQUIRED_FIELDS = ("score", "decision", "entry_price", "sl", "tp", "direction")


@dataclass(frozen=True)
class ActiveSignal:
    """The Signal payload plus lifecycle metadata; persists to JSON.

    ``entry_source`` / ``signal_id``: ``None`` means ABSENT (the post-restart
    state, because V1's load drops both keys); ``""`` is a valid present value.
    ``to_v1_dict`` omits the keys entirely when None, matching what V1 writes
    after a restart.
    """

    direction: str
    score: float
    decision: str
    entry_price: float
    sl: float
    tp: float
    regime: str
    timestamp: float
    entry_mode: str
    miss_count: int = 0
    status: str = "WATCH"
    alert_sent_tier: str = ""
    alert_sent_ts: float = 0.0
    last_miss_h1_ts: float = 0.0
    last_ob_audit_h1_ts: float = 0.0
    entry_source: Optional[str] = None
    signal_id: Optional[str] = None

    def to_v1_dict(self) -> Dict[str, Any]:
        """Serialize to the exact V1 ``signal_payload`` shape and key order.

        Key order matches V1's payload construction (main.py): the 16 literal
        keys, then ``signal_id`` appended last. ``entry_source``/``signal_id``
        are omitted when None (V1 post-restart records lack both keys).
        Values are emitted as stored — no coercion — so an int score stays an
        int (fresh V1 payload) and a float score stays a float (post-restart),
        exactly like V1's JSON dump of its in-memory dict.
        """
        record: Dict[str, Any] = {
            "direction": self.direction,
            "score": self.score,
            "decision": self.decision,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "regime": self.regime,
            "timestamp": self.timestamp,
            "miss_count": self.miss_count,
            "status": self.status,
            "alert_sent_tier": self.alert_sent_tier,
            "alert_sent_ts": self.alert_sent_ts,
            "last_miss_h1_ts": self.last_miss_h1_ts,
            "last_ob_audit_h1_ts": self.last_ob_audit_h1_ts,
            "entry_mode": self.entry_mode,
        }
        if self.entry_source is not None:
            record["entry_source"] = self.entry_source
        if self.signal_id is not None:
            record["signal_id"] = self.signal_id
        return record

    @classmethod
    def from_v1_dict(
        cls,
        data: Dict[str, Any],
        *,
        default_timestamp: Optional[float] = None,
    ) -> "ActiveSignal":
        """Deserialize with V1 ``load_memory`` semantics, field for field.

        - Required-field gate: raises ValueError if any of
          ``V1_REQUIRED_FIELDS`` is missing or a coercion fails (V1 skips the
          record silently; the persistence adapter catches this to reproduce
          that).
        - Coercions and defaults are V1's exactly, including
          ``alert_sent_tier`` defaulting to the record's ``decision``.
        - ``entry_source`` and ``signal_id`` are DROPPED (set to None) even if
          present — V1's load does not read them back.
        - ``timestamp`` missing: V1 uses the wall clock; here the injectable
          ``default_timestamp`` is used instead (raises ValueError if the
          record has no timestamp and no default was provided).
        """
        if not all(k in data for k in V1_REQUIRED_FIELDS):
            missing = [k for k in V1_REQUIRED_FIELDS if k not in data]
            raise ValueError(f"missing required field(s): {missing}")

        if "timestamp" in data:
            timestamp = float(data["timestamp"])
        elif default_timestamp is not None:
            timestamp = float(default_timestamp)
        else:
            raise ValueError(
                "record has no 'timestamp' and no default_timestamp was "
                "injected (V1 defaults to the wall clock; the domain layer "
                "cannot read the clock — Architecture Principle 10)"
            )

        try:
            return cls(
                direction=str(data["direction"]),
                score=float(data["score"]),
                decision=str(data["decision"]),
                entry_price=float(data["entry_price"]),
                sl=float(data["sl"]),
                tp=float(data["tp"]),
                regime=str(data.get("regime", "unknown")),
                timestamp=timestamp,
                miss_count=int(data.get("miss_count", 0)),
                last_miss_h1_ts=float(data.get("last_miss_h1_ts", 0.0)),
                status=str(data.get("status", "WATCH")),
                last_ob_audit_h1_ts=float(data.get("last_ob_audit_h1_ts", 0.0)),
                entry_mode=str(data.get("entry_mode", "RETRACEMENT")),
                alert_sent_tier=str(
                    data.get("alert_sent_tier", data.get("decision", ""))
                ),
                alert_sent_ts=float(data.get("alert_sent_ts", 0.0)),
                entry_source=None,
                signal_id=None,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid field value: {exc}") from exc
