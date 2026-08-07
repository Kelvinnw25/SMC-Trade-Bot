"""app.ledger — assemble the Signal Ledger record from analysis outputs.

Phase 7.1 (Evaluation Infrastructure). The ledger is the permanent, append-only
evaluation dataset: one record per analyzed symbol per cycle, for EVERY decision
tier (A+/VALID/WATCH/SKIP). It is written by the orchestrator after a completed
analysis and is never read back into any runtime decision (an observability
contract — schema in `domain`, sink in `infra`; see `domain.signal_ledger`).

This module ONLY reads already-computed domain objects (the `Signal` + its
candidate, the `MarketSnapshot`, and the pre-M5 `AnalysisContext`) and copies them
into an immutable `SignalLedgerRecord`. It performs no analysis, no scoring, no
recomputation — nothing here can change a trading decision.

HTF source: when the signal has a candidate (scored / not-ready) the HTF is the
candidate's; when it does not (invalid-input / no_direction / veto SKIPs) the HTF
is taken from the pre-M5 `context.htf` — the same reading V1 exposed for those
tiers, and the same source the orchestrator uses for the kill/stale predicate.
"""
from __future__ import annotations

from typing import Any, Optional

from v2.domain import MarketSnapshot, Signal
from v2.domain.signal_ledger import LEDGER_SCHEMA_VERSION, SignalLedgerRecord

__all__ = ["assemble_ledger_record", "STRATEGY_VERSION", "CONTRACT_VERSION"]

# The REPLAY_CONTRACT.md version whose semantics were in force when a record was
# produced (C-16.1: this module owns the constant). Bump it only when the contract
# version bumps — a row's outcome semantics are only meaningful against its
# contract, so rows state it themselves rather than inheriting it from a sibling
# file. The domain default is None so that pre-schema-2 lines are not mislabeled.
CONTRACT_VERSION = "1.1"

# The strategy code version that produced a record. Manually maintained: bump it
# whenever analysis/lifecycle *behavior* changes (Phase 9+) AND on metadata fixes
# that change recorded values, so a future analysis can segment the dataset by
# "which code". History:
#   2.0.0 — the frozen V2 baseline (behavior the migration reproduced from V1).
#   2.0.1 — BUG-1 + BUG-2 metadata fixes (continuation tp_source/tp_rr published;
#           OB-audit selected-OB/window/location/current_price populated). No
#           decision/score/level/lifecycle change.
#   2.1.0 — Phase 0 Universe Freeze. The live universe filter moved from the
#           symbol-name test to the exchange-declared rule in
#           `v2/domain/universe.py` (PERPETUAL / COIN / USDT), removing 150 of
#           851 instruments — tokenized equities, index and commodity products —
#           that the name test admitted. This changes which symbols are scanned
#           and therefore which signals exist, so it is a behaviour change, not
#           a metadata fix. See PHASE0_FREEZE.md.
#   2.2.0 — Finding A1: replay's C-6.9 universe reconstruction ranked delisted
#           instruments on frozen volume, because `KlineRepository.window` is
#           row-based (C-6.3) and kept returning a dead symbol's final 24 candles
#           at every later `T`. `get_top_volume_symbols` now also requires the
#           newest of those rows to open inside the trailing day. Like 2.1.0 this
#           changes which symbols are scanned and therefore which signals exist.
#           **The live path is unchanged** — live ranks from `/fapi/v1/ticker/24hr`
#           and never saw a delisted instrument — so this bump records a change to
#           the symbol set the strategy is *evaluated over*, not to the strategy.
#           It is nonetheless required here: `ReplayConfig.core()` is C-16.2's
#           closed list and carries no harness version, so without it two runs
#           either side of the fix would share a `run_id` and look comparable
#           while differing (C-5.7.2).
#
#           **Authorized exception to C-16.3.1.** That clause sanctions `v2/`
#           production edits only for the M1 lineage set (its enumerated items
#           1-4) and the C-8.5 performance work, and IMPLEMENTATION_PLAN's
#           constraint table restates it as "Step 5 is the only step that may
#           touch `v2/`". This bump is neither, so it is a deviation — approved by
#           the user on 2026-08-03, recorded here, in that constraint table, and
#           in PROJECT_STATE.md rather than passed silently. The scope of the
#           exception is exactly this line and its history comment: no behaviour,
#           signature or field in `v2/` changes with it.
#
# This is the single definition. `backtest/lineage.py` imports it rather than
# declaring its own: it previously declared `"v2.0"` while ledger rows carried
# `"2.0.1"`, so the run manifest and the rows inside it disagreed about which
# code produced them.
STRATEGY_VERSION = "2.2.0"


