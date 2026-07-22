"""SignalCandidate and Signal — the analysis layer's output objects.

Architecture ref: 02-software-architecture.md §2 (SignalCandidate, Signal),
§8 (Signal is the single analysis→lifecycle handoff, carrying the OrderBlock
candidate set + selection for auditability).

V1 behavior refs:
- SignalCandidate mirrors the ``h1_setup`` dict built in
  ``build_signal_context`` (confirmation flags + entry/risk data + atr_last),
  bundled with the typed HTFContext / Regime / structure objects. It replaces
  V1's sprawling ``context``/``h1_setup`` dicts. Boundary note (Arch §2): it is
  an internal analysis-layer concept, not a standard app-level contract.
- Signal mirrors the ``result`` dict: ``score_signal`` output, updated with
  entry/sl/tp/mode/source by ``build_signal_context``, and optionally with
  ``m5_choch``/``m5_reason`` by ``enrich_with_m5``. A SKIP Signal is still a
  valid object (it just won't enter memory). Early-exit SKIPs carry only
  score/decision/reason — hence the liberal defaults here.

Exact V1 decision strings: "SKIP" | "WATCH" | "VALID" | "A+".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from v2.domain.entry import EntryPlan
from v2.domain.htf import HTFContext
from v2.domain.regime import Regime
from v2.domain.risk import RiskPlan
from v2.domain.structure import FairValueGap, LiquiditySweep, OrderBlock, Swing


@dataclass(frozen=True)
class SignalCandidate:
    """A fully-analyzed setup BEFORE the decision tier is assigned.

    The boolean confirmation flags duplicate what the structure objects imply
    on purpose: the flags are the exact scoring inputs V1's ``h1_setup``
    carried, while the objects carry the audit detail (Arch §2 OrderBlock).
    """

    htf: HTFContext
    regime: Regime
    entry: EntryPlan
    risk: RiskPlan
    sweep: LiquiditySweep
    fvg: FairValueGap
    swing_highs: Tuple[Swing, ...]
    swing_lows: Tuple[Swing, ...]
    order_blocks: Tuple[OrderBlock, ...]
    # --- V1 h1_setup confirmation flags (exact names) ---
    sweep_valid: bool
    fvg_valid: bool
    displacement_valid: bool
    fresh_ob: bool
    volume_spike: bool
    target_clear: bool
    session_active: bool
    htf_context_clean: bool
    atr_last: float


@dataclass(frozen=True)
class Signal:
    """A SignalCandidate that has passed scoring and received a decision tier.

    Composition per Architecture §2 (Signal = "the candidate plus decision,
    notes, final score"): the embedded ``candidate`` is the SINGLE source of
    truth for entry/risk/structure/confirmation data. V1's flat ``result``
    keys (entry_price, sl, tp, entry_source, tp_source, tp_rr, rr,
    entry_ready, volume_spike) are NOT duplicated here — consumers read them
    from ``candidate.entry`` / ``candidate.risk`` / the candidate flags, per
    the §8 contract that assigns those fields to EntryPlan/RiskPlan. This is
    a structural representation choice only; no V1 behavior depends on
    Signal's shape (Signal is never persisted).

    Contains NO lifecycle metadata (Arch §8). ``candidate`` is None for
    early-exit SKIPs (invalid input / no direction / veto), exactly where V1
    returned a bare ``result`` without a full ``h1_setup``.

    ``direction`` / ``entry_mode`` / ``regime`` stay flat because early-exit
    Signals carry them in V1's ``result`` without any candidate. When a
    candidate IS present, construction enforces the consistency invariant
    (flat value == candidate value) and raises ValueError on mismatch, so the
    two representations can never disagree.
    """

    symbol: str
    score: int
    decision: str
    entry_mode: str
    direction: Optional[str] = None
    notes: Tuple[str, ...] = ()
    regime: str = "unknown"
    reason: Optional[str] = None
    m5_choch: Optional[bool] = None
    m5_reason: Optional[str] = None
    candidate: Optional[SignalCandidate] = None

    def __post_init__(self) -> None:
        if self.candidate is None:
            return
        mismatches = []
        if self.entry_mode != self.candidate.entry.entry_mode:
            mismatches.append(
                f"entry_mode {self.entry_mode!r} != "
                f"candidate.entry.entry_mode {self.candidate.entry.entry_mode!r}"
            )
        if self.regime != self.candidate.regime.label:
            mismatches.append(
                f"regime {self.regime!r} != "
                f"candidate.regime.label {self.candidate.regime.label!r}"
            )
        if self.direction != self.candidate.htf.direction:
            mismatches.append(
                f"direction {self.direction!r} != "
                f"candidate.htf.direction {self.candidate.htf.direction!r}"
            )
        if mismatches:
            raise ValueError(
                "Signal/candidate consistency invariant violated: "
                + "; ".join(mismatches)
            )
