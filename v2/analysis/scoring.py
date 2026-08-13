"""Scoring stage — the score engine, the decision tier, and the final Signal.

Pipeline position: 9 of 9 (terminal — produces `context.signal` and
`context.candidate`).

This stage completes the analysis layer: it reproduces V1's `score_signal`
(strategy.py:761-1071) plus the wrapping logic in `build_signal_context` that
turns a scored result into the published outcome — the early exits, the choppy
penalty, the `h1_setup` assembly and the `result.update`.

Ported from V1 (`src/strategy.py`):
- `score_signal`                       (761-1071) → `score_signal`
- `_is_market_choppy`                  (41-58)    → `is_market_choppy`
- invalid_input / invalid_htf_map exit (1113-1134)→ `ScoringStage.run`
- no_direction exit                    (1238)     → `ScoringStage.run`
- veto exit                            (1264-1306)→ `ScoringStage.run`
- `target_clear`                       (1672)     → `_target_clear`
- `htf_context_clean`                  (1691)     → `_htf_context_clean`
- `h1_setup` assembly                  (1678-1702)→ `_h1_setup`
- not-ready exit                       (1716-1730)→ `ScoringStage.run`
- score_signal call + choppy + update  (1732-1782)→ `ScoringStage.run`
- `enrich_with_m5`                     (1809-1959)→ `enrich_with_m5`

## The score engine is a verbatim dict port

`score_signal` takes and returns the exact V1 dicts (htf_map / h1_setup /
m5_setup → result), so it can be diffed 1:1 against V1's method. The stage builds
those dicts from the domain objects and calls it — every weight, threshold,
bonus, note and ordering is preserved, including V1's own quirks (see below). No
scoring value is recomputed anywhere else.

## Injected session, never a clock read

V1's `h1_setup["session_active"]` is `get_session_mode() == "SNIPER"`
(strategy.py:1690) — a wall-clock read. Analysis must stay pure, so the flag is
an injected input (`context.session_active`, default False). The same flag drives
the M5 swing parameters (`(1, 0.15)` for SNIPER vs `(2, 0.25)`, strategy.py:1837).

## V1 quirks preserved

1. **The choppy penalty and `result.update` apply to score_signal's SKIP results
   too.** `build_signal_context` runs them on *any* dict `score_signal` returns
   (1767-1782), so e.g. a `bad_rr` SKIP in a choppy market publishes `score=-2`
   and `notes=["choppy_penalty"]`. Only the pre-scoring exits (invalid /
   no_direction / veto / not-ready) skip them.
2. **The not-ready exit precedes scoring**, so a not-ready SKIP carries no
   `entry_price`/`sl`/`tp` in its result — the observables read 0.0 / 0.0 / None,
   not the candidate's stored levels. WATCH_ONLY does *not* take this exit and is
   scored even when not ready.
3. **`score_signal` normalises an unknown `entry_mode` to RETRACEMENT** (790) and
   stringifies `tp_source` to `""` (969) — both harmless here because the
   reachable modes are already canonical and `build_signal_context` overwrites
   `tp_source` from the entry engine (1780).
4. **`rr` is computed inside `score_signal`** (975-1002) from entry/sl/tp against
   the 1.2 hard floor, and only there — it is stamped onto `RiskPlan.rr` when the
   candidate is finalised, which is why Phase 3.8 left that field None.

Purity: no I/O, no clock, no state, no caches, no numpy (V1's `np.isfinite` on
scalars is `math.isfinite`). ATR is *read*; nothing here computes ATR.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict, Optional

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.order_block import fresh_ob
from v2.analysis.scoring_config import (
    DEFAULT_SCORING_CONFIG,
    ScoreAccumulator,
    ScoringConfig,
)
from v2.analysis.structure import identify_swings
from v2.domain import (
    FairValueGap,
    HTFContext,
    LiquiditySweep,
    Regime,
    Signal,
    SignalCandidate,
)

#: The post-scoring choppy note, and the mode that is scored even when the entry
#: is not ready. Both are recorded ledger values, not tunables — see
#: `scoring_config` ("Reasons and note names are schema, not parameters").
CHOPPY_NOTE = "choppy_penalty"
WATCH_ONLY_MODE = "WATCH_ONLY"

# V1 M5 gate + swing parameters (strategy.py:1825, 1837).
_M5_MIN_ROWS = 6
_M5_SNIPER_SWING = (1, 0.15)
_M5_STRICT_SWING = (2, 0.25)


# ==========================================================================
# _is_market_choppy — V1 strategy.py:41-58 (verbatim)
# ==========================================================================

def is_market_choppy(
    df: pd.DataFrame, lookback: int = 20, wick_body_threshold: float = 1.8
) -> bool:
    if df is None or df.empty or len(df) < lookback:
        return True
    x = df.copy().reset_index(drop=True)
    w = x.tail(lookback)
    body = (w["close"] - w["open"]).abs()
    upper_wick = w["high"] - w[["open", "close"]].max(axis=1)
    lower_wick = w[["open", "close"]].min(axis=1) - w["low"]
    avg_body = float(body.mean())
    avg_wick = float((upper_wick + lower_wick).mean())
    avg_range = float((w["high"] - w["low"]).mean())
    if avg_range <= 0:
        return True
    wick_body_ratio = avg_wick / max(avg_body, 1e-9)
    body_ratio = avg_body / avg_range
    atr_ratio = (
        float(w["atr"].dropna().iloc[-1]) / max(float(w["close"].iloc[-1]), 1e-9)
        if "atr" in w.columns and not w["atr"].dropna().empty
        else None
    )
    if wick_body_ratio > wick_body_threshold:
        return True
    if body_ratio < 0.28:
        return True
    if atr_ratio is not None and atr_ratio > 0.08:
        return True
    return False


# ==========================================================================
# score_signal — V1 strategy.py:761-1071 (verbatim dict port)
# ==========================================================================

def score_signal(
    htf_map: Dict[str, Any],
    regime: str,
    h1_setup: Dict[str, Any],
    m5_setup: Optional[Dict[str, Any]] = None,
    *,
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> Dict[str, Any]:
    if not htf_map or not h1_setup:
        return {"score": 0, "decision": "SKIP", "reason": "missing_input"}

    direction = str(h1_setup.get("direction", "")).upper().strip()
    entry_mode = str(
        h1_setup.get("entry_mode", config.default_entry_mode)).upper().strip()

    if direction not in config.directions:
        return {"score": 0, "decision": "SKIP", "reason": "missing_direction"}

    if entry_mode not in config.entry_modes:
        entry_mode = config.default_entry_mode

    # `notes` and `score` move together from here on: every scored term appends
    # exactly one note, which is what makes a recorded `notes` list decomposable.
    acc = ScoreAccumulator(config.weights_for(entry_mode))
    notes = acc.notes

    displacement_present = bool(h1_setup.get("displacement_valid", False))
    volume_spike = bool(h1_setup.get("volume_spike", False))
    sweep_present = bool(h1_setup.get("sweep_valid", False))
    fvg_present = bool(h1_setup.get("fvg_valid", False))
    ob_present = bool(h1_setup.get("fresh_ob", False))

    # -- MODE-AWARE GATES --
    if entry_mode == "CONTINUATION":
        if str(h1_setup.get("entry_source", "")) != config.continuation_entry_source:
            return {
                "score": 0,
                "decision": "SKIP",
                "reason": "no_continuation_displacement",
                "entry_mode": entry_mode,
            }
        if not volume_spike:
            acc.award("weak_volume")
    elif entry_mode == "WATCH_ONLY":
        if not (
            htf_map.get("weekly_bias") in config.directional_biases
            and htf_map.get("d1_bias") in config.directional_biases
        ):
            return {
                "score": 0,
                "decision": "SKIP",
                "reason": "weak_watch_only_context",
                "entry_mode": entry_mode,
            }

    # -- HTF ALIGNMENT --
    if direction == "LONG":
        aligned = config.alignment["LONG"]
        if htf_map.get("weekly_bias") == aligned["bias"]:
            acc.award("weekly_aligned")
        if htf_map.get("d1_bias") == aligned["bias"]:
            acc.award("d1_aligned")
        if htf_map.get("h4_bias") == aligned["bias"]:
            acc.award("h4_aligned")
        if htf_map.get("location") == aligned["location"]:
            acc.award("discount")
    elif direction == "SHORT":
        aligned = config.alignment["SHORT"]
        if htf_map.get("weekly_bias") == aligned["bias"]:
            acc.award("weekly_aligned")
        if htf_map.get("d1_bias") == aligned["bias"]:
            acc.award("d1_aligned")
        if htf_map.get("h4_bias") == aligned["bias"]:
            acc.award("h4_aligned")
        if htf_map.get("location") == aligned["location"]:
            acc.award("premium")

    # -- CORE CONFIRMATIONS --
    if entry_mode == "CONTINUATION":
        if sweep_present:
            acc.award("sweep")
        if fvg_present:
            acc.award("fvg")
        if str(h1_setup.get("entry_source", "")) == config.continuation_entry_source:
            acc.award("displacement")
        if ob_present:
            acc.award("fresh_ob")
    elif entry_mode == "RETRACEMENT":
        if sweep_present:
            acc.award("sweep")
        else:
            acc.award("weak_sweep")
        if fvg_present:
            acc.award("fvg")
        if displacement_present:
            acc.award("displacement")
        if ob_present:
            acc.award("fresh_ob")
        else:
            acc.award("no_ob")
    else:  # WATCH_ONLY
        if sweep_present:
            acc.award("sweep")
        if fvg_present:
            acc.award("fvg")
        if displacement_present:
            acc.award("displacement")
        if ob_present:
            acc.award("fresh_ob")

    # -- STRUCTURE QUALITY --
    if h1_setup.get("target_clear", False):
        acc.award("target_clear")
    if h1_setup.get("session_active", False):
        acc.award("session_active")
    if h1_setup.get("htf_context_clean", False):
        acc.award("htf_context_clean")

    # -- REGIME ENGINE --
    if regime in config.expansion_regimes:
        acc.award("expansion")
    elif regime in config.squeeze_regimes:
        acc.award("squeeze")
    elif regime in config.penalty_regimes:
        acc.award("neutral_penalty")

    # -- RR ENGINE --
    entry = h1_setup.get("entry_price")
    sl = h1_setup.get("sl")
    tp = h1_setup.get("tp")
    entry_source = str(h1_setup.get("entry_source", "") or "")
    tp_source = str(h1_setup.get("tp_source", "") or "")
    tp_rr = h1_setup.get("tp_rr", None)

    rr = None

    if entry and sl and tp:
        try:
            risk = abs(float(entry) - float(sl))
            reward = abs(float(tp) - float(entry))

            if math.isfinite(risk) and math.isfinite(reward) and risk > 0:
                rr = reward / risk

                if rr < config.rr_floor:
                    return {
                        "score": 0,
                        "decision": "SKIP",
                        "reason": "bad_rr",
                        "rr": round(rr, 2),
                        "entry_mode": entry_mode,
                    }

                # The band's weight comes from the band, not the weight table:
                # the note text carries the value. `None` means no band matched
                # and no note is appended — distinct from a band worth 0, which
                # appends the note and adds nothing.
                band = config.rr_band_weight(rr)
                if band is not None:
                    acc.add(band, f"rr_{rr:.1f}")
        except Exception:
            pass

    # -- MOMENTUM QUALITY --
    if htf_map.get("momentum_weakening", False):
        acc.award("momentum_weakening")

    # -- M5 ENRICHMENT --
    if m5_setup and m5_setup.get("choch_confirmed", False):
        if entry_mode == "CONTINUATION":
            acc.award("m5_continuation_confirm")
        elif entry_mode == "RETRACEMENT":
            acc.award("m5_choch")
        else:
            acc.award("m5_watch_confirm")

    # -- FINAL DECISION --
    decision = config.decide(entry_mode, acc.score, displacement_present)

    return {
        "score": int(acc.score),
        "decision": decision,
        "direction": direction,
        "notes": notes,
        "regime": regime,
        "rr": round(rr, 2) if rr is not None else None,
        "entry_mode": entry_mode,
        "volume_spike": volume_spike,
        "entry_source": entry_source,
        "tp_source": tp_source,
        "tp_rr": tp_rr,
    }


# ==========================================================================
# Candidate assembly helpers (build_signal_context h1_setup, 1672-1702)
# ==========================================================================

def _htf_map(htf: HTFContext) -> Dict[str, Any]:
    """The subset of V1's htf_map that score_signal reads."""
    return {
        "weekly_bias": htf.weekly_bias,
        "d1_bias": htf.d1_bias,
        "h4_bias": htf.h4_bias,
        "location": htf.location,
        "momentum_weakening": htf.momentum_weakening,
    }


