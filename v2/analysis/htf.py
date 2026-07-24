"""HTF stage — higher-timeframe context and directional bias.

Pipeline position: 1 of 9 (first — everything downstream is conditioned on
direction).

Produces: `context.htf` (`HTFContext`), or None when V1 would have bailed before
producing an htf_map (its `invalid_input` / `invalid_htf_map` early exits).

Ported from V1 (`src/strategy.py`), reproduced behavior-for-behavior:
- `build_signal_context._safe_df`      → `analysis.frames.safe_frame` (strategy.py:1077-1084)
- `build_htf_map`                      → `_build_htf_map`   (strategy.py:559-610)
- `_cached_bias`                       → `_timeframe_bias`  (strategy.py:21-35)
- `_derive_simple_bias_from_df`        → `_derive_simple_bias` (strategy.py:612-662)
- `_momentum_weakening`                → `_momentum_weakening` (strategy.py:60-72)
- weighted direction selection         → `_select_direction`  (strategy.py:1144-1222)
- hard expansion-vs-direction veto     → `_veto`              (strategy.py:1248-1266)

This stage owns no regime logic. V1 computes the regime label *before* direction
selection and consumes it in the rotational anchor override (strategy.py:1213)
and the hard veto (1252-1262), so HTF needs the label — but it obtains it by
calling `analysis.regime.classify_regime_label`, which owns that behavior per
Architecture §3. See `analysis.regime` for why the dependency cannot be inverted
through the AnalysisContext.

This stage also owns no swing detection. HTF bias derivation needs swings, but it
obtains them from `analysis.structure.identify_swings`, which owns that behavior
per Architecture §3 (extracted in Phase 3.3). V1 calls its single
`identify_swings` with two different parameter sets; HTF uses the *bias* set
(window 3, prominence 0.18 — strategy.py:620), while the structural set
(window 2, prominence 0.3) belongs to `analysis.structure`.

Purity: no I/O, no clock, no network, no global or cached state. V1's
`_cached_bias` memoized results in `self._bias_cache`; that cache is a pure
performance optimization (same input → same bias), so it is omitted here without
any behavioral effect. ATR is *read* from the snapshot's existing `atr` column
(V1 computes it upstream in `api_handler`); nothing here computes ATR.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Tuple

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import D1_MIN_ROWS, H1_MIN_ROWS, safe_frame
from v2.analysis.regime import classify_regime_label
from v2.analysis.structure import identify_swings
from v2.domain import HTFContext, MarketSnapshot

# V1 bias-derivation parameters (strategy.py:620).
_BIAS_SWING_WINDOW = 3
_BIAS_PROMINENCE_MULT = 0.18


# --------------------------------------------------------------------------
# Bias derivation
# --------------------------------------------------------------------------

def _derive_simple_bias(df: Optional[pd.DataFrame]) -> str:
    """V1 `_derive_simple_bias_from_df` (strategy.py:612-662).

    Returns "BULLISH" | "BEARISH" | "CHOPPY".
    """
    if df is None or df.empty or len(df) < 20:
        return "CHOPPY"

    x = df.copy().reset_index(drop=True)
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["ema200"] = x["close"].ewm(span=200, adjust=False).mean()

    sh, sl = identify_swings(x, _BIAS_SWING_WINDOW, _BIAS_PROMINENCE_MULT)

    current_close = float(x["close"].iloc[-1])
    ema50_now = float(x["ema50"].iloc[-1])
    ema200_now = float(x["ema200"].iloc[-1])
    ema50_prev = float(x["ema50"].iloc[-5]) if len(x) >= 5 else ema50_now

    bull = 0.0
    bear = 0.0

    # 1) Structure swing
    if len(sh) >= 2 and len(sl) >= 2:
        sh1, sh2 = float(sh[-1]["price"]), float(sh[-2]["price"])
        sl1, sl2 = float(sl[-1]["price"]), float(sl[-2]["price"])

        if sh1 > sh2 and sl1 > sl2:
            bull += 2.0
        elif sh1 < sh2 and sl1 < sl2:
            bear += 2.0

    # 2) Price vs EMA50
    if current_close > ema50_now:
        bull += 1.5
    elif current_close < ema50_now:
        bear += 1.5

    # 3) Price vs EMA200
    if current_close > ema200_now:
        bull += 2.0
    elif current_close < ema200_now:
        bear += 2.0

    # 4) EMA50 slope
    if ema50_now > ema50_prev:
        bull += 1.0
    elif ema50_now < ema50_prev:
        bear += 1.0

    if bull >= 4.0 and bull > bear:
        return "BULLISH"
    if bear >= 4.0 and bear > bull:
        return "BEARISH"
    return "CHOPPY"


def _timeframe_bias(df: Optional[pd.DataFrame]) -> str:
    """V1 `_cached_bias` (strategy.py:21-35) with the memo cache removed.

    The cache was keyed by timeframe + last timestamp and only ever returned a
    previously-computed result for identical input, so removing it preserves
    behavior exactly while keeping this layer free of hidden state.
    """
    if df is None or df.empty:
        return "CHOPPY"
    x = df.copy().reset_index(drop=True)
    if "timestamp" not in x.columns:
        return _derive_simple_bias(x)
    x = x.dropna(subset=["timestamp", "high", "low", "close"])
    if x.empty:
        return "CHOPPY"
    return _derive_simple_bias(x)


def _momentum_weakening(df: Optional[pd.DataFrame], lookback: int = 5) -> bool:
    """V1 `_momentum_weakening` (strategy.py:60-72)."""
    if df is None or df.empty or len(df) < lookback * 2:
        return False
    x = df.copy().reset_index(drop=True)
    recent = x.tail(lookback)
    prev = x.iloc[-lookback * 2 : -lookback]
    recent_trend = abs(float(recent["close"].iloc[-1]) - float(recent["close"].iloc[0]))
    prev_trend = abs(float(prev["close"].iloc[-1]) - float(prev["close"].iloc[0]))
    recent_range = float(recent["high"].max() - recent["low"].min())
    prev_range = float(prev["high"].max() - prev["low"].min())
    trend_weak = recent_trend < (prev_trend * 0.6)
    range_weak = recent_range < (prev_range * 0.7)
    return trend_weak or range_weak


# --------------------------------------------------------------------------
# HTF map
# --------------------------------------------------------------------------

def _build_htf_map(
    df_d1: Optional[pd.DataFrame], df_h1: Optional[pd.DataFrame]
) -> Optional[dict[str, Any]]:
    """V1 `build_htf_map` (strategy.py:559-610). None when V1 returns None."""
    if df_d1 is None or df_h1 is None or df_d1.empty or df_h1.empty:
        return None

    d1 = df_d1.copy().reset_index(drop=True)
    h1 = df_h1.copy().reset_index(drop=True)
    if len(d1) < 5 or len(h1) < 20:
        return None

    d1["timestamp"] = pd.to_datetime(d1["timestamp"], errors="coerce")
    h1["timestamp"] = pd.to_datetime(h1["timestamp"], errors="coerce")
    d1 = d1.dropna(subset=["timestamp", "high", "low", "close"])
    h1 = h1.dropna(subset=["timestamp", "high", "low", "close"])

    d1["date"] = d1["timestamp"].dt.date
    d1["year"] = d1["timestamp"].dt.isocalendar().year.astype(int)
    d1["week"] = d1["timestamp"].dt.isocalendar().week.astype(int)

    daily = (
        d1.groupby("date", sort=True)
        .agg(day_high=("high", "max"), day_low=("low", "min"))
        .reset_index()
    )
    if len(daily) < 2:
        return None
    pdh, pdl = float(daily.iloc[-2]["day_high"]), float(daily.iloc[-2]["day_low"])
    daily_eq = (pdh + pdl) / 2.0

    weekly = (
        d1.groupby(["year", "week"], sort=True)
        .agg(week_high=("high", "max"), week_low=("low", "min"))
        .reset_index()
    )
    pwh, pwl, weekly_eq = None, None, None
    weekly_bias = "CHOPPY"
    if len(weekly) >= 2:
        pwh, pwl = float(weekly.iloc[-2]["week_high"]), float(weekly.iloc[-2]["week_low"])
        weekly_eq = (pwh + pwl) / 2.0
        current_price = float(d1.iloc[-1]["close"])
        if current_price > weekly_eq:
            weekly_bias = "BULLISH"
        elif current_price < weekly_eq:
            weekly_bias = "BEARISH"

    current_price = float(d1.iloc[-1]["close"])
    d1_bias = _timeframe_bias(d1)

    h1_rs = h1.set_index("timestamp").sort_index()
    h4 = (
        h1_rs.resample("4h")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
        )
        .dropna()
        .reset_index()
    )
    if len(h4) >= 2:
        h4 = h4.iloc[:-1].copy()
    h4_bias = _timeframe_bias(h4) if len(h4) >= 5 else "CHOPPY"

    location = "equilibrium"
    if weekly_eq is not None:
        if current_price < daily_eq and current_price < weekly_eq:
            location = "discount"
        elif current_price > daily_eq and current_price > weekly_eq:
            location = "premium"

    return {
        "pdh": pdh,
        "pdl": pdl,
        "pwh": pwh,
        "pwl": pwl,
        "daily_eq": daily_eq,
        "weekly_eq": weekly_eq,
        "h4_bias": h4_bias,
        "d1_bias": d1_bias,
        "location": location,
        "current_price": current_price,
        "weekly_bias": weekly_bias,
        "momentum_weakening": _momentum_weakening(d1),
    }


# --------------------------------------------------------------------------
# Weighted direction selection + veto
# --------------------------------------------------------------------------

def _select_direction(
    htf_map: dict[str, Any], h1: Optional[pd.DataFrame], regime_str: str
) -> Tuple[float, float, Optional[str], str, Optional[str], bool]:
    """V1's weighted direction block (strategy.py:1144-1222).

    Returns (bull, bear, direction, direction_source, fallback_h1_bias,
    anchor_override).
    """
    wb = htf_map["weekly_bias"]
    db = htf_map["d1_bias"]
    hb = htf_map["h4_bias"]
    loc = htf_map["location"]

    bull = 0.0
    bear = 0.0
    fallback_h1_bias = None
    direction_source = "none"

    # Weekly should be dominant
    if wb == "BULLISH":
        bull += 4.0
    elif wb == "BEARISH":
        bear += 4.0

    # Daily still matters, but less than weekly
    if db == "BULLISH":
        bull += 2.0
    elif db == "BEARISH":
        bear += 2.0

    # H4 is supportive, not decisive
    if hb == "BULLISH":
        bull += 1.0
    elif hb == "BEARISH":
        bear += 1.0

    # Location must be context-aware, not absolute
    if loc == "discount":
        if wb == "BULLISH":
            bull += 1.5
        elif wb == "BEARISH":
            bull += 0.5
    elif loc == "premium":
        if wb == "BEARISH":
            bear += 1.5
        elif wb == "BULLISH":
            bear += 0.5

    if max(bull, bear) < 4.0:
        fallback_h1_bias = _derive_simple_bias(h1)
        if fallback_h1_bias == "BULLISH":
            bull += 1.0
            direction_source = "fallback_h1"
        elif fallback_h1_bias == "BEARISH":
            bear += 1.0
            direction_source = "fallback_h1"

    direction = None

    # V1 keeps both branches here even though they are identical; reproduced
    # verbatim so this stays a 1:1 diff against strategy.py:1194-1199.
    if regime_str == "rotational":
        min_score_for_direction = 4.0
        min_gap = 1.0
    else:
        min_score_for_direction = 4.0
        min_gap = 1.0

    if bull >= min_score_for_direction and bull >= bear + min_gap:
        direction = "LONG"
        if direction_source == "none":
            direction_source = "weighted_bull"
    elif bear >= min_score_for_direction and bear >= bull + min_gap:
        direction = "SHORT"
        if direction_source == "none":
            direction_source = "weighted_bear"

    # Weekly anchor override for rotational conflict
    anchor_override = False

    if direction is None and regime_str == "rotational":
        if wb == "BULLISH" and bull > bear:
            direction = "LONG"
            direction_source = "weekly_anchor_rotational"
            anchor_override = True

        elif wb == "BEARISH" and bear > bull:
            direction = "SHORT"
            direction_source = "weekly_anchor_rotational"
            anchor_override = True

    return bull, bear, direction, direction_source, fallback_h1_bias, anchor_override


def _veto(
    direction: Optional[str], weekly_bias: str, regime_str: str
) -> Tuple[bool, Optional[str]]:
    """V1's hard expansion-vs-direction veto (strategy.py:1248-1266).

    V1 evaluates this only after establishing a direction; with `direction` None
    both conditions are False, so the guard is implicit here.
    """
    veto_short = (
        direction == "SHORT" and weekly_bias == "BULLISH" and regime_str == "expansion_up"
    )
    veto_long = (
        direction == "LONG" and weekly_bias == "BEARISH" and regime_str == "expansion_down"
    )
    if veto_short:
        return True, "weekly_bullish_expansion_up_short_veto"
    if veto_long:
        return True, "weekly_bearish_expansion_down_long_veto"
    return False, None


# --------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------

class HTFStage:
    """Derives the higher-timeframe context and directional bias."""

    name = "htf"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        return context.with_(htf=self.analyze(context.snapshot))

    @staticmethod
    def analyze(snapshot: MarketSnapshot) -> Optional[HTFContext]:
        """Compute the HTF reading for one snapshot.

        Returns None exactly where V1 exits before producing an htf_map — its
        `invalid_input` (insufficient/unusable D1 or H1) and `invalid_htf_map`
        paths. A returned context may still carry `direction=None` (V1's
        `no_direction` exit) or `veto_active=True`; both are valid HTF readings
        that later stages translate into a SKIP.
        """
        d1 = safe_frame(snapshot.d1, D1_MIN_ROWS)
        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        if d1 is None or h1 is None:
            return None

        htf_map = _build_htf_map(d1, h1)
        if htf_map is None:
            return None

        regime_str = str(classify_regime_label(h1)).lower().strip()

        (
            bull,
            bear,
            direction,
            direction_source,
            fallback_h1_bias,
            anchor_override,
        ) = _select_direction(htf_map, h1, regime_str)

        veto_active, veto_reason = _veto(direction, htf_map["weekly_bias"], regime_str)

        return HTFContext(
            pdh=htf_map["pdh"],
            pdl=htf_map["pdl"],
            daily_eq=htf_map["daily_eq"],
            weekly_bias=htf_map["weekly_bias"],
            d1_bias=htf_map["d1_bias"],
            h4_bias=htf_map["h4_bias"],
            location=htf_map["location"],
            current_price=htf_map["current_price"],
            momentum_weakening=htf_map["momentum_weakening"],
            bull=bull,
            bear=bear,
            direction_source=direction_source,
            anchor_override=anchor_override,
            veto_active=veto_active,
            pwh=htf_map["pwh"],
            pwl=htf_map["pwl"],
            weekly_eq=htf_map["weekly_eq"],
            direction=direction,
            fallback_h1_bias=fallback_h1_bias,
            veto_reason=veto_reason,
        )
