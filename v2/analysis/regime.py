"""Regime stage — market-regime classification and the execution mode it implies.

Pipeline position: 2 of 9.

Produces: `context.regime` (`Regime` = label + execution mode).

Ported from V1 (`src/strategy.py`):
- `classify_regime`                  → `classify_regime_label` (strategy.py:667-756)
- regime → execution-philosophy map  → `derive_entry_mode`     (strategy.py:1308-1342)

`classify_regime_label` was co-located in `analysis.htf` during Phase 3.1 and is
**moved here verbatim** in Phase 3.2 — this module is now its single owner, per
Architecture §3 (`analysis.regime`: "classify regime and derive execution mode").

## Why `analysis.htf` still calls into this module

V1's dependency runs in *both* directions across the HTF/regime boundary:

- the regime **label** is computed first (strategy.py:1136) and feeds HTF's
  rotational anchor override (1213) and the hard veto (1252-1262);
- HTF's **direction** then feeds the regime's compression → execution-mode
  branch (1324-1334).

The frozen `Regime` object couples label and entry_mode, so a complete `Regime`
cannot exist before HTF runs, and HTF cannot wait for one. Therefore `HTFStage`
imports `classify_regime_label` from this module (it owns no regime logic), and
`RegimeStage` runs after HTF to assemble the complete `Regime` once `direction`
is known. Both call the same pure function, so the label is identical by
construction; V1 computes it once and V2 computes it twice, which is free of
behavioral consequence for a deterministic pure function of the same frame.

Purity: no I/O, no clock, no state, no caches.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from v2.analysis.context import AnalysisContext
from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.domain import HTFContext, MarketSnapshot, Regime

# V1 regime labels that map to a retracement execution philosophy
# (strategy.py:1316).
_RETRACEMENT_REGIMES = {"accumulation", "distribution", "rotational"}


def classify_regime_label(df: Optional[pd.DataFrame]) -> str:
    """V1 `classify_regime` (strategy.py:667-756), verbatim behavior.

    Returns one of: "expansion_up", "expansion_down", "accumulation",
    "distribution", "rotational", "compression", "neutral", "unknown".

    Reads the frame's existing `atr` column (V1 computes ATR upstream in
    `api_handler`); nothing here computes ATR.
    """
    if df is None or df.empty or len(df) < 30:
        return "unknown"

    x = df.copy().reset_index(drop=True)

    if "atr" not in x.columns or x["atr"].dropna().empty:
        return "unknown"

    atr_now_raw = x["atr"].iloc[-1]
    if pd.isna(atr_now_raw):
        return "unknown"

    atr_now = float(atr_now_raw)
    if atr_now <= 0:
        return "unknown"

    window = min(20, len(x))
    recent = x.tail(window)

    atr_avg_raw = recent["atr"].dropna().mean()
    if pd.isna(atr_avg_raw):
        return "unknown"

    atr_avg = float(atr_avg_raw)
    if atr_avg <= 0:
        return "unknown"

    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    recent_range = max(recent_high - recent_low, 1e-9)

    range_ratio = recent_range / atr_avg

    candle_range = (x["high"] - x["low"]).replace(0, float("nan"))
    body = (x["close"] - x["open"]).abs()
    body_ratio_series = (body / candle_range).tail(window).dropna()
    body_ratio = float(body_ratio_series.mean()) if not body_ratio_series.empty else 0.0

    rr_low = recent_low
    rr_high = recent_high
    rr = max(rr_high - rr_low, 1e-9)
    close_pos = (float(x["close"].iloc[-1]) - rr_low) / rr

    last_close = float(x["close"].iloc[-1])
    close_5 = float(x["close"].iloc[-5]) if len(x) >= 5 else last_close
    close_10 = float(x["close"].iloc[-10]) if len(x) >= 10 else close_5

    drift_5 = (last_close - close_5) / max(close_5, 1e-9)
    drift_10 = (last_close - close_10) / max(close_10, 1e-9)

    compression_votes = 0
    if range_ratio < 3.0:
        compression_votes += 1
    if atr_now <= atr_avg * 0.95:
        compression_votes += 1
    if body_ratio < 0.42:
        compression_votes += 1

    expansion_votes = 0
    if range_ratio > 4.0:
        expansion_votes += 1
    if atr_now >= atr_avg * 1.03:
        expansion_votes += 1
    if body_ratio > 0.46:
        expansion_votes += 1

    strong_drift = abs(drift_5) > 0.008 or abs(drift_10) > 0.012

    # Compression-like structure
    if compression_votes >= 2 and not strong_drift:
        if close_pos < 0.30:
            return "accumulation"
        if close_pos > 0.70:
            return "distribution"
        return "compression"

    # Expansion-like structure
    if expansion_votes >= 2:
        if drift_5 > 0.003 or drift_10 > 0.005:
            return "expansion_up"
        if drift_5 < -0.003 or drift_10 < -0.005:
            return "expansion_down"
        return "rotational"

    # Middle ground: not dead, not explosive
    if range_ratio >= 2.6 and (abs(drift_5) > 0.004 or abs(drift_10) > 0.006):
        return "rotational"

    return "neutral"


def derive_entry_mode(regime_label: str, htf: Optional[HTFContext]) -> str:
    """V1's regime → execution-philosophy mapping (strategy.py:1308-1342).

    Returns one of: "CONTINUATION", "RETRACEMENT", "WATCH_ONLY", "NONE".

    Two guards reproduce paths where V1 never reaches the mapping:
    - **no direction** — V1 returns at its `no_direction` exit (strategy.py:1238)
      before the mapping runs, so no mode is derived; "NONE" is V1's own
      non-actionable value.
    - **veto active** — V1's veto branch sets `entry_mode` to "NONE" explicitly
      (strategy.py:1280, 1303) rather than consulting the mapping.
    """
    if htf is None or htf.direction is None:
        return "NONE"
    if htf.veto_active:
        return "NONE"

    regime_str = str(regime_label).lower().strip()

    if regime_str.startswith("expansion"):
        return "CONTINUATION"

    if regime_str in _RETRACEMENT_REGIMES:
        return "RETRACEMENT"

    if regime_str == "compression":
        strong_htf_alignment = (
            (
                htf.direction == "LONG"
                and htf.weekly_bias == "BULLISH"
                and htf.d1_bias == "BULLISH"
                and htf.location == "discount"
            )
            or (
                htf.direction == "SHORT"
                and htf.weekly_bias == "BEARISH"
                and htf.d1_bias == "BEARISH"
                and htf.location == "premium"
            )
        )
        return "WATCH_ONLY" if strong_htf_alignment else "NONE"

    return "NONE"


class RegimeStage:
    """Classifies the market regime and the execution mode it implies."""

    name = "regime"

    def run(self, context: AnalysisContext) -> AnalysisContext:
        return context.with_(regime=self.analyze(context.snapshot, context.htf))

    @staticmethod
    def analyze(
        snapshot: MarketSnapshot, htf: Optional[HTFContext]
    ) -> Optional[Regime]:
        """Classify `snapshot` into a `Regime`.

        Returns None when `htf` is None: V1 classifies the regime only after
        `build_htf_map` succeeds (strategy.py:1125-1136), so its `invalid_input`
        and `invalid_htf_map` exits produce no regime at all.
        """
        if htf is None:
            return None

        h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
        if h1 is None:
            return None

        label = classify_regime_label(h1)
        return Regime(label=label, entry_mode=derive_entry_mode(label, htf))