def _htf_context_clean(htf: HTFContext, direction: Optional[str]) -> bool:
    """V1 `htf_context_clean` (strategy.py:1691-1695), verbatim."""
    wb, db, hb, loc = htf.weekly_bias, htf.d1_bias, htf.h4_bias, htf.location
    if direction == "LONG":
        return (
            wb != "BEARISH" and db != "BEARISH" and hb != "BEARISH"
            and loc in {"discount", "equilibrium"}
        )
    return (
        wb != "BULLISH" and db != "BULLISH" and hb != "BULLISH"
        and loc in {"premium", "equilibrium"}
    )


def _target_clear(tp: float, last_close: float, atr_last: float) -> bool:
    """V1 `target_clear` (strategy.py:1672-1676), verbatim."""
    if atr_last > 0:
        return tp is not None and tp > 0 and abs(tp - last_close) >= (atr_last * 0.75)
    return True


def _h1_setup(
    context: AnalysisContext,
    htf: HTFContext,
    regime: Regime,
    last_close: float,
) -> Dict[str, Any]:
    """V1's `h1_setup` dict (strategy.py:1678-1702), rebuilt from the context."""
    entry, risk = context.entry, context.risk
    sweep = context.sweep if context.sweep is not None else LiquiditySweep(detected=False)
    fvg = context.fvg if context.fvg is not None else FairValueGap(valid=False)
    atr_last = float(context.atr_last)

    return {
        "direction": htf.direction,
        "entry_mode": entry.entry_mode,
        "entry_ready": entry.entry_ready,
        "entry_source": entry.entry_source,
        "entry_reason": entry.entry_reason,
        "sweep_valid": sweep.detected,
        "fvg_valid": fvg.valid,
        "displacement_valid": bool(context.displacement_valid),
        "fresh_ob": fresh_ob(context.order_blocks),
        "volume_spike": bool(context.volume_spike),
        "target_clear": _target_clear(risk.tp, last_close, atr_last),
        "session_active": bool(context.session_active),
        "htf_context_clean": _htf_context_clean(htf, htf.direction),
        "entry_price": entry.entry_price,
        "sl": risk.sl,
        "tp": risk.tp,
        "atr_last": atr_last,
        "tp_source": risk.tp_source,
        "tp_rr": risk.tp_rr,
    }


