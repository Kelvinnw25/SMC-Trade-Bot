"""V2 domain layer — Phase 1: Domain Model Foundation.

Immutable, dependency-free domain objects (02-software-architecture.md §2/§4;
03-implementation-roadmap.md Phase 1). This layer:

- depends on NOTHING in the application (no src/, no infra, no I/O, no clock);
- contains NO behavior — construction, equality, and serialization contracts
  only ("this phase produces no behavior — it produces the vocabulary");
- is immutable: every object is a frozen dataclass. The single documented
  exception is MarketSnapshot's pandas DataFrames (immutable by contract, see
  v2.domain.market).

KilledThesisRecord, OBAuditRecord, and the SAME_THESIS / KILL_COOLDOWN log
event types are user-approved extensions (proposed Decision #010) modeling
verified V1 behavior absent from the frozen architecture document.
"""
from v2.domain.active_signal import V1_REQUIRED_FIELDS, ActiveSignal
from v2.domain.entry import EntryPlan
from v2.domain.htf import HTFContext
from v2.domain.killed_thesis import KilledThesisRecord
from v2.domain.lifecycle import (
    ALERT_KINDS,
    EVICTION_REASONS,
    LOG_EVENT_TYPES,
    Alert,
    EvictMutation,
    IncrementMissMutation,
    InsertMutation,
    LifecycleDecision,
    LifecycleState,
    LogEvent,
    Mutation,
    PartialRefreshMutation,
    ReplaceMutation,
)
from v2.domain.market import MarketSnapshot
from v2.domain.ob_audit import OBAuditRecord
from v2.domain.regime import Regime
from v2.domain.risk import RiskPlan
from v2.domain.signal import Signal, SignalCandidate
from v2.domain.structure import FairValueGap, LiquiditySweep, OrderBlock, Swing

__all__ = [
    "ActiveSignal",
    "Alert",
    "ALERT_KINDS",
    "EntryPlan",
    "EVICTION_REASONS",
    "EvictMutation",
    "FairValueGap",
    "HTFContext",
    "IncrementMissMutation",
    "InsertMutation",
    "KilledThesisRecord",
    "LifecycleDecision",
    "LifecycleState",
    "LiquiditySweep",
    "LogEvent",
    "LOG_EVENT_TYPES",
    "MarketSnapshot",
    "Mutation",
    "OBAuditRecord",
    "OrderBlock",
    "PartialRefreshMutation",
    "Regime",
    "ReplaceMutation",
    "RiskPlan",
    "Signal",
    "SignalCandidate",
    "Swing",
    "V1_REQUIRED_FIELDS",
]
