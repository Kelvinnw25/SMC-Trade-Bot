"""Regime — the classified market state and derived execution mode.

Architecture ref: 02-software-architecture.md §2 (Regime).
V1 behavior ref: ``SMCStrategy.classify_regime`` (label) and the
regime→execution-philosophy mapping inside ``build_signal_context``.

Exact V1 string values (plan §5, risk R2 — no enums):
    label:      "expansion_up" | "expansion_down" | "accumulation" |
                "distribution" | "rotational" | "compression" | "neutral" |
                "unknown"
    entry_mode: "CONTINUATION" | "RETRACEMENT" | "WATCH_ONLY" | "NONE"
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Regime:
    """Derived from MarketSnapshot; drives which entry engine runs."""

    label: str
    entry_mode: str