def _candidate(context: AnalysisContext, h1_setup: Dict[str, Any]) -> SignalCandidate:
    """Assemble the SignalCandidate from the context and the h1_setup flags."""
    return SignalCandidate(
        htf=context.htf,
        regime=context.regime,
        entry=context.entry,
        risk=context.risk,
        sweep=context.sweep if context.sweep is not None else LiquiditySweep(detected=False),
        fvg=context.fvg if context.fvg is not None else FairValueGap(valid=False),
        swing_highs=context.swing_highs or (),
        swing_lows=context.swing_lows or (),
        order_blocks=context.order_blocks or (),
        sweep_valid=h1_setup["sweep_valid"],
        fvg_valid=h1_setup["fvg_valid"],
        displacement_valid=h1_setup["displacement_valid"],
        fresh_ob=h1_setup["fresh_ob"],
        volume_spike=h1_setup["volume_spike"],
        target_clear=h1_setup["target_clear"],
        session_active=h1_setup["session_active"],
        htf_context_clean=h1_setup["htf_context_clean"],
        atr_last=h1_setup["atr_last"],
    )


def _skip(symbol: str, reason: str, **kw: Any) -> Signal:
    """An early-exit SKIP Signal (no candidate)."""
    return Signal(
        symbol=symbol,
        score=0,
        decision="SKIP",
        entry_mode=kw.pop("entry_mode", "NONE"),
        reason=reason,
        **kw,
    )


