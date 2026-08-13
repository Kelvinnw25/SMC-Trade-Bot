"""The golden oracle — extract an `ObservableBehavior` from V1's own analysis.

`V1Oracle` is the reference the whole harness is built around. It does **not**
re-implement any analysis; it calls V1's existing, unmodified functions
(`SMCStrategy.build_signal_context` / `enrich_with_m5` / `is_poi_invalidated`)
on a `MarketSnapshot`, in the same order and under the same gates as V1's
`run()` loop, then reads the observable values back out of the `context` /
`result` / `debug` dicts those functions already return.

That V1 emits its intermediates in `context["debug"]` is what lets the harness
capture trend / structure / OB / FVG observables *without instrumenting or
modifying V1* — the "non-invasive capture hooks" the roadmap (§6) budgeted for
turn out to be unnecessary because the functions are pure and already return
everything.

`Analyzer` is the seam future milestones plug into: a `V2Analyzer` will consume
the identical `MarketSnapshot` and emit the identical `ObservableBehavior`; the
harness diffs the two. Right now only `V1Oracle` exists — running it against
itself is the deterministic regression baseline every V2 result is measured on.

Scope boundary (deliberate, so this harness stays out of Analysis/Lifecycle):
- It captures the **analysis-level** observables plus the single-cycle,
  state-independent *projection* of the persistence and notification payloads V1
  would emit for a fresh signal (the mapping in `run()` lines 528-545 / 678-692).
- Wall-clock fields V1 stamps at write time (`timestamp`, `alert_sent_ts`) are
  omitted — they are not analysis behavior and are validated separately with an
  injected clock in the lifecycle phase.
- Path-dependent lifecycle sequencing (miss_count progression, alert dedup /
  upgrade routing, TTL, replace, the OB-audit dedup set) is **not** replayed
  here; the alert *tier* that drives that routing is captured, and the lifecycle
  phase reuses this same comparison machinery for its own sequence replay.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Protocol, runtime_checkable

from src.strategy import SMCStrategy
from v2.domain.market import MarketSnapshot

from tests.regression.observable import ObservableBehavior

# M5 enrichment fires at V1's decision gate: score >= 8 (main.py:352).
_M5_SCORE_GATE = 8
_PERSISTED_DECISIONS = {"WATCH", "VALID", "A+"}


@runtime_checkable
class Analyzer(Protocol):
    """Turns identical market input into an observable outcome. Implemented by
    `V1Oracle` now and by a future `V2Analyzer`; the harness compares any two."""

    def analyze(
        self, snapshot: MarketSnapshot, *, prior: Optional[dict[str, Any]] = None
    ) -> ObservableBehavior:
        ...


class V1Oracle:
    """Reference `Analyzer` backed by V1's unmodified `SMCStrategy`."""

    def __init__(self, strategy: Optional[SMCStrategy] = None) -> None:
        self._strategy = strategy if strategy is not None else SMCStrategy()

    def analyze(
        self, snapshot: MarketSnapshot, *, prior: Optional[dict[str, Any]] = None
    ) -> ObservableBehavior:
        """Run V1's pipeline on `snapshot` and extract its observable outcome.

        `prior` is a previously-persisted signal dict (as stored in
        `active_pois.json`); when supplied, the kill-switch observable is
        evaluated against it. It never mutates state.
        """
        strategy = self._strategy

        context = strategy.build_signal_context(snapshot.d1, snapshot.h1)
        if not isinstance(context, dict):  # V1 always returns a dict; defensive.
            context = {"result": {"score": 0, "decision": "SKIP", "reason": "no_context"}}
        else:
            context = dict(context)

        # Live forming-candle price, exactly as run() injects it (main.py:307).
        context["market_price"] = float(snapshot.live_price)
        result = context.get("result", {}) or {}

        # Kill switch runs before M5 enrichment in V1 (main.py:316-347), using
        # the pre-enrichment context; M5 changes only score/decision, not the
        # HTF/atr inputs the switch reads, so the verdict is order-independent.
        kill_triggered: Optional[bool] = None
        if prior is not None:
            kill_triggered = bool(strategy.is_poi_invalidated(prior, context))

        # M5 enrichment: V1 requests M5 only at score >= 8 (main.py:352); the
        # snapshot already carries closed, hour-aligned M5 (or None) per the
        # market-data adapter's guards.
        if (
            isinstance(result, dict)
            and int(result.get("score", 0)) >= _M5_SCORE_GATE
            and snapshot.m5 is not None
        ):
            enriched = strategy.enrich_with_m5(context, snapshot.m5)
            if isinstance(enriched, dict):
                result = enriched
                context["result"] = result

        return self._extract(snapshot, context, result, kill_triggered)

    # -- extraction ---------------------------------------------------------

    def _extract(
        self,
        snapshot: MarketSnapshot,
        context: dict[str, Any],
        result: dict[str, Any],
        kill_triggered: Optional[bool],
    ) -> ObservableBehavior:
        debug = context.get("debug", {}) or {}
        htf = debug.get("htf", {}) or {}
        conf = debug.get("conf", {}) or {}
        ob = debug.get("ob_audit", {}) or {}
        veto = debug.get("veto", {}) or {}
        htf_map = context.get("htf_map", {}) or {}
        h1_setup = context.get("h1_setup", {}) or {}

        decision = str(result.get("decision", "SKIP"))
        entry_mode = result.get("entry_mode") or h1_setup.get("entry_mode")
        entry_mode = str(entry_mode).upper() if entry_mode is not None else None

        entry_price = _as_float(result.get("entry_price"), 0.0)
        sl = _as_float(result.get("sl"), 0.0)
        tp = _optional_float(result.get("tp"))
        score = int(result.get("score", 0))
        entry_source = result.get("entry_source", h1_setup.get("entry_source"))

        alert_tier = _alert_tier(decision)
        alert_rank = _alert_rank(alert_tier)

        persistence_payload = self._persistence_payload(
            snapshot, result, decision, entry_mode, entry_price, sl, tp, entry_source
        )
        notification = self._notification_payload(
            snapshot, result, h1_setup, htf, decision, entry_mode, entry_price, sl,
            tp, entry_source, alert_tier, persistence_payload is not None,
        )

        return ObservableBehavior(
            # market
            symbol=snapshot.symbol,
            h1_last_ts=float(snapshot.h1_last_ts),
            live_price=float(snapshot.live_price),
            d1_rows=int(len(snapshot.d1)),
            h1_rows=int(len(snapshot.h1)),
            m5_rows=(int(len(snapshot.m5)) if snapshot.m5 is not None else None),
            atr_last=_as_float(h1_setup.get("atr_last"), 0.0),
            # trend
            weekly_bias=_first_str(htf.get("weekly"), htf_map.get("weekly_bias")),
            d1_bias=_first_str(htf.get("d1"), htf_map.get("d1_bias")),
            h4_bias=_first_str(htf.get("h4"), htf_map.get("h4_bias")),
            location=_first_str(htf.get("loc"), htf_map.get("location")),
            regime=_first_str(result.get("regime"), htf.get("regime"), context.get("regime")),
            direction=_first_str(htf.get("dir"), result.get("direction")),
            direction_source=_optional_str(htf.get("src")),
            bull=_optional_float(htf.get("bull")),
            bear=_optional_float(htf.get("bear")),
            anchor_override=_optional_bool(htf.get("anchor_override")),
            veto_reason=_optional_str(veto.get("reason")),
            # structure / liquidity
            sweep_valid=_optional_bool(conf.get("sweep", h1_setup.get("sweep_valid"))),
            displacement_valid=_optional_bool(conf.get("disp", h1_setup.get("displacement_valid"))),
            fvg_valid=_optional_bool(conf.get("fvg", h1_setup.get("fvg_valid"))),
            fresh_ob=_optional_bool(conf.get("ob", h1_setup.get("fresh_ob"))),
            m5_choch=_optional_bool(result.get("m5_choch")),
            # order block
            ob_selected_index=_optional_int(ob.get("ob_index")),
            ob_selected_top=_optional_float(ob.get("ob_top")),
            ob_selected_bottom=_optional_float(ob.get("ob_bottom")),
            ob_candidate_count=int(len(ob.get("ob_candidates", []) or [])),
            # entry
            entry_mode=entry_mode,
            entry_ready=_optional_bool(result.get("entry_ready", h1_setup.get("entry_ready"))),
            entry_source=_optional_str(entry_source),
            entry_price=entry_price,
            # risk
            sl=sl,
            tp=tp,
            tp_source=_optional_str(result.get("tp_source")),
            tp_rr=_optional_float(result.get("tp_rr")),
            rr=_optional_float(result.get("rr")),
            # decision
            score=score,
            decision=decision,
            reason=_optional_str(result.get("reason")),
            notes=tuple(str(n) for n in (result.get("notes") or [])),
            # upgrade thesis
            alert_tier=alert_tier,
            alert_rank=alert_rank,
            # kill thesis
            kill_triggered=kill_triggered,
            # payloads
            persistence_payload=persistence_payload,
            notification=notification,
        )

    def _persistence_payload(
        self,
        snapshot: MarketSnapshot,
        result: dict[str, Any],
        decision: str,
        entry_mode: Optional[str],
        entry_price: float,
        sl: float,
        tp: Optional[float],
        entry_source: Any,
    ) -> Optional[dict[str, Any]]:
        """The state-independent fresh-signal record V1 would persist for this
        cycle (main.py:526-545,639-647), minus the wall-clock fields. Returns
        None when V1 would not persist (SKIP, or invalid levels)."""
        if decision.upper() not in _PERSISTED_DECISIONS:
            return None
        if not _levels_valid(entry_price, sl, tp):
            return None
        mode = entry_mode or "RETRACEMENT"
        return {
            "direction": str(result.get("direction", "NONE")),
            "score": int(result.get("score", 0)),
            "decision": decision,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "regime": str(result.get("regime", "unknown")),
            "miss_count": 0,
            "status": "WATCH",
            "alert_sent_tier": "",
            "last_miss_h1_ts": float(snapshot.h1_last_ts),
            "last_ob_audit_h1_ts": 0.0,
            "entry_mode": mode,
            "entry_source": str(result.get("entry_source", "")),
            "signal_id": f"{snapshot.symbol}|{mode}|{snapshot.h1_last_ts}",
        }

    def _notification_payload(
        self,
        snapshot: MarketSnapshot,
        result: dict[str, Any],
        h1_setup: dict[str, Any],
        htf: dict[str, Any],
        decision: str,
        entry_mode: Optional[str],
        entry_price: float,
        sl: float,
        tp: Optional[float],
        entry_source: Any,
        alert_tier: str,
        would_persist: bool,
    ) -> Optional[dict[str, Any]]:
        """The new-signal alert V1 would send for a fresh signal of this tier
        (main.py:675-692). Returns None when V1 would not alert (no tier — SKIP
        or WATCH — or the signal would not be persisted)."""
        if not (would_persist and alert_tier):
            return None
        return {
            "kind": "new",
            "symbol": snapshot.symbol,
            "decision": decision,
            "direction": str(result.get("direction", "NONE")),
            "score": int(result.get("score", 0)),
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "regime": str(result.get("regime", "unknown")),
            "entry_mode": entry_mode or "RETRACEMENT",
            "htf": {str(k): htf[k] for k in htf},
            "entry_source": str(result.get("entry_source", h1_setup.get("entry_source", "-"))),
            "tp_source": str(result.get("tp_source", h1_setup.get("tp_source", "-"))),
            "volume_spike": bool(h1_setup.get("volume_spike", False)),
        }


