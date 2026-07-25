"""Shared builders for the lifecycle tests.

A ``CycleSpec`` describes one symbol's fresh analysis + market for one cycle,
abstractly enough to project BOTH:

- a V2 ``Signal`` (+ the fresh ``HTFContext``) that the reconciler consumes; and
- the equivalent V1 ``result`` / ``context`` dicts that the ``V1LifecycleReference``
  (a transcription of ``main.py``'s memory engine) consumes.

Driving both sides from one spec is what makes the equivalence replay honest:
neither side is fed a projection derived from the other's output.

The projections reproduce V1's exact per-path ``result`` shape (verified against
``src/strategy.py`` early exits and ``result.update``): the scored path carries
entry/sl/tp; the not-ready / veto / no_direction / invalid paths do not (or only
partially), which is the source of several lifecycle quirks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from v2.domain import (
    EntryPlan,
    FairValueGap,
    HTFContext,
    LiquiditySweep,
    Regime,
    RiskPlan,
    Signal,
    SignalCandidate,
)

_MODES = {"CONTINUATION", "RETRACEMENT", "WATCH_ONLY"}


@dataclass(frozen=True)
class CycleSpec:
    """One symbol-cycle of fresh analysis + market inputs."""

    symbol: str
    decision: str = "SKIP"
    direction: Optional[str] = None
    entry_mode: str = "NONE"
    regime: str = "unknown"
    score: int = 0
    entry_ready: bool = False
    entry_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    entry_source: str = "none"
    tp_source: Optional[str] = None
    volume_spike: bool = False
    reason: Optional[str] = None
    # fresh HTF reading
    weekly_bias: str = "CHOPPY"
    d1_bias: str = "CHOPPY"
    h4_bias: str = "CHOPPY"
    location: str = "equilibrium"
    has_htf: bool = True
    veto: bool = False
    veto_reason: str = "weekly_bearish_expansion_down_long_veto"
    atr_last: float = 0.0
    live_price: float = 0.0
    h1_last_ts: float = 0.0

    @property
    def scored(self) -> bool:
        """Whether V1 ran ``result.update`` (entry/sl/tp present in result)."""
        return self.entry_ready or self.entry_mode == "WATCH_ONLY"

    @property
    def is_candidate_path(self) -> bool:
        return (
            self.has_htf
            and self.direction is not None
            and not self.veto
            and self.entry_mode in _MODES
        )


def build_htf(spec: CycleSpec) -> Optional[HTFContext]:
    if not spec.has_htf:
        return None
    return HTFContext(
        pdh=0.0,
        pdl=0.0,
        daily_eq=0.0,
        weekly_bias=spec.weekly_bias,
        d1_bias=spec.d1_bias,
        h4_bias=spec.h4_bias,
        location=spec.location,
        current_price=spec.live_price,
        momentum_weakening=False,
        bull=0.0,
        bear=0.0,
        direction_source="none",
        anchor_override=False,
        veto_active=spec.veto,
        direction=spec.direction,
        veto_reason=spec.veto_reason if spec.veto else None,
    )


def make_signal(spec: CycleSpec) -> Tuple[Signal, Optional[HTFContext]]:
    """Project the spec into a V2 Signal (+ fresh HTFContext)."""
    htf = build_htf(spec)

    if not spec.has_htf:
        return (
            Signal(
                symbol=spec.symbol,
                score=0,
                decision="SKIP",
                entry_mode="NONE",
                reason="invalid_input",
                regime="unknown",
            ),
            None,
        )

    if spec.direction is None:
        return (
            Signal(
                symbol=spec.symbol,
                score=0,
                decision="SKIP",
                entry_mode="NONE",
                reason="no_direction",
                regime=spec.regime,
            ),
            htf,
        )

    if spec.veto:
        return (
            Signal(
                symbol=spec.symbol,
                score=0,
                decision="SKIP",
                entry_mode="NONE",
                direction=spec.direction,
                reason=spec.veto_reason,
                regime=spec.regime,
            ),
            htf,
        )

    # candidate path (not-ready SKIP or a scored result)
    assert htf is not None
    regime = Regime(label=spec.regime, entry_mode=spec.entry_mode)
    entry = EntryPlan(
        entry_mode=spec.entry_mode,
        entry_price=spec.entry_price,
        entry_ready=spec.entry_ready,
        entry_source=spec.entry_source,
        entry_reason=spec.reason or "no_entry",
    )
    risk = RiskPlan(
        sl=spec.sl,
        tp=spec.tp,
        tp_source=spec.tp_source,
        tp_rr=None,
        rr=None,
    )
    candidate = SignalCandidate(
        htf=htf,
        regime=regime,
        entry=entry,
        risk=risk,
        sweep=LiquiditySweep(detected=False),
        fvg=FairValueGap(valid=False),
        swing_highs=(),
        swing_lows=(),
        order_blocks=(),
        sweep_valid=False,
        fvg_valid=False,
        displacement_valid=False,
        fresh_ob=False,
        volume_spike=spec.volume_spike,
        target_clear=False,
        session_active=False,
        htf_context_clean=False,
        atr_last=spec.atr_last,
    )
    scored = spec.scored
    signal = Signal(
        symbol=spec.symbol,
        score=int(spec.score) if scored else 0,
        decision=spec.decision if scored else "SKIP",
        entry_mode=spec.entry_mode,
        direction=spec.direction,
        regime=spec.regime,
        reason=spec.reason,
        candidate=candidate,
    )
    return signal, htf


def make_v1_result(spec: CycleSpec) -> Dict[str, Any]:
    """The V1 ``result`` dict for this spec (shapes verified vs strategy.py)."""
    if not spec.has_htf:
        return {"score": 0, "decision": "SKIP", "reason": "invalid_input"}

    if spec.direction is None:
        return {"score": 0, "decision": "SKIP", "reason": "no_direction"}

    if spec.veto:
        return {
            "score": 0,
            "decision": "SKIP",
            "reason": spec.veto_reason,
            "direction": spec.direction,
            "regime": spec.regime,
            "entry_mode": "NONE",
        }

    if not spec.scored:
        # not-ready exit (strategy.py:1716-1730): no entry_price/sl/tp keys.
        return {
            "score": 0,
            "decision": "SKIP",
            "reason": spec.reason,
            "direction": spec.direction,
            "regime": spec.regime,
            "entry_mode": spec.entry_mode,
        }

    # scored path: score_signal return + build_signal_context result.update.
    result: Dict[str, Any] = {
        "score": int(spec.score),
        "decision": spec.decision,
        "direction": spec.direction,
        "notes": [],
        "regime": spec.regime,
        "rr": None,
        "entry_mode": spec.entry_mode,
        "volume_spike": spec.volume_spike,
        "entry_source": spec.entry_source,
        "tp_source": spec.tp_source if spec.tp_source is not None else "",
        "tp_rr": None,
        "entry_price": spec.entry_price,
        "sl": spec.sl,
        "tp": spec.tp,
        "entry_ready": spec.entry_ready,
    }
    if spec.reason is not None:
        result["reason"] = spec.reason
    return result


def make_v1_context(spec: CycleSpec, result: Dict[str, Any]) -> Dict[str, Any]:
    """The V1 ``context`` dict as ``is_poi_invalidated`` reads it (market_price,
    result, htf_map biases, h1_setup.atr_last).

    h1_setup exists only once the pipeline passes direction selection; its
    ``atr_last`` is the real ATR on a candidate path but hard-coded 0.0 on the
    veto path (strategy.py:1295), and absent entirely on no_direction / invalid.
    """
    htf_map: Dict[str, Any] = {}
    h1_setup: Dict[str, Any] = {}
    if spec.has_htf:
        htf_map = {"d1_bias": spec.d1_bias, "h4_bias": spec.h4_bias}
    if spec.has_htf and spec.direction is not None:
        # veto or candidate path -> h1_setup present
        atr = 0.0 if spec.veto else spec.atr_last
        h1_setup = {"atr_last": atr}
    return {
        "market_price": spec.live_price,
        "result": result,
        "htf_map": htf_map,
        "h1_setup": h1_setup,
    }


# ---------------------------------------------------------------------------
# Normalizers for comparing V2 and reference outputs.
# ---------------------------------------------------------------------------

def active_as_v1(active: Any) -> Dict[str, Dict[str, Any]]:
    """Serialize a ``{symbol: ActiveSignal}`` mapping to V1 dict form."""
    return {symbol: sig.to_v1_dict() for symbol, sig in active.items()}


def normalize_alert(alert: Any) -> Tuple:
    """A comparable tuple of an alert's decision-relevant fields (V2 Alert)."""
    return (
        str(alert.kind),
        str(alert.symbol),
        str(alert.direction),
        round(float(alert.entry_price), 8),
        round(float(alert.sl), 8),
        round(float(alert.tp), 8),
        str(alert.dedup_key or ""),
        str(alert.new_decision) if alert.new_decision is not None else "",
        str(alert.old_decision) if alert.old_decision is not None else "",
    )


def normalize_ref_alert(a: Dict[str, Any]) -> Tuple:
    """The same tuple shape from a reference alert record dict."""
    return (
        str(a["kind"]),
        str(a["symbol"]),
        str(a["direction"]),
        round(float(a["entry_price"]), 8),
        round(float(a["sl"]), 8),
        round(float(a["tp"]), 8),
        str(a.get("dedup_key", "") or ""),
        str(a.get("new_decision", "") or ""),
        str(a.get("old_decision", "") or ""),
    )