def _signal_from_result(
    symbol: str,
    result: Dict[str, Any],
    htf: HTFContext,
    regime: Regime,
    candidate: SignalCandidate,
) -> Signal:
    """Build the domain Signal from a score_signal-derived `result` dict.

    Flat entry/risk scalars live on the candidate, not the Signal (Arch §2);
    `rr` is stamped onto the candidate's RiskPlan so it stays reachable.
    """
    reason = result.get("reason")
    return Signal(
        symbol=symbol,
        score=int(result["score"]),
        decision=str(result["decision"]),
        entry_mode=candidate.entry.entry_mode,
        direction=htf.direction,
        notes=tuple(str(n) for n in (result.get("notes") or [])),
        regime=regime.label,
        reason=reason if reason is not None else None,
        m5_choch=result.get("m5_choch"),
        m5_reason=result.get("m5_reason"),
        candidate=candidate,
    )


def _finalize_candidate(
    context: AnalysisContext, rr: Optional[float], h1_setup: Dict[str, Any]
) -> SignalCandidate:
    """Candidate whose RiskPlan carries the scoring-side `rr` (Phase 3.8 left it
    None; scoring is where V1 computes it)."""
    risk_with_rr = dataclasses.replace(context.risk, rr=rr)
    return _candidate(context.with_(risk=risk_with_rr), h1_setup)


