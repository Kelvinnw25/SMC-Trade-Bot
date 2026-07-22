"""RiskPlan — the SL/TP/RR for a setup.

Architecture ref: 02-software-architecture.md §2 (RiskPlan).
V1 behavior ref: ``find_liquidity_target`` (tp/source/rr) + the 2R fallback
branches in the entry engine; ``rr`` is the value ``score_signal`` computes
against the hard floor (1.2) from the STRUCTURAL entry (Contract B,
Decision #001).

Exact V1 ``tp_source`` values: "EQH_cluster", "EQL_cluster", "SWING_HIGH",
"SWING_LOW", "PDH", "PDL", "fallback_2r" (and None when no entry).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RiskPlan:
    """Produced from EntryPlan + liquidity targeting; consumed by the RR gate.

    tp_rr: the RR reported by liquidity targeting (rounded to 2 in V1).
    rr:    the scoring-side RR (reward/risk from structural entry) used
           against the 1.2 hard floor; None when levels are absent.
    """

    sl: float
    tp: float
    tp_source: Optional[str] = None
    tp_rr: Optional[float] = None
    rr: Optional[float] = None
