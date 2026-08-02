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

BUG-2 fix (post-migration, STRATEGY_NOTES.md / REPLAY_CONTRACT.md §12): V1's
audit read keys the strategy never published under those names, so
``location`` / ``window_bounds`` / ``selected_ob_*`` / ``current_price`` were
ALWAYS empty — a key-name mismatch, not a design. The values were present on
the same call; they are now recorded:
- ``location``       ← the candidate's HTF location (V1 intended
  ``result["location"]``, never set);
- ``selected_ob_*``  ← the selected ``OrderBlock``'s index/rank/top/bottom/size;
- ``window_bounds``  ← the OB scan window (``candidate_window`` over the same
  prepared frame the OB stage scanned — the identical pure helpers, re-applied
  to the immutable snapshot, not a re-analysis);
- ``current_price``  ← the OB-filter reference price (the last CLOSED H1 close,
  ``order_block.last_close`` — what selection actually reasoned against),
  distinct from ``market_price`` (the live forming-candle close).
The record remains write-only observability (Decision #010): no runtime module
reads it back, so populating it cannot change any trading decision.
"""
from __future__ import annotations

from typing import Optional, Tuple

from v2.analysis.frames import H1_MIN_ROWS, safe_frame
from v2.analysis.order_block import candidate_window, last_close
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

    # BUG-2: the OB scan window and its reference price, recovered with the
    # same pure helpers the OB stage used on the same immutable snapshot.
    window_bounds: Optional[Tuple[int, int]] = None
    current_price: Optional[float] = None
    prepared_h1 = safe_frame(snapshot.h1, H1_MIN_ROWS)
    if prepared_h1 is not None:
        window_bounds = candidate_window(len(prepared_h1) - 1)
        current_price = last_close(prepared_h1)

    return OBAuditRecord(
        symbol=snapshot.symbol,
        h1_last_ts=float(snapshot.h1_last_ts),
        direction=str(signal.direction) if signal.direction is not None else "NONE",
        entry_mode=AUDIT_ENTRY_MODE,
        decision=str(signal.decision),
        score=signal.score,
        regime=str(signal.regime),
        location=str(candidate.htf.location),
        market_price=float(snapshot.live_price),
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        rr=rr,
        entry_source=str(entry.entry_source),
        tp_source=str(candidate.risk.tp_source),
        volume_spike=bool(candidate.volume_spike),
        window_bounds=window_bounds,
        ob_meta=ob_meta,
        ob_candidates=candidate.order_blocks,
        selected_ob_index=selected.index if selected is not None else None,
        selected_ob_rank=selected.rank_from_last if selected is not None else None,
        selected_ob_top=selected.top if selected is not None else None,
        selected_ob_bottom=selected.bottom if selected is not None else None,
        selected_ob_size=selected.size if selected is not None else None,
        fresh_ob=bool(candidate.fresh_ob),
        fvg_valid=bool(candidate.fvg_valid),
        current_price=current_price,
        entry_equals_ob_bottom=entry_equals_ob_bottom,
    )