# ==========================================================================
# M5 enrichment — V1 strategy.py:1809-1959
# ==========================================================================

def _candidate_h1_setup(candidate: SignalCandidate) -> Dict[str, Any]:
    """The h1_setup dict as it stood when the candidate was built."""
    return {
        "direction": candidate.htf.direction,
        "entry_mode": candidate.entry.entry_mode,
        "entry_ready": candidate.entry.entry_ready,
        "entry_source": candidate.entry.entry_source,
        "entry_reason": candidate.entry.entry_reason,
        "sweep_valid": candidate.sweep_valid,
        "fvg_valid": candidate.fvg_valid,
        "displacement_valid": candidate.displacement_valid,
        "fresh_ob": candidate.fresh_ob,
        "volume_spike": candidate.volume_spike,
        "target_clear": candidate.target_clear,
        "session_active": candidate.session_active,
        "htf_context_clean": candidate.htf_context_clean,
        "entry_price": candidate.entry.entry_price,
        "sl": candidate.risk.sl,
        "tp": candidate.risk.tp,
        "atr_last": candidate.atr_last,
        "tp_source": candidate.risk.tp_source,
        "tp_rr": candidate.risk.tp_rr,
    }


def _m5_setup(
    direction: str,
    entry_mode: str,
    m5: pd.DataFrame,
    *,
    session_active: bool,
) -> Dict[str, Any]:
    """V1's M5 CHoCH / continuation detection (strategy.py:1835-1932), verbatim.

    Reuses `analysis.structure.identify_swings`; the swing window/prominence are
    the session-derived `(1, 0.15)` / `(2, 0.25)` (strategy.py:1837), with the
    session flag injected rather than read from the clock.
    """
    m5_atr = (
        float(m5["atr"].dropna().iloc[-1])
        if "atr" in m5.columns and not m5["atr"].dropna().empty
        else 0.0
    )

    cw, prom = _M5_SNIPER_SWING if session_active else _M5_STRICT_SWING
    sh_m5, sl_m5 = identify_swings(m5, cw, prom)

    last = m5.iloc[-1]
    prev = m5.iloc[-2]

    last_body = abs(float(last["close"]) - float(last["open"]))
    last_range = float(last["high"]) - float(last["low"])
    last_br = last_body / last_range if last_range > 0 else 0.0

    m5_confirmed = False
    m5_reason = "no_m5_confirmation"

    if entry_mode == "CONTINUATION":
        if direction == "LONG":
            ref_level = float(sh_m5[-1]["price"]) if sh_m5 else None
            m5_confirmed = (
                ref_level is not None
                and float(last["close"]) > ref_level
                and float(last["close"]) > float(last["open"])
                and last_body >= (m5_atr * 0.35)
                and last_br >= 0.50
                and float(last["close"]) >= float(prev["close"])
            )
        else:  # SHORT
            ref_level = float(sl_m5[-1]["price"]) if sl_m5 else None
            m5_confirmed = (
                ref_level is not None
                and float(last["close"]) < ref_level
                and float(last["close"]) < float(last["open"])
                and last_body >= (m5_atr * 0.35)
                and last_br >= 0.50
                and float(last["close"]) <= float(prev["close"])
            )
        m5_reason = (
            "continuation_micro_confirm" if m5_confirmed else "continuation_no_confirm"
        )

    else:
        breakout = m5.iloc[-2]
        confirm = m5.iloc[-1]

        b_m5 = abs(float(breakout["close"]) - float(breakout["open"]))
        rng_m5 = float(breakout["high"]) - float(breakout["low"])
        br_m5 = b_m5 / rng_m5 if rng_m5 > 0 else 0.0

        if direction == "LONG" and sh_m5:
            breakout_valid = (
                float(breakout["close"]) > float(sh_m5[-1]["price"])
                and float(breakout["close"]) > float(breakout["open"])
                and b_m5 >= (m5_atr * 0.5)
                and br_m5 >= 0.55
            )
            follow_through = (
                float(confirm["close"])
                >= (float(breakout["high"]) + float(breakout["low"])) / 2.0
                and float(confirm["low"]) >= float(prev["low"])
            )
            m5_confirmed = breakout_valid and follow_through
            m5_reason = (
                "retracement_long_confirm" if m5_confirmed else "retracement_long_fail"
            )
        elif direction == "SHORT" and sl_m5:
            breakout_valid = (
                float(breakout["close"]) < float(sl_m5[-1]["price"])
                and float(breakout["close"]) < float(breakout["open"])
                and b_m5 >= (m5_atr * 0.5)
                and br_m5 >= 0.55
            )
            follow_through = (
                float(confirm["close"])
                <= (float(breakout["high"]) + float(breakout["low"])) / 2.0
                and float(confirm["high"]) <= float(prev["high"])
            )
            m5_confirmed = breakout_valid and follow_through
            m5_reason = (
                "retracement_short_confirm" if m5_confirmed else "retracement_short_fail"
            )

    return {
        "choch_confirmed": m5_confirmed,
        "m5_reason": m5_reason,
        "entry_mode": entry_mode,
    }


