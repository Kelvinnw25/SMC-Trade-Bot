"""Test-side bridge: V2 analysis result -> harness `ObservableBehavior`.

This lives in the test layer, never in `v2/`. Runtime analysis must not import
`tests/regression` (Rule A8; purity denylist), so the Signal -> ObservableBehavior
translation is done here, where importing both sides is legitimate. This is the
"bridge, proven, not wired into runtime" the milestone calls for.

It runs the V2 pipeline to a terminal context (so it can read every observable
the harness needs — bull/bear/direction_source/veto/OB selection live on the
domain objects, not on the Signal alone), applies the M5 re-score under V1's
exact gate, then maps to `ObservableBehavior` field-for-field so the existing
comparison engine can diff it against `V1Oracle`.
"""
from __future__ import annotations

from typing import Any, Optional

from tests.regression.observable import ObservableBehavior
from tests.regression.v1_projection import (
    _alert_rank,
    _alert_tier,
    _levels_valid,
)
from v2.analysis.context import AnalysisContext
from v2.analysis.order_block import selected_block
from v2.analysis.pipeline import AnalysisPipeline
from v2.analysis.scoring import enrich_with_m5
from v2.domain import MarketSnapshot, Signal, SignalCandidate

_M5_SCORE_GATE = 8
_PERSISTED_DECISIONS = {"WATCH", "VALID", "A+"}


def v2_observable(
    snapshot: MarketSnapshot,
    *,
    session_active: bool = False,
    prior: Optional[dict] = None,
) -> ObservableBehavior:
    """Produce the V2 `ObservableBehavior` for `snapshot`.

    `prior`/kill are out of analysis scope (lifecycle), so `kill_triggered` is
    left None — matching the harness when no prior is supplied.
    """
    context = AnalysisContext.initial(snapshot, session_active=session_active)
    context = AnalysisPipeline.default().run(context)
    signal = context.signal
    assert signal is not None

    if signal.score >= _M5_SCORE_GATE and snapshot.m5 is not None:
        signal = enrich_with_m5(signal, snapshot.m5, session_active=session_active)

    return _to_observable(snapshot, context, signal)


def _to_observable(
    snapshot: MarketSnapshot, context: AnalysisContext, signal: Signal
) -> ObservableBehavior:
    htf = context.htf
    candidate = signal.candidate

    # V1 publishes the OB audit and the flat entry/sl/tp only on the scored path
    # (after `result.update`); the not-ready / veto / early exits do not.
    scored = candidate is not None and (
        candidate.entry.entry_ready or candidate.entry.entry_mode == "WATCH_ONLY"
    )

    entry_price = candidate.entry.entry_price if candidate is not None else 0.0
    sl = candidate.risk.sl if candidate is not None else 0.0
    tp = candidate.risk.tp if scored else None
    tp_source = candidate.risk.tp_source if candidate is not None else None
    tp_rr = candidate.risk.tp_rr if candidate is not None else None
    rr = candidate.risk.rr if candidate is not None else None
    atr_last = float(candidate.atr_last) if candidate is not None else 0.0

    entry_mode = _entry_mode_observable(signal, candidate)
    entry_source = candidate.entry.entry_source if candidate is not None else None

    selected = selected_block(candidate.order_blocks) if scored else None
    ob_count = len(candidate.order_blocks) if scored and candidate is not None else 0

    decision = signal.decision
    alert_tier = _alert_tier(decision)

    persistence_payload = _persistence_payload(
        snapshot, signal, decision, entry_mode, entry_price, sl, tp, entry_source
    )
    notification = _notification_payload(
        snapshot, signal, htf, decision, entry_mode, entry_price, sl, tp,
        entry_source, tp_source, alert_tier, persistence_payload is not None,
    )

    return ObservableBehavior(
        symbol=snapshot.symbol,
        h1_last_ts=float(snapshot.h1_last_ts),
        live_price=float(snapshot.live_price),
        d1_rows=int(len(snapshot.d1)),
        h1_rows=int(len(snapshot.h1)),
        m5_rows=(int(len(snapshot.m5)) if snapshot.m5 is not None else None),
        atr_last=atr_last,
        weekly_bias=htf.weekly_bias if htf else None,
        d1_bias=htf.d1_bias if htf else None,
        h4_bias=htf.h4_bias if htf else None,
        location=htf.location if htf else None,
        regime=context.regime.label if context.regime is not None else None,
        direction=htf.direction if htf else None,
        direction_source=htf.direction_source if htf else None,
        bull=round(float(htf.bull), 1) if htf else None,
        bear=round(float(htf.bear), 1) if htf else None,
        anchor_override=htf.anchor_override if htf else None,
        veto_reason=htf.veto_reason if htf else None,
        sweep_valid=candidate.sweep_valid if candidate is not None else None,
        displacement_valid=candidate.displacement_valid if candidate is not None else None,
        fvg_valid=candidate.fvg_valid if candidate is not None else None,
        fresh_ob=candidate.fresh_ob if candidate is not None else None,
        m5_choch=signal.m5_choch,
        ob_selected_index=selected.index if selected is not None else None,
        ob_selected_top=selected.top if selected is not None else None,
        ob_selected_bottom=selected.bottom if selected is not None else None,
        ob_candidate_count=ob_count,
        entry_mode=entry_mode,
        entry_ready=candidate.entry.entry_ready if candidate is not None else None,
        entry_source=entry_source,
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        tp_source=tp_source,
        tp_rr=tp_rr,
        rr=rr,
        score=int(signal.score),
        decision=decision,
        reason=signal.reason,
        notes=tuple(signal.notes),
        alert_tier=alert_tier,
        alert_rank=_alert_rank(alert_tier),
        kill_triggered=None,
        persistence_payload=persistence_payload,
        notification=notification,
    )


