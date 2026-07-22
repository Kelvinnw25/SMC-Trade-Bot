"""HTFContext — the higher-timeframe reading.

Architecture ref: 02-software-architecture.md §2 (HTFContext).
V1 behavior ref: ``SMCStrategy.build_htf_map`` (htf_map dict) plus the weighted
direction selection / veto block inside ``build_signal_context``.

Enum-like fields stay ``str`` with the exact V1 values (plan §5 — over-typing
risk R2): bias values are "BULLISH" / "BEARISH" / "CHOPPY"; location is
"discount" / "premium" / "equilibrium"; direction is "LONG" / "SHORT" / None;
direction_source is one of "none", "fallback_h1", "weighted_bull",
"weighted_bear", "weekly_anchor_rotational".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class HTFContext:
    """Derived from MarketSnapshot; consumed by regime mapping, entry, scoring.

    htf_map fields mirror V1 ``build_htf_map`` output; the weighted-direction
    fields mirror V1's direction block (bull/bear weights: weekly 4.0 > D1 2.0
    > H4 1.0, context-aware location, fallback H1 bias, rotational weekly
    anchor override, hard expansion-vs-direction veto).
    """

    # --- V1 htf_map ---
    pdh: float
    pdl: float
    daily_eq: float
    weekly_bias: str
    d1_bias: str
    h4_bias: str
    location: str
    current_price: float
    momentum_weakening: bool
    # --- V1 weighted direction selection ---
    bull: float
    bear: float
    direction_source: str
    anchor_override: bool
    # --- V1 hard veto ---
    veto_active: bool
    # --- optionals (V1 may produce None) ---
    pwh: Optional[float] = None
    pwl: Optional[float] = None
    weekly_eq: Optional[float] = None
    direction: Optional[str] = None
    fallback_h1_bias: Optional[str] = None
    veto_reason: Optional[str] = None
