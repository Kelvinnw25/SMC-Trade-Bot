"""EntryPlan — the constructed entry for a setup (Contract B).

Architecture ref: 02-software-architecture.md §2 (EntryPlan), §6 (entry_source
becomes a typed field, not a magic-string convention).
Decision ref: 05-decision-log.md #001 (FROZEN) — Contract B:

    ``entry_price`` is a STRUCTURAL REFERENCE, not an executable order price.
    Any consumer computing risk/reward from ``entry_price`` MUST treat it as
    structural RR and MUST NOT substitute live price. Changing this is a
    behavior change and out of scope for V2.

V1 behavior ref: the entry engine inside ``build_signal_context``
(CONTINUATION ``_calc_continuation_entry`` closure; RETRACEMENT OB branch).

Exact V1 ``entry_source`` values observed in code: "none", "veto",
"continuation_micro_pullback", "no_continuation_displacement",
"no_micro_pullback", "retracement_ob". (In V1's CONTINUATION path
``entry_source`` is assigned from ``entry_reason``; both fields are kept so the
scoring gate keys off ``entry_source`` exactly as V1 does.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EntryPlan:
    """Produced by an entry engine; feeds RiskPlan and scoring.

    contract: the explicit Contract-B marker (Decision #001). "B" is the only
    valid value in V2; the field exists so no downstream consumer can
    misinterpret entry semantics.
    """

    entry_mode: str
    entry_price: float
    entry_ready: bool
    entry_source: str
    entry_reason: str
    displacement_index: Optional[int] = None
    pullback_index: Optional[int] = None
    contract: str = "B"