def _entry_mode_observable(
    signal: Signal, candidate: Optional[SignalCandidate]
) -> Optional[str]:
    """Match V1's `result.get("entry_mode") or h1_setup.get("entry_mode")`.

    Early exits with no h1_setup (invalid_input / invalid_htf_map / no_direction)
    publish no entry_mode; veto publishes "NONE"; everything else the mode.
    """
    if candidate is not None:
        return candidate.entry.entry_mode
    if signal.reason in {"invalid_input", "invalid_htf_map", "no_direction"}:
        return None
    return signal.entry_mode  # veto -> "NONE"


def _persistence_payload(
    snapshot: MarketSnapshot,
    signal: Signal,
    decision: str,
    entry_mode: Optional[str],
    entry_price: float,
    sl: float,
    tp: Optional[float],
    entry_source: Any,
) -> Optional[dict]:
    """Reproduce the oracle's fresh-signal persistence projection (oracle.py)."""
    if decision.upper() not in _PERSISTED_DECISIONS:
        return None
    if not _levels_valid(entry_price, sl, tp):
        return None
    mode = entry_mode or "RETRACEMENT"
    return {
        "direction": str(signal.direction if signal.direction is not None else "NONE"),
        "score": int(signal.score),
        "decision": decision,
        "entry_price": entry_price,
        "sl": sl,
        "tp": tp,
        "regime": str(signal.regime),
        "miss_count": 0,
        "status": "WATCH",
        "alert_sent_tier": "",
        "last_miss_h1_ts": float(snapshot.h1_last_ts),
        "last_ob_audit_h1_ts": 0.0,
        "entry_mode": mode,
        "entry_source": str(entry_source if entry_source is not None else ""),
        "signal_id": f"{snapshot.symbol}|{mode}|{snapshot.h1_last_ts}",
    }


def _notification_payload(
    snapshot: MarketSnapshot,
    signal: Signal,
    htf: Any,
    decision: str,
    entry_mode: Optional[str],
    entry_price: float,
    sl: float,
    tp: Optional[float],
    entry_source: Any,
    tp_source: Any,
    alert_tier: str,
    would_persist: bool,
) -> Optional[dict]:
    """Reproduce the oracle's fresh-signal notification projection (oracle.py).

    The oracle sources the alert's `htf` dict from `debug["htf"]`, which carries
    biases under short keys; this rebuilds the same mapping from HTFContext.
    """
    if not (would_persist and alert_tier):
        return None
    htf_debug = {
        "weekly": htf.weekly_bias,
        "d1": htf.d1_bias,
        "h4": htf.h4_bias,
        "loc": htf.location,
        "regime": signal.regime,
        "h1_fb": htf.fallback_h1_bias,
        "src": htf.direction_source,
        "dir": htf.direction,
        "bull": round(float(htf.bull), 1),
        "bear": round(float(htf.bear), 1),
        "anchor_override": htf.anchor_override,
    }
    return {
        "kind": "new",
        "symbol": snapshot.symbol,
        "decision": decision,
        "direction": str(signal.direction if signal.direction is not None else "NONE"),
        "score": int(signal.score),
        "entry_price": entry_price,
        "sl": sl,
        "tp": tp,
        "regime": str(signal.regime),
        "entry_mode": entry_mode or "RETRACEMENT",
        "htf": {str(k): htf_debug[k] for k in htf_debug},
        # On the persisted (scored) path V1's `result` always carries these keys
        # (from `result.update`), so its `str(result.get(k, "-"))` stringifies the
        # value — and a continuation's None becomes the literal "None", never "-".
        "entry_source": str(entry_source),
        "tp_source": str(tp_source),
        "volume_spike": bool(signal.candidate.volume_spike) if signal.candidate else False,
    }
