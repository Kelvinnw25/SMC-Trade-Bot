"""SMC Signal Radar V2.

Behavior-preserving architectural migration of V1 (see SMC-BOT-DOCS/).
Layout mirrors the Software Architecture Specification module names:

- v2.domain     — Phase 1: immutable, dependency-free domain objects
- v2.infra      — Phase 2: infrastructure adapters (clock built; market_data, persistence, notifier pending)
- v2.analysis   — Phase 3: pure analysis layer (not yet built)
- v2.lifecycle  — Phase 4: stateful lifecycle core (not yet built)
- v2.app        — Phase 5: thin orchestrator (not yet built)
"""
