"""ObservableBehavior — the normalized record of everything V1 (or V2) observably
produces for one analysis input.

This is the *unit of comparison*. Behavioral equivalence is defined over these
fields, not over internal implementation: two systems are equivalent for an
input iff they produce an equal `ObservableBehavior` (Roadmap §6 "the captured
V1 outputs *are* the expected outputs"). Every field here maps to an externally
observable result the migration must preserve; nothing here exposes *how* a
value was computed.

The field groups mirror the observable surface the harness must cover:
market snapshot · trend · structure (BOS/CHOCH) · liquidity · order blocks ·
fair value gaps · score · entry · take-profit · stop-loss · kill thesis ·
upgrade thesis · persistence payload · notification payload.

Backend-neutral: the V1 oracle populates these from V1's `result`/`context`
dicts; a future V2 analyzer populates the identical fields from its own domain
objects. The comparison engine never sees either backend — only this record.

`as_field_map()` flattens the record to an ordered `{field_path: leaf}` mapping;
that mapping is the single seam the comparator and the golden files use, so
"PASS/FAIL per observable field" is well-defined even for the nested payloads.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ObservableBehavior:
    """Immutable snapshot of one input's observable analysis outcome."""

    # -- market snapshot --
    symbol: str
    h1_last_ts: float
    live_price: float
    d1_rows: int
    h1_rows: int
    m5_rows: Optional[int]
    atr_last: float

    # -- trend (top-down HTF read + weighted direction) --
    weekly_bias: Optional[str]
    d1_bias: Optional[str]
    h4_bias: Optional[str]
    location: Optional[str]
    regime: Optional[str]
    direction: Optional[str]
    direction_source: Optional[str]
    bull: Optional[float]
    bear: Optional[float]
    anchor_override: Optional[bool]
    veto_reason: Optional[str]

    # -- structure (BOS/CHOCH) + liquidity --
    sweep_valid: Optional[bool]          # liquidity sweep
    displacement_valid: Optional[bool]   # H1 displacement / break of structure
    fvg_valid: Optional[bool]            # fair value gap
    fresh_ob: Optional[bool]             # order block present & fresh
    m5_choch: Optional[bool]             # M5 change-of-character confirmation

    # -- order block selection --
    ob_selected_index: Optional[int]
    ob_selected_top: Optional[float]
    ob_selected_bottom: Optional[float]
    ob_candidate_count: int

    # -- entry --
    entry_mode: Optional[str]
    entry_ready: Optional[bool]
    entry_source: Optional[str]
    entry_price: float

    # -- take-profit / stop-loss / risk --
    sl: float
    tp: Optional[float]
    tp_source: Optional[str]
    tp_rr: Optional[float]
    rr: Optional[float]

    # -- score / decision --
    score: int
    decision: str
    reason: Optional[str]
    notes: tuple[str, ...]

    # -- upgrade thesis (alert tier that drives new-vs-upgrade routing) --
    alert_tier: str
    alert_rank: int

    # -- kill thesis (only evaluated when a prior signal is supplied) --
    kill_triggered: Optional[bool]

    # -- derived payloads (the fresh-cycle projection; see oracle docs) --
    persistence_payload: Optional[dict[str, Any]] = None
    notification: Optional[dict[str, Any]] = None

    def as_field_map(self) -> dict[str, Any]:
        """Flatten to an ordered ``{path: leaf}`` mapping for field-level diffs.

        Dict and list/tuple values expand into dotted / indexed sub-paths so the
        nested persistence and notification payloads compare field-by-field.
        NaN leaves collapse to None (JSON-safe and comparison-safe; an observable
        should never legitimately carry NaN).
        """
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            _flatten(f.name, getattr(self, f.name), out)
        return out


def _flatten(path: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        if not value:
            out[path] = {}
            return
        for key in sorted(value):
            _flatten(f"{path}.{key}", value[key], out)
    elif isinstance(value, (list, tuple)):
        if not value:
            out[path] = []
            return
        for idx, item in enumerate(value):
            _flatten(f"{path}.{idx}", item, out)
    else:
        if isinstance(value, float) and math.isnan(value):
            out[path] = None
        else:
            out[path] = value
