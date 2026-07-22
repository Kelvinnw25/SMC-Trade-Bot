# Project Context

Project:
SMC Signal Radar V2

Mission:
Rebuild V1 with a completely new architecture.

Goal:
Preserve behavior while improving maintainability.

---

## Project Status

Status:
🟢 Documentation Frozen

Current Objective:
Implementation

Architecture Changes:
Not allowed unless explicitly approved.

Behavior Changes:
Not allowed.

---

Current Status:
✅ Legacy Documentation
✅ Software Architecture Specification
✅ Implementation Roadmap

These documents are frozen unless explicitly changed.

Implementation must follow them exactly.

---

## Current Phase

**Phase:** Implementation

**Current Sprint:** Phase 2 — Infrastructure Adapters (not started; awaiting approval to begin).

**Last completed milestone:** Milestone 1 — Phase 1 Domain Model Foundation (complete, independently reviewed, and approved 2026-07-22). Delivered `v2/domain/` with 18 immutable domain objects (16 spec + KilledThesisRecord and OBAuditRecord per Decision #010), serialization contracts validated against `active_pois.json`, `killed_thesis.json`, and `retracement_ob_audit.jsonl`, and 39 passing `unittest` tests. Documentation synchronized to the implementation on 2026-07-22 (Decision #010 added; Architecture §2/§6/§7/§8, Roadmap §0/§3, and Legacy Documentation updated).

**Blocking / open items:** None. Golden-dataset capture is scheduled as its own milestone before Phase 3 (Roadmap §0).

> Update instruction: every implementation chat MUST advance this section on completing a milestone or changing phase. A stale Current Phase misdirects the next chat and is worse than none. This is the only stateful section in the Project Context; keep it accurate.

---

## Document Priority

When two documents conflict, resolve in this order:

1. **Engineering Rules / Architectural Principles** — the non-negotiable structural laws.

2. **Legacy behavior (source of truth for WHAT the system does)** — if any document conflicts with V1's actual behavior, V1 behavior wins.

3. **Software Architecture Specification (source of truth for HOW the system is structured)** — governs module boundaries, dependencies, and architecture. Overrides Legacy structure, but never Legacy behavior.

4. **Implementation Roadmap** — sequencing and execution guidance.

5. **Decision Log** — records of specific architectural decisions.

**Why this order**

Behavior preservation is the primary objective of the migration.

Legacy defines the behavior.

Architecture defines the structure.

Engineering Rules define the constraints under which the structure may evolve.

---

## Scope

### Included

- Architecture migration
- Codebase restructuring
- Modularization
- Test infrastructure
- Golden dataset
- Comparison harness
- Dependency cleanup

### Not Included

- Strategy redesign
- New indicators
- New detectors
- New confirmations
- Scoring changes
- Threshold changes
- Calibration changes
- Lifecycle redesign
- Performance optimization

> Any change that alters what a signal is, or when it fires, is out of scope by definition.

---

## Implementation Philosophy

When trade-offs exist:

1. Behavior preservation takes precedence over cleaner code.

2. Correctness takes precedence over speed.

3. Incremental migration takes precedence over big-bang rewrites.