"""OBAuditRecord — observability contract for the OB-selection audit trail.

Approved extension (plan §10, proposed Decision #010), placement validated by
architectural analysis (plan §10.5):

    This is an OBSERVABILITY CONTRACT — a sibling of LogEvent, not a peer of
    Signal. It is the evidence trail for the OB selection policy frozen under
    investigation (Decision #002: First Match).

    HARD CONSTRAINTS:
    - No ``analysis.*`` or ``lifecycle.*`` module may consume, import, or
      condition on this object. It is write-only observability; reading it
      back into any decision is a behavior change and out of scope for V2.
    - Created by ``app.orchestrator`` (the eligibility conditional is
      operational per Architecture Principle 12; the per-process dedup set —
      V1's in-memory ``ob_audit_seen``, which resets on restart — is
      orchestrator-owned transient state per Architecture §6).
    - Schema owned by ``domain`` (this module); executed by ``infra`` as an
      append-only JSONL sink (``retracement_ob_audit.jsonl``).

V1 behavior ref: the ``audit_record`` literal in main.py — emitted for
RETRACEMENT signals with ``entry_source == "retracement_ob"`` and valid
levels, deduplicated per ``(symbol, h1_last_ts, entry_mode)``.

Nested shapes kept faithful to the V1 record:
- ``window_bounds``: ``(start_index, end_index)`` or None — serialized as
  ``{"start_index": ..., "end_index": ...}``.
- ``ob_meta``: ``(top, bottom)`` or None — serialized as
  ``{"top": ..., "bottom": ...}``.
- ``ob_candidates``: tuple of OrderBlock — serialized as the V1 candidate
  dicts, in order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from v2.domain.structure import OrderBlock


@dataclass(frozen=True)
class OBAuditRecord:
    """One JSONL audit line, field-for-field with V1's ``audit_record``."""

    symbol: str
    h1_last_ts: float
    direction: str
    entry_mode: str
    decision: str
    score: float
    regime: str
    location: str
    market_price: float
    entry_price: float
    sl: float
    tp: float
    rr: Optional[float]
    entry_source: str
    tp_source: str
    volume_spike: bool
    window_bounds: Optional[Tuple[int, int]] = None
    ob_meta: Optional[Tuple[float, float]] = None
    ob_candidates: Tuple[OrderBlock, ...] = ()
    selected_ob_index: Optional[int] = None
    selected_ob_rank: Optional[int] = None
    selected_ob_top: Optional[float] = None
    selected_ob_bottom: Optional[float] = None
    selected_ob_size: Optional[float] = None
    fresh_ob: Optional[bool] = None
    fvg_valid: Optional[bool] = None
    current_price: Optional[float] = None
    entry_equals_ob_bottom: Optional[bool] = None

    def to_v1_dict(self) -> Dict[str, Any]:
        """Exact V1 ``audit_record`` dict, same keys, same order."""
        return {
            "symbol": self.symbol,
            "h1_last_ts": self.h1_last_ts,
            "direction": self.direction,
            "entry_mode": self.entry_mode,
            "decision": self.decision,
            "score": self.score,
            "regime": self.regime,
            "location": self.location,
            "market_price": self.market_price,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "rr": self.rr,
            "entry_source": self.entry_source,
            "tp_source": self.tp_source,
            "volume_spike": self.volume_spike,
            "window_bounds": (
                None
                if self.window_bounds is None
                else {
                    "start_index": self.window_bounds[0],
                    "end_index": self.window_bounds[1],
                }
            ),
            "ob_meta": (
                None
                if self.ob_meta is None
                else {"top": self.ob_meta[0], "bottom": self.ob_meta[1]}
            ),
            "ob_candidates": [c.to_v1_dict() for c in self.ob_candidates],
            "selected_ob_index": self.selected_ob_index,
            "selected_ob_rank": self.selected_ob_rank,
            "selected_ob_top": self.selected_ob_top,
            "selected_ob_bottom": self.selected_ob_bottom,
            "selected_ob_size": self.selected_ob_size,
            "fresh_ob": self.fresh_ob,
            "fvg_valid": self.fvg_valid,
            "current_price": self.current_price,
            "entry_equals_ob_bottom": self.entry_equals_ob_bottom,
        }

    @classmethod
    def from_v1_dict(cls, data: Dict[str, Any]) -> "OBAuditRecord":
        """Parse one V1 JSONL line (already json-decoded) back into the record."""
        wb = data.get("window_bounds")
        ob_meta = data.get("ob_meta")
        return cls(
            symbol=str(data["symbol"]),
            h1_last_ts=float(data["h1_last_ts"]),
            direction=str(data["direction"]),
            entry_mode=str(data["entry_mode"]),
            decision=str(data["decision"]),
            score=data["score"],
            regime=str(data["regime"]),
            location=str(data["location"]),
            market_price=float(data["market_price"]),
            entry_price=float(data["entry_price"]),
            sl=float(data["sl"]),
            tp=float(data["tp"]),
            rr=None if data.get("rr") is None else float(data["rr"]),
            entry_source=str(data["entry_source"]),
            tp_source=str(data["tp_source"]),
            volume_spike=bool(data["volume_spike"]),
            window_bounds=(
                None if wb is None else (int(wb["start_index"]), int(wb["end_index"]))
            ),
            ob_meta=(
                None
                if ob_meta is None
                else (float(ob_meta["top"]), float(ob_meta["bottom"]))
            ),
            ob_candidates=tuple(
                OrderBlock.from_v1_dict(c) for c in data.get("ob_candidates", [])
            ),
            selected_ob_index=data.get("selected_ob_index"),
            selected_ob_rank=data.get("selected_ob_rank"),
            selected_ob_top=data.get("selected_ob_top"),
            selected_ob_bottom=data.get("selected_ob_bottom"),
            selected_ob_size=data.get("selected_ob_size"),
            fresh_ob=data.get("fresh_ob"),
            fvg_valid=data.get("fvg_valid"),
            current_price=data.get("current_price"),
            entry_equals_ob_bottom=data.get("entry_equals_ob_bottom"),
        )