def assemble_ledger_record(
    snapshot: MarketSnapshot,
    context: Any,
    signal: Signal,
    *,
    session_active: bool,
    recorded_at: float,
    source: str = "live",
    run_id: Optional[str] = None,
    cycle_index: Optional[int] = None,
    universe_rank: Optional[int] = None,
    contract_version: Optional[str] = CONTRACT_VERSION,
    data_snapshot_id: Optional[str] = None,
    strategy_version: str = STRATEGY_VERSION,
    schema_version: int = LEDGER_SCHEMA_VERSION,
) -> SignalLedgerRecord:
    """Build the ledger record for one completed analysis.

    ``context`` is the pre-M5 `AnalysisContext` (only ``.htf`` is read, and only
    as the fallback when the signal has no candidate).

    The lineage keywords (schema 2, C-16.1) are supplied by the caller — the
    orchestrator, which received them as constructor arguments — and are copied
    verbatim. This module derives only the three window-shape counts, straight from
    the snapshot it already holds; it computes nothing else and reads no clock.
    """
    candidate = signal.candidate
    htf = candidate.htf if candidate is not None else getattr(context, "htf", None)
    entry = candidate.entry if candidate is not None else None
    risk = candidate.risk if candidate is not None else None
    sweep = candidate.sweep if candidate is not None else None

    selected = None
    ob_candidate_count: Optional[int] = None
    if candidate is not None:
        ob_candidate_count = len(candidate.order_blocks)
        selected = next(
            (b for b in candidate.order_blocks if b.is_selected), None
        )

    return SignalLedgerRecord(
        # provenance
        schema_version=schema_version,
        strategy_version=strategy_version,
        recorded_at=float(recorded_at),
        symbol=snapshot.symbol,
        h1_last_ts=float(snapshot.h1_last_ts),
        # decision
        decision=str(signal.decision),
        score=int(signal.score),
        entry_mode=str(signal.entry_mode),
        regime=str(signal.regime),
        session_active=bool(session_active),
        live_price=float(snapshot.live_price),
        direction=signal.direction,
        reason=signal.reason,
        notes=tuple(signal.notes),
        m5_choch=signal.m5_choch,
        m5_reason=signal.m5_reason,
        # HTF
        weekly_bias=htf.weekly_bias if htf is not None else None,
        d1_bias=htf.d1_bias if htf is not None else None,
        h4_bias=htf.h4_bias if htf is not None else None,
        location=htf.location if htf is not None else None,
        momentum_weakening=htf.momentum_weakening if htf is not None else None,
        direction_source=htf.direction_source if htf is not None else None,
        htf_bull=float(htf.bull) if htf is not None else None,
        htf_bear=float(htf.bear) if htf is not None else None,
        anchor_override=htf.anchor_override if htf is not None else None,
        veto_active=htf.veto_active if htf is not None else None,
        veto_reason=htf.veto_reason if htf is not None else None,
        # confirmations (candidate-only; None for early-exit SKIPs)
        sweep=candidate.sweep_valid if candidate is not None else None,
        fvg=candidate.fvg_valid if candidate is not None else None,
        displacement=candidate.displacement_valid if candidate is not None else None,
        fresh_ob=candidate.fresh_ob if candidate is not None else None,
        volume_spike=candidate.volume_spike if candidate is not None else None,
        target_clear=candidate.target_clear if candidate is not None else None,
        htf_context_clean=candidate.htf_context_clean if candidate is not None else None,
        # levels & risk
        entry_ready=entry.entry_ready if entry is not None else None,
        entry_price=float(entry.entry_price) if entry is not None else None,
        sl=float(risk.sl) if risk is not None else None,
        tp=float(risk.tp) if (risk is not None and risk.tp is not None) else None,
        entry_source=entry.entry_source if entry is not None else None,
        tp_source=risk.tp_source if risk is not None else None,
        rr=risk.rr if risk is not None else None,
        tp_rr=risk.tp_rr if risk is not None else None,
        # selected OB + sweep quality
        selected_ob_top=selected.top if selected is not None else None,
        selected_ob_bottom=selected.bottom if selected is not None else None,
        selected_ob_index=selected.index if selected is not None else None,
        selected_ob_rank=selected.rank_from_last if selected is not None else None,
        selected_ob_fresh=selected.fresh_status if selected is not None else None,
        ob_candidate_count=ob_candidate_count,
        sweep_reclaim_strength=(
            float(sweep.reclaim_strength)
            if (sweep is not None and sweep.reclaim_strength is not None)
            else None
        ),
        # market context
        atr_last=float(candidate.atr_last) if candidate is not None else None,
        # lineage (schema 2, C-16.1) — copied from the caller; the window shapes
        # are the served frames' row counts (m5 absent -> 0, distinguishing "no M5
        # attached" from "attached but the score gate did not fire").
        source=str(source),
        run_id=run_id,
        cycle_index=cycle_index,
        universe_rank=universe_rank,
        contract_version=contract_version,
        data_snapshot_id=data_snapshot_id,
        d1_rows=len(snapshot.d1),
        h1_rows=len(snapshot.h1),
        m5_rows=0 if snapshot.m5 is None else len(snapshot.m5),
    )