# -- V1 alert-tier lookup ---------------------------------------------------
# Trivial 3-value mapping reproduced from V1 main.py:32-50 (not analysis logic;
# pinned to V1's exact table by test_oracle). The lifecycle phase, which owns
# alert routing, re-verifies this against V1 when it ports the dispatch.

def _alert_tier(decision: str) -> str:
    d = str(decision).upper()
    if d == "VALID":
        return "VALID"
    if d == "A+":
        return "A+"
    return ""


def _alert_rank(tier: str) -> int:
    return {"": 0, "VALID": 1, "A+": 2}.get(str(tier).upper(), 0)


# -- coercion helpers -------------------------------------------------------

def _levels_valid(entry_price: float, sl: float, tp: Optional[float]) -> bool:
    """Reproduce V1's `levels_valid` gate (main.py:471-480)."""
    if tp is None:
        return False
    values = (entry_price, sl, tp)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return False
    return (
        entry_price > 0.0
        and sl > 0.0
        and tp > 0.0
        and entry_price != sl
        and entry_price != tp
    )


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _first_str(*values: Any) -> Optional[str]:
    """First non-None value as a string (used to prefer debug['htf'] over the
    htf_map fallback, which carry the same bias under different keys)."""
    for value in values:
        if value is not None:
            return str(value)
    return None
