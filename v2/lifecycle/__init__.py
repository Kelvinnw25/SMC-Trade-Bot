"""V2 lifecycle layer — Phase 4: Lifecycle Core.

Architecture ref: 02-software-architecture.md §3 (`lifecycle.reconciler`,
`lifecycle.store`, `lifecycle.rules`), §6 (single writer per state), §7 (the
signal lifecycle state machine), §10 (Principles 2/5/9/10). This layer is the
disciplined replacement for V1's monolithic ``run()`` memory engine.

Responsibilities OWNED here (and nowhere else):
- active-signal memory + killed-thesis cooldown store (``store``);
- the lifecycle predicates — kill / levels / same-thesis / TTL tiering / alert
  rank / H1 decay gate (``rules``, exactly one implementation each);
- the reconciliation of a fresh Signal against memory into a declarative
  ``LifecycleDecision`` (``reconciler``).

This layer is PURE state logic: no filesystem, no network, no wall clock (time
comes only through an injected ``infra.clock.Clock``), no delivery. It emits
``Alert`` / ``LogEvent`` objects; it never sends them. Persistence, the OB-audit
sink, the dashboard, and the scan loop are the orchestrator's job (Phase 5).

The lifecycle *vocabulary* (ActiveSignal, the mutations, Alert, LogEvent,
LifecycleDecision, KilledThesisRecord, LifecycleState) already lives in
``v2.domain``; this layer produces and consumes it.
"""
from v2.lifecycle.config import LifecycleConfig
from v2.lifecycle.memory import MemoryView
from v2.lifecycle.reconciler import Reconciler
from v2.lifecycle.store import SignalStore

__all__ = [
    "LifecycleConfig",
    "MemoryView",
    "Reconciler",
    "SignalStore",
]
