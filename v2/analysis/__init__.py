"""V2 analysis layer — pure business logic (Layer 2).

Architecture ref: 02-software-architecture.md §4; 03-implementation-roadmap.md
Phase 3. This layer turns a `MarketSnapshot` into a `Signal` through an ordered
chain of small, single-responsibility stages.

**Purity is the defining constraint.** This layer may depend only on
`v2.domain`, `typing`, and the standard library. It must never touch the
filesystem, network, persistence, notification, the clock, `src/` (V1),
`v2.infra`, lifecycle state, or previous-signal state. That is enforced by
`tests/analysis/test_analysis_purity.py`, and it is what makes analysis
deterministic and therefore diffable against V1.

Structure:
- `context`  — `AnalysisContext`, the immutable state threaded through stages.
- `stage`    — the `AnalysisStage` contract every stage implements.
- `pipeline` — the ordered composition (and the single place order is declared).
- `engine`   — `V2Analyzer`, the public entry point; orchestration only.
- one module per stage: `htf`, `regime`, `structure`, `liquidity`,
  `order_block`, `fair_value_gap`, `entry`, `risk`, `scoring`.

Status: **Phase 3.0 — architecture skeleton.** Every stage raises
`NotImplementedError`; no trading behavior exists yet. Behavior is ported in a
later, separately-approved milestone and validated against V1 through the
regression harness.
"""
from v2.analysis.context import AnalysisContext
from v2.analysis.engine import AnalysisError, V2Analyzer
from v2.analysis.entry import EntryStage
from v2.analysis.fair_value_gap import FairValueGapStage
from v2.analysis.htf import HTFStage
from v2.analysis.liquidity import LiquidityStage
from v2.analysis.order_block import OrderBlockStage
from v2.analysis.pipeline import AnalysisPipeline, default_stages
from v2.analysis.regime import RegimeStage
from v2.analysis.risk import RiskStage
from v2.analysis.scoring import ScoringStage
from v2.analysis.stage import AnalysisStage, not_implemented
from v2.analysis.structure import StructureStage

__all__ = [
    "AnalysisContext",
    "AnalysisError",
    "AnalysisPipeline",
    "AnalysisStage",
    "EntryStage",
    "FairValueGapStage",
    "HTFStage",
    "LiquidityStage",
    "OrderBlockStage",
    "RegimeStage",
    "RiskStage",
    "ScoringStage",
    "StructureStage",
    "V2Analyzer",
    "default_stages",
    "not_implemented",
]
