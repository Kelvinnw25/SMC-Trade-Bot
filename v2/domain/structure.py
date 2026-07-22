"""Structural domain objects: Swing, OrderBlock, FairValueGap, LiquiditySweep.

Architecture ref: 02-software-architecture.md §2.
V1 behavior refs: ``identify_swings``, the OB candidate registration
(``_register_ob_candidate`` closure), the FVG inline loops, ``_detect_sweep``.

OrderBlock note (Decision #002 — First Match is FROZEN): detection produces ALL
candidates as first-class objects; ``is_selected`` marks the one first-match
selection. ``OrderBlock.to_v1_dict``/``from_v1_dict`` reproduce the exact V1
candidate-dict shape used in the OB audit trail (``retracement_ob_audit.jsonl``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Swing:
    """A single detected swing point (V1: ``{'index': i, 'price': p}`` lists).

    kind: "high" | "low" (V1 keeps separate swing_highs/swing_lows lists; the
    kind field encodes which list the point came from).
    """

    index: int
    price: float
    kind: str


@dataclass(frozen=True)
class OrderBlock:
    """A detected OB candidate zone (V1 candidate dict, field-for-field).

    fresh_status: V1 freshness check (no later close beyond the zone).
    passed_quality_filter: V1 OB quality filter (size >= 0.25*ATR, not lost,
        distance ok) evaluated at registration time.
    is_selected: True for the first candidate that passed the quality filter
        (first-match policy, Decision #002 — frozen).
    """

    index: int
    rank_from_last: int
    top: float
    bottom: float
    size: float
    fresh_status: bool
    passed_quality_filter: bool
    is_selected: bool
    size_atr: Optional[float] = None
    depth_from_price: Optional[float] = None

    def to_v1_dict(self) -> Dict[str, Any]:
        """Exact V1 candidate dict (key order preserved)."""
        return {
            "index": self.index,
            "rank_from_last": self.rank_from_last,
            "top": self.top,
            "bottom": self.bottom,
            "size": self.size,
            "size_atr": self.size_atr,
            "depth_from_price": self.depth_from_price,
            "fresh_status": self.fresh_status,
            "passed_quality_filter": self.passed_quality_filter,
            "is_selected": self.is_selected,
        }

    @classmethod
    def from_v1_dict(cls, data: Dict[str, Any]) -> "OrderBlock":
        return cls(
            index=int(data["index"]),
            rank_from_last=int(data["rank_from_last"]),
            top=float(data["top"]),
            bottom=float(data["bottom"]),
            size=float(data["size"]),
            fresh_status=bool(data["fresh_status"]),
            passed_quality_filter=bool(data["passed_quality_filter"]),
            is_selected=bool(data["is_selected"]),
            size_atr=None if data.get("size_atr") is None else float(data["size_atr"]),
            depth_from_price=(
                None if data.get("depth_from_price") is None
                else float(data["depth_from_price"])
            ),
        )


@dataclass(frozen=True)
class FairValueGap:
    """A detected FVG.

    V1 records only the boolean ``fvg_valid`` (the inline loop breaks on first
    valid gap without persisting bounds), so bounds/index are optional and
    default to None to stay faithful — populating them is allowed but nothing
    in V1 depends on it.
    """

    valid: bool
    index: Optional[int] = None
    top: Optional[float] = None
    bottom: Optional[float] = None


@dataclass(frozen=True)
class LiquiditySweep:
    """Result of sweep detection (V1 ``_detect_sweep`` -> (bool, metadata))."""

    detected: bool
    index: Optional[int] = None
    ref_index: Optional[int] = None
    ref_price: Optional[float] = None
    sweep_depth: Optional[float] = None
    reclaim_strength: Optional[float] = None
