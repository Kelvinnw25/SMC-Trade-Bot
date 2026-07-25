"""lifecycle.rules — the pure lifecycle predicates.

Architecture ref: 02-software-architecture.md §3 (`lifecycle.rules`: "pure
predicates for lifecycle decisions — invalidation check, decay-gating (H1
timestamp comparison), TTL tiering (including the miss_count tap-proxy),
slot-replace eligibility, alert dedup rank. Pure functions, no state."), §7
(state machine transitions), §10 Principle 1/10 (pure, no wall-clock reads).

There is exactly ONE implementation of each rule here (milestone: "no
duplicated predicates, no duplicated kill logic, no duplicated cooldown logic").
The reconciler and store call these; they never re-derive a predicate.

Every function is a verbatim port of the V1 rule it reproduces, cited inline.
No I/O, no clock, no state, no numpy — V1's ``np.isfinite`` on scalars is
``math.isfinite``.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from v2.lifecycle.config import LifecycleConfig

__all__ = [
    "alert_tier",
    "alert_rank",
    "is_same_thesis",
    "levels_valid",
    "is_poi_invalidated",
    "ttl_for",
    "h1_gate_open",
]


# ---------------------------------------------------------------------------
# Alert tiering — V1 main.py:32-50 (verbatim; also pinned by the harness's
# test_oracle, reproduced here as the single lifecycle-owned implementation).
# ---------------------------------------------------------------------------

def alert_tier(decision: str) -> str:
    """The alert tier for a decision: only VALID / A+ carry a tier."""
    d = str(decision).upper()
    if d == "VALID":
        return "VALID"
    if d == "A+":
        return "A+"
    return ""


def alert_rank(tier: str) -> int:
    """Dedup rank: "" < VALID < A+ (main.py:32-38)."""
    return {"": 0, "VALID": 1, "A+": 2}.get(str(tier).upper(), 0)


# ---------------------------------------------------------------------------
# Same-thesis predicate — V1 main.py:249-253 (`_is_same_thesis`), verbatim.
# ---------------------------------------------------------------------------

def is_same_thesis(
    existing_direction: str,
    existing_entry_mode: str,
    direction: str,
    entry_mode: str,
) -> bool:
    """True when the existing signal shares direction AND entry_mode (upper)."""
    return (
        str(existing_direction).upper() == str(direction).upper()
        and str(existing_entry_mode).upper() == str(entry_mode).upper()
    )


# ---------------------------------------------------------------------------
# levels_valid — V1 main.py:471-480 (verbatim; np.isfinite -> math.isfinite).
# ---------------------------------------------------------------------------

def levels_valid(entry_price: float, sl: float, tp: float) -> bool:
    """The V1 admission gate: finite, positive, and entry distinct from sl/tp."""
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


# ---------------------------------------------------------------------------
# is_poi_invalidated — the KILL predicate.
# Verbatim port of V1 SMCStrategy.is_poi_invalidated (strategy.py:1964-2013).
#
# This is a *lifecycle* rule (Arch §3 "invalidation check"), not analysis: it
# reads a saved signal (memory) against the fresh cycle's HTF/price — analysis
# is forbidden to reference memory (§4). V1 physically located it on the
# strategy object; Phase 3 deliberately did NOT port it into analysis, so it
# lands here as the single implementation.
#
# The full try/except is preserved so a bad input (e.g. a ``None`` sl on the
# not-ready STALE path, where V1 read ``result.get("sl")`` == None) raises inside
# ``float(...)`` and returns False, exactly as V1's bare ``except`` did.
# ---------------------------------------------------------------------------

def is_poi_invalidated(
    *,
    direction: Optional[str],
    sl: Any,
    live_price: Any,
    fallback_entry_price: Any,
    d1_bias: Optional[str],
    h4_bias: Optional[str],
    atr_last: Any,
) -> bool:
    """Kill if HTF flips against the signal OR price breaks the SL zone.

    - ``direction`` / ``sl`` come from the *saved* signal (KILL) or the *fresh*
      result (STALE); ``sl`` may be None on the STALE not-ready path (-> False).
    - ``live_price`` is the forming-candle price; when it is not finite / <= 0,
      V1 falls back to the fresh result's entry price (``fallback_entry_price``).
    - ``d1_bias`` / ``h4_bias`` / ``atr_last`` are the *fresh cycle's* HTF /
      structure reading (V1 read ``context['htf_map']`` / ``h1_setup`` regardless
      of the decision tier); None biases default to "CHOPPY", matching V1's
      ``htf_map.get(..., "CHOPPY")``.
    """
    try:
        direction_s = str(direction if direction is not None else "").upper()

        current_price = float(live_price if live_price is not None else 0.0)
        if not math.isfinite(current_price) or current_price <= 0:
            current_price = float(
                fallback_entry_price if fallback_entry_price is not None else 0.0
            )

        # A None sl (STALE not-ready) raises here -> caught -> False (V1 quirk).
        sl_f = float(sl)  # type: ignore[arg-type]

        d1 = str(d1_bias if d1_bias is not None else "CHOPPY").upper()
        h4 = str(h4_bias if h4_bias is not None else "CHOPPY").upper()

        atr = float(atr_last if atr_last is not None else 0.0)
        atr_buffer = atr * 0.30 if (math.isfinite(atr) and atr > 0) else 0.0

        if direction_s == "LONG":
            if d1 == "BEARISH" and h4 == "BEARISH":
                return True
            if current_price < (sl_f - atr_buffer):
                return True
        elif direction_s == "SHORT":
            if d1 == "BULLISH" and h4 == "BULLISH":
                return True
            if current_price > (sl_f + atr_buffer):
                return True

        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# TTL tiering — V1 cleanup_expired_signals (main.py:190-195), verbatim.
# The miss_count > 0 tap-proxy is the second consumer of miss_count
# (Decisions #006/#007); it reads the raw value, never a recomputed tap-state.
# ---------------------------------------------------------------------------

def ttl_for(status: str, miss_count: int, config: LifecycleConfig) -> float:
    """The TTL (seconds) for a signal by status then tapped-proxy."""
    status_u = str(status).upper().strip()
    if status_u == "MANUAL_ACTION":
        return config.manual_action_ttl_seconds
    if int(miss_count) > 0:
        return config.tapped_ttl_seconds
    return config.untapped_ttl_seconds


# ---------------------------------------------------------------------------
# H1-gated decay — V1 main.py:736/753 (`h1_last_ts > prev_last_miss`).
# Decision #006: a miss only accrues once a NEW H1 candle has closed.
# ---------------------------------------------------------------------------

def h1_gate_open(h1_last_ts: float, last_miss_h1_ts: float) -> bool:
    """True when a new H1 has closed since the last recorded miss."""
    return float(h1_last_ts) > float(last_miss_h1_ts)