def enrich_with_m5(
    base_signal: Signal,
    m5_df: Optional[pd.DataFrame],
    *,
    session_active: bool,
    config: ScoringConfig = DEFAULT_SCORING_CONFIG,
) -> Signal:
    """V1 `enrich_with_m5` (strategy.py:1809-1959), verbatim behavior.

    Re-scores an already-scored Signal with M5 confirmation. A candidate-less
    early-exit Signal, an absent/short M5 frame, or a non-directional setup all
    return the base Signal unchanged — matching V1's guards (1812-1833).
    """
    candidate = base_signal.candidate
    if candidate is None:
        return base_signal

    if m5_df is None or m5_df.empty:
        return base_signal

    m5 = m5_df.copy().reset_index(drop=True)
    m5 = m5.dropna(
        subset=[c for c in ["timestamp", "open", "high", "low", "close"] if c in m5.columns]
    )
    if len(m5) < _M5_MIN_ROWS:
        return base_signal

    htf, regime = candidate.htf, candidate.regime
    direction = str(candidate.htf.direction or "").upper().strip()
    entry_mode = str(
        candidate.entry.entry_mode or config.default_entry_mode).upper().strip()

    if direction not in config.directions:
        return base_signal

    m5_setup = _m5_setup(direction, entry_mode, m5, session_active=session_active)

    # V1 reconstructs its h1_setup from the context; the candidate carries the
    # identical inputs, so the re-score sees the same values plus m5_setup.
    h1_setup = _candidate_h1_setup(candidate)
    result = dict(score_signal(
        _htf_map(htf), regime.label, h1_setup, m5_setup, config=config))

    # V1 re-applies the choppy penalty from the base result's notes (1946-1948).
    if CHOPPY_NOTE in base_signal.notes:
        result["score"] = int(result.get("score", 0)) - config.choppy_penalty
        result.setdefault("notes", []).append(CHOPPY_NOTE)

    result.update(
        {
            "entry_price": candidate.entry.entry_price,
            "sl": candidate.risk.sl,
            "tp": candidate.risk.tp,
            "m5_choch": m5_setup["choch_confirmed"],
            "m5_reason": m5_setup["m5_reason"],
            "entry_mode": entry_mode,
        }
    )

    finalized = dataclasses.replace(
        candidate, risk=dataclasses.replace(candidate.risk, rr=result.get("rr"))
    )
    return _signal_from_result(base_signal.symbol, result, htf, regime, finalized)


