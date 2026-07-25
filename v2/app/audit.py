"""app.audit — assemble the retracement OB-audit record from analysis outputs.

Architecture / Decision ref: 05-decision-log.md #010 and
02-software-architecture.md §2 (OBAuditRecord): the audit is an OBSERVABILITY
contract — "created by app.orchestrator … its schema owned by domain … executed
by infra as an append-only JSONL sink … never consumed by analysis or
lifecycle." The eligibility conditional is operational (Architecture Principle
12), and the per-process dedup set is orchestrator-owned transient state (§6).

This module ONLY assembles an immutable ``OBAuditRecord`` from already-computed
domain objects (the `Signal`'s candidate + the `MarketSnapshot`). It performs no
analysis and duplicates no calculation: the OB candidate set, the selected OB,
the entry/risk levels and the confirmation flags are read straight off the
candidate. The one arithmetic it does — the audit ``rr`` = |tp-entry|/|entry-sl|
— is V1's own audit-line formula (main.py:505), a presentation field, not the
scoring RR.

Faithful V1 quirks preserved (verified against main.py:482-524 vs
strategy.py:1784-1796 — the audit reads several keys the strategy never sets
under those names, so they are ALWAYS None):
- ``location``  → "" (V1 reads ``result.get("location","")``; ``result`` never
  carries a "location" key);
- ``window_bounds``, ``selected_ob_index/rank/top/bottom/size``, ``current_price``
  → None (main.py reads ``selected_ob_*`` / ``window_bounds`` / ``current_price``
  but strategy publishes ``ob_index`` / ``ob_top`` / … under different names).
"""
from __future__ import annotations

from typing import Optional

from v2.domain import MarketSnapshot, OBAuditRecord, Signal
from v2.lifecycle.rules import levels_valid

__all__ = ["assemble_ob_audit", "AUDIT_ENTRY_MODE"]

# V1's audit eligibility: RETRACEMENT + entry_source "retracement_ob" + valid
# levels (main.py:484-488). The dedup key's entry_mode is this literal.
AUDIT_ENTRY_MODE = "RETRACEMENT"


def assemble_ob_audit(
    snapshot: MarketSnapshot, signal: Signal
) -> Optional[OBAuditRecord]:
    """Build the OB-audit record for ``signal``, or None if it is not eligible.

    Eligible exactly when V1 would have written a line: a candidate whose entry
    is a RETRACEMENT ``retracement_ob`` with valid levels. Values are the FINAL
    (post-M5) decision/score and the candidate's (M5-invariant) levels/flags —
    matching what V1's ``result`` carried at the audit site.
    """
    candidate = signal.candidate
    if candidate is None:
        return None

    entry = candidate.entry
    if entry.entry_mode != AUDIT_ENTRY_MODE:
        return None
    if str(entry.entry_source).strip() != "retracement_ob":
        return None

    entry_price = float(entry.entry_price)
    sl = float(candidate.risk.sl)
    tp_value = candidate.risk.tp
    tp = float(tp_value) if tp_value is not None else 0.0
    if not levels_valid(entry_price, sl, tp):
        return None

    selected = next((b for b in candidate.order_blocks if b.is_selected), None)
    ob_meta = (selected.top, selected.bottom) if selected is not None else None

    entry_equals_ob_bottom: Optional[bool] = None
    if entry.entry_ready and selected is not None:
        if signal.direction == "SHORT":
            entry_equals_ob_bottom = abs(entry_price - selected.bottom) < 1e-12
        else:
            entry_equals_ob_bottom = abs(entry_price - selected.top) < 1e-12

    risk = abs(entry_price - sl)
    rr = (abs(tp - entry_price) / risk) if risk > 0 else None

    return OBAuditRecord(
        symbol=snapshot.symbol,
        h1_last_ts=float(snapshot.h1_last_ts),
        direction=str(signal.direction) if signal.direction is not None else "NONE",
        entry_mode=AUDIT_ENTRY_MODE,
        decision=str(signal.decision),
        score=signal.score,
        regime=str(signal.regime),
        location="",  # V1 quirk: result.get("location", "") is always ""
        market_price=float(snapshot.live_price),
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        rr=rr,
        entry_source=str(entry.entry_source),
        tp_source=str(candidate.risk.tp_source),
        volume_spike=bool(candidate.volume_spike),
        window_bounds=None,          # V1 quirk (key-name mismatch -> None)
        ob_meta=ob_meta,
        ob_candidates=candidate.order_blocks,
        selected_ob_index=None,      # V1 quirk
        selected_ob_rank=None,       # V1 quirk
        selected_ob_top=None,        # V1 quirk
        selected_ob_bottom=None,     # V1 quirk
        selected_ob_size=None,       # V1 quirk
        fresh_ob=bool(candidate.fresh_ob),
        fvg_valid=bool(candidate.fvg_valid),
        current_price=None,          # V1 quirk
        entry_equals_ob_bottom=entry_equals_ob_bottom,
    )
