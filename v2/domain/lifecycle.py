"""Lifecycle vocabulary: LifecycleState, mutations, Alert, LogEvent, LifecycleDecision.

Architecture refs: 02-software-architecture.md §2 (Alert, LifecycleDecision,
LifecycleState), §7 (state machine), §8 (Alert / LogEvent / LifecycleDecision
contracts). This module defines VOCABULARY ONLY — the rules that produce these
objects are Phase 4 (lifecycle.reconciler / lifecycle.rules / lifecycle.store).

V1 behavior refs: the three memory-engine branches, kill switch, same-thesis
refresh, kill-cooldown gate, slot management, and alert dispatch in main.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Any, Optional, Tuple, Union

from v2.domain.active_signal import ActiveSignal
from v2.domain.killed_thesis import KilledThesisRecord


@unique
class LifecycleState(Enum):
    """Runtime state of an ActiveSignal (Arch §7). Never persisted — distinct
    from the persisted ``status`` string ("WATCH"/"MANUAL_ACTION")."""

    ABSENT = "ABSENT"
    DISCOVERED = "DISCOVERED"
    ACTIVE = "ACTIVE"
    DECAYING = "DECAYING"
    EVICTED = "EVICTED"


#: LogEvent types (Arch §8 set + SAME_THESIS / KILL_COOLDOWN per proposed
#: Decision #010 — both verified V1 log emissions).
LOG_EVENT_TYPES = frozenset({
    "SCAN", "KILL", "STALE", "GUARD", "DECAY", "TTL", "REPLACE", "CAPPED",
    "ALERTED", "SAME_THESIS", "KILL_COOLDOWN",
})

#: Alert kinds (Arch §8): V1's send_signal_alert / send_upgrade_alert /
#: send_kill_alert.
ALERT_KINDS = frozenset({"new", "upgrade", "kill"})

#: Eviction reasons observed in V1 (Arch §7 eviction transitions).
EVICTION_REASONS = frozenset({"kill", "decay", "ttl", "replace"})


@dataclass(frozen=True)
class LogEvent:
    """An instruction to emit one operator log line (lifecycle → delivery).

    ``details`` is an ordered tuple of (name, value) pairs — kept generic in
    Phase 1; Phase 4 populates the exact fields per event type (e.g. DECAY
    carries from_decision / mode / reason / miss_count, whose sources are
    intentionally mixed per Decision #009).
    """

    event_type: str
    symbol: Optional[str] = None
    details: Tuple[Tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Alert:
    """An instruction to deliver an outbound notification (Arch §2/§8).

    Produced by Lifecycle as a decision; consumed by Delivery as an
    instruction. Lifecycle never sends. The payload fields mirror the exact
    kwargs of V1's telegram_notifier senders; kind-specific fields are
    optional and unused for other kinds:
      - kind "new":     decision, score, regime, entry_mode, htf,
                        entry_source, tp_source, volume_spike
      - kind "upgrade": old_decision, new_decision, old_score, new_score,
                        regime, entry_mode, entry_source, tp_source,
                        volume_spike
      - kind "kill":    reason
    ``dedup_key`` carries the tier-rank dedup semantics of V1's
    ``_alert_tier``/``_alert_rank`` ("" < "VALID" < "A+").
    ``htf`` is the V1 htf debug summary as ordered (name, value) pairs.
    """

    kind: str
    symbol: str
    direction: str
    entry_price: float
    sl: float
    tp: float
    dedup_key: str = ""
    decision: Optional[str] = None
    score: Optional[float] = None
    regime: Optional[str] = None
    entry_mode: Optional[str] = None
    entry_source: Optional[str] = None
    tp_source: Optional[str] = None
    volume_spike: Optional[bool] = None
    htf: Tuple[Tuple[str, Any], ...] = ()
    old_decision: Optional[str] = None
    new_decision: Optional[str] = None
    old_score: Optional[float] = None
    new_score: Optional[float] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Memory mutations (Arch §8 LifecycleDecision: "ordered set of memory
# mutations"). One dataclass per mutation type; V1 sources noted inline.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InsertMutation:
    """Insert a new ActiveSignal (V1: slot-available insert, or same-symbol
    payload replacement)."""

    symbol: str
    record: ActiveSignal


@dataclass(frozen=True)
class ReplaceMutation:
    """Evict the weakest signal and insert a stronger one (V1 [🔄 REPLACE])."""

    target_symbol: str
    symbol: str
    record: ActiveSignal


@dataclass(frozen=True)
class PartialRefreshMutation:
    """V1 same-thesis in-place refresh (main.py [🟰 SAME THESIS] branch):
    score = max(old, new); decision never downgraded; entry/sl/tp/regime
    updated; timestamp reset to now (TTL restart); miss_count and
    last_miss_h1_ts UNCHANGED. Fields carry the resulting values to apply —
    the max/no-downgrade decisions belong to the Phase 4 reconciler."""

    symbol: str
    score: float
    decision: str
    entry_price: float
    sl: float
    tp: float
    regime: str
    timestamp: float


@dataclass(frozen=True)
class IncrementMissMutation:
    """H1-gated miss increment (Decision #006): apply only when a new H1 has
    closed since last_miss_h1_ts; sets last_miss_h1_ts to h1_last_ts."""

    symbol: str
    h1_last_ts: float


@dataclass(frozen=True)
class EvictMutation:
    """Remove a signal from memory. ``reason`` is one of EVICTION_REASONS."""

    symbol: str
    reason: str


Mutation = Union[
    InsertMutation,
    ReplaceMutation,
    PartialRefreshMutation,
    IncrementMissMutation,
    EvictMutation,
]


@dataclass(frozen=True)
class LifecycleDecision:
    """The complete, declarative outcome of reconciling one Signal against
    memory (Arch §2/§8): mutations to apply, Alerts to dispatch, log events to
    emit, and killed-thesis records to store (V1 writes the thesis on KILL).

    Fully declarative — describes WHAT to change, not HOW. Applied atomically
    by the Phase 4 store; routed by the Phase 5 orchestrator.
    """

    mutations: Tuple[Mutation, ...] = ()
    alerts: Tuple[Alert, ...] = ()
    log_events: Tuple[LogEvent, ...] = ()
    killed_theses: Tuple[KilledThesisRecord, ...] = ()