# ==========================================================================
# The stage
# ==========================================================================

class ScoringStage:
    """Scores the setup, assigns the decision tier, and emits the Signal.

    `config` is the scoring seam: the stage holds the `ScoringConfig` the whole
    analysis runs under, and `AnalysisPipeline.scoring_config` reads it back off
    the stage so the M5 re-score cannot drift from the pipeline. Defaulting to
    `DEFAULT_SCORING_CONFIG` keeps every existing caller — `ScoringStage()` —
    working unchanged and producing 2.2.0 behaviour.
    """

    name = "scoring"

    def __init__(self, config: ScoringConfig = DEFAULT_SCORING_CONFIG) -> None:
        self.config = config

    def run(self, context: AnalysisContext) -> AnalysisContext:
        symbol = context.snapshot.symbol
        htf = context.htf

        # -- pre-scoring exits (build_signal_context 1113-1306) --
        if htf is None:
            d1 = safe_frame(context.snapshot.d1, 5)
            h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
            reason = "invalid_input" if (d1 is None or h1 is None) else "invalid_htf_map"
            return context.with_(signal=_skip(symbol, reason, regime="unknown"))

        regime = context.regime
        regime_label = regime.label if regime is not None else "unknown"

        if htf.direction is None:
            return context.with_(
                signal=_skip(symbol, "no_direction", regime=regime_label)
            )

        if htf.veto_active:
            return context.with_(
                signal=_skip(
                    symbol,
                    str(htf.veto_reason),
                    direction=htf.direction,
                    regime=regime_label,
                )
            )

        h1 = safe_frame(context.snapshot.h1, H1_MIN_ROWS)
        last_close = float(h1["close"].iloc[-1])
        h1_setup = _h1_setup(context, htf, regime, last_close)

        entry = context.entry

        # -- not-ready exit (1716-1730): precedes scoring, no result.update --
        if not entry.entry_ready and entry.entry_mode != WATCH_ONLY_MODE:
            candidate = _candidate(context, h1_setup)
            signal = Signal(
                symbol=symbol,
                score=0,
                decision="SKIP",
                entry_mode=entry.entry_mode,
                direction=htf.direction,
                regime=regime_label,
                reason=entry.entry_reason,
                candidate=candidate,
            )
            return context.with_(candidate=candidate, signal=signal)

        # -- score + choppy penalty + result.update (1732-1782) --
        result = dict(score_signal(
            _htf_map(htf), regime_label, h1_setup, None, config=self.config))

        if is_market_choppy(h1):
            result["score"] = int(result.get("score", 0)) - self.config.choppy_penalty
            result.setdefault("notes", []).append(CHOPPY_NOTE)

        candidate = _finalize_candidate(context, result.get("rr"), h1_setup)
        signal = _signal_from_result(symbol, result, htf, regime, candidate)
        return context.with_(candidate=candidate, signal=signal)
