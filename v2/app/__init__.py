"""V2 application layer — Phase 5: Orchestration & Delivery.

Architecture ref: 02-software-architecture.md §3 (`app.orchestrator`: "the
top-level loop — sequence the phases (fetch → analyze → reconcile → apply →
dispatch → persist), handle per-coin errors, sleep. The thin replacement for
``run()``'s outer structure, containing *coordination only*, no business
logic"), §4 (app may depend on all layers below), §10 Principle 12 (the
orchestrator holds no branch that changes a signal's outcome).

Phase 5.1 delivers the thin orchestrator. Delivery presentation — dashboard
rendering, terminal LogEvent output, and the retracement OB-audit JSONL sink —
is Phase 5.2.
"""
from v2.app.orchestrator import Orchestrator

__all__ = ["Orchestrator"]
