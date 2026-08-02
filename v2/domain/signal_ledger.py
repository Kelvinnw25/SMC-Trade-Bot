"""SignalLedgerRecord — the evaluation ledger schema (observability contract).

Phase 7.1 (Evaluation Infrastructure). This is an OBSERVABILITY CONTRACT, a
sibling of `OBAuditRecord` and `LogEvent` — NOT a peer of `Signal`. It is the
permanent, append-only dataset every future strategy evaluation reads from.

HARD CONSTRAINTS (same discipline as `OBAuditRecord`, Decision #010):
- No ``analysis.*`` or ``lifecycle.*`` module may consume, import, or condition
  on this object. At runtime it is WRITE-ONLY; reading it back into any trading
  decision would be a behavior change and is out of scope.
- Assembled by ``app`` (`app.ledger.assemble_ledger_record`) from already-computed
  domain objects — no re-analysis, no recomputation.
- Schema owned by ``domain`` (this module); executed by ``infra`` as an
  append-only JSONL sink (``signal_ledger.jsonl``).

DIFFERENCES from `OBAuditRecord`:
- It records EVERY completed analysis — one record per analyzed symbol per cycle,
  for all decision tiers (A+/VALID/WATCH/SKIP), not only eligible retracements.
- It is intended to be READ BACK OFFLINE by evaluation tooling (hence
  ``from_jsonl_dict``); the runtime still never reads it.

## Schema stability

This dataset will be accumulated over months/years, so the schema is versioned
(``schema_version``) and every record also carries the ``strategy_version`` that
produced it — the two keys that let a future analysis segment records by "which
code" and "which schema" without guessing. Fields are only ever ADDED (new
optional fields, ``schema_version`` bumped); existing fields are never repurposed,
so old lines stay readable forever.

Version history:
- **1** — the Phase-7.1 baseline schema.
- **2** — REPLAY_CONTRACT.md C-16.1 (Milestone M1): the nine **lineage** fields
  (``source``, ``run_id``, ``cycle_index``, ``universe_rank``,
  ``contract_version``, ``data_snapshot_id``, ``d1_rows``, ``h1_rows``,
  ``m5_rows``). Purely additive; every schema-1 field keeps its meaning, so a
  schema-1 line still parses (``source`` -> "live", the other eight -> None) and a
  schema-1 reader still tolerates a schema-2 line (C-16.5).

## Why the lineage fields exist (C-16.1)

Without them a live row and a replay row are structurally indistinguishable, so a
merged file or a mis-pointed base directory silently corrupts a dataset that is
meant to last years — and lineage cannot be reconstructed after the fact.
``source``/``run_id`` identify the execution, ``cycle_index``/``universe_rank``
restore the intra-cycle ordering the lifecycle depends on (the processing order
decides the slot cap and REPLACE outcome — C-5.6.2), ``contract_version`` and
``data_snapshot_id`` pin the semantics and the exact klines a row was produced
from, and ``d1_rows``/``h1_rows``/``m5_rows`` record the served window shape, which
is part of the strategy's definition (C-6.5).

## Why NO outcome field

WIN / LOSS / BE / EXPIRED / NOT_FILLED are NOT stored here. Outcomes are not known
at analysis time; they are derived LATER by walking forward price against
``entry_price``/``sl``/``tp``/``direction`` (from ``W0``, the candle opening at the
scan boundary — REPLAY_CONTRACT.md C-4.0) and joined back to these records by the
**canonical row** rule of C-9.4: ``(run_id, symbol, h1_last_ts)``, disambiguated
for live data by ``cycle_index``. Keeping the ledger a pure record of *what the
strategy decided* (never mutated post-write) is what makes it a trustworthy
append-only source; the labeled dataset is a separate, reproducible derivation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Bump when a field is ADDED (never when one is repurposed — that is forbidden).
LEDGER_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class SignalLedgerRecord:
    """One completed analysis result, as one JSONL line.

    Field groups (see module docstring for the evaluation rationale):
    provenance · decision · HTF context · confirmations · levels & risk ·
    selected order block · market context.
    """

    # -- provenance / identity (required) --
    schema_version: int
    strategy_version: str
    recorded_at: float          # wall-clock epoch of the write (injected clock)
    symbol: str
    h1_last_ts: float           # the closed-H1 reference candle (analysis time)

    # -- decision (required) --
    decision: str               # A+ / VALID / WATCH / SKIP
    score: int
    entry_mode: str             # CONTINUATION / RETRACEMENT / WATCH_ONLY / NONE
    regime: str

    # -- market context always known (required) --
    session_active: bool
    live_price: float

    # -- decision detail (optional) --
    direction: Optional[str] = None
    reason: Optional[str] = None
    notes: Tuple[str, ...] = ()
    m5_choch: Optional[bool] = None
    m5_reason: Optional[str] = None

    # -- HTF context --
    weekly_bias: Optional[str] = None
    d1_bias: Optional[str] = None
    h4_bias: Optional[str] = None
    location: Optional[str] = None
    momentum_weakening: Optional[bool] = None
    direction_source: Optional[str] = None
    htf_bull: Optional[float] = None
    htf_bear: Optional[float] = None
    anchor_override: Optional[bool] = None
    veto_active: Optional[bool] = None
    veto_reason: Optional[str] = None

    # -- confirmations (the score inputs) --
    sweep: Optional[bool] = None
    fvg: Optional[bool] = None
    displacement: Optional[bool] = None
    fresh_ob: Optional[bool] = None
    volume_spike: Optional[bool] = None
    target_clear: Optional[bool] = None
    htf_context_clean: Optional[bool] = None

    # -- levels & risk --
    entry_ready: Optional[bool] = None
    entry_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    entry_source: Optional[str] = None
    tp_source: Optional[str] = None
    rr: Optional[float] = None          # scoring-side RR (score_signal)
    tp_rr: Optional[float] = None       # target-side RR (find_liquidity_target)

    # -- selected order block + sweep quality (OB/liquidity evaluation) --
    selected_ob_top: Optional[float] = None
    selected_ob_bottom: Optional[float] = None
    selected_ob_index: Optional[int] = None
    selected_ob_rank: Optional[int] = None
    selected_ob_fresh: Optional[bool] = None
    ob_candidate_count: Optional[int] = None
    sweep_reclaim_strength: Optional[float] = None

    # -- market context --
    atr_last: Optional[float] = None

    # -- lineage (schema 2; REPLAY_CONTRACT.md C-16.1) ----------------------
    # Written by `app.ledger` from values injected into the orchestrator; never
    # derived from market data and never read by any runtime branch.
    #: "live" | "replay". Defaults to "live" because only live could have written
    #: a schema-1 row, so legacy lines are labeled correctly on load (C-16.5).
    source: str = "live"
    #: One execution. Live: "live-<iso8601>-<pid>"; replay: a content hash of the
    #: run's determinants (C-16.2). None only when no run identity was supplied.
    run_id: Optional[str] = None
    #: Monotonic cycle counter within the run; groups the rows of one scan cycle.
    cycle_index: Optional[int] = None
    #: 1-based rank of the symbol in this cycle's universe = its processing order.
    universe_rank: Optional[int] = None
    #: The REPLAY_CONTRACT version whose semantics were in force ("1.1").
    contract_version: Optional[str] = None
    #: Content identity of the kline repository (replay only; None for live).
    data_snapshot_id: Optional[str] = None
    #: Served window shapes — the analysis input's row counts (C-6.5).
    d1_rows: Optional[int] = None
    h1_rows: Optional[int] = None
    m5_rows: Optional[int] = None

    def to_jsonl_dict(self) -> Dict[str, Any]:
        """The exact dict written as one JSONL line. Stable key order; ``notes``
        as a list. Only JSON-native types (str/int/float/bool/None/list).

        Schema-2 keys are APPENDED after the schema-1 keys (never interleaved), so
        a diff of two lines stays readable and a schema-1 reader that only looks up
        the keys it knows is unaffected (C-16.1).
        """
        return {
            "schema_version": self.schema_version,
            "strategy_version": self.strategy_version,
            "recorded_at": self.recorded_at,
            "symbol": self.symbol,
            "h1_last_ts": self.h1_last_ts,
            "decision": self.decision,
            "score": self.score,
            "entry_mode": self.entry_mode,
            "regime": self.regime,
            "session_active": self.session_active,
            "live_price": self.live_price,
            "direction": self.direction,
            "reason": self.reason,
            "notes": list(self.notes),
            "m5_choch": self.m5_choch,
            "m5_reason": self.m5_reason,
            "weekly_bias": self.weekly_bias,
            "d1_bias": self.d1_bias,
            "h4_bias": self.h4_bias,
            "location": self.location,
            "momentum_weakening": self.momentum_weakening,
            "direction_source": self.direction_source,
            "htf_bull": self.htf_bull,
            "htf_bear": self.htf_bear,
            "anchor_override": self.anchor_override,
            "veto_active": self.veto_active,
            "veto_reason": self.veto_reason,
            "sweep": self.sweep,
            "fvg": self.fvg,
            "displacement": self.displacement,
            "fresh_ob": self.fresh_ob,
            "volume_spike": self.volume_spike,
            "target_clear": self.target_clear,
            "htf_context_clean": self.htf_context_clean,
            "entry_ready": self.entry_ready,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "entry_source": self.entry_source,
            "tp_source": self.tp_source,
            "rr": self.rr,
            "tp_rr": self.tp_rr,
            "selected_ob_top": self.selected_ob_top,
            "selected_ob_bottom": self.selected_ob_bottom,
            "selected_ob_index": self.selected_ob_index,
            "selected_ob_rank": self.selected_ob_rank,
            "selected_ob_fresh": self.selected_ob_fresh,
            "ob_candidate_count": self.ob_candidate_count,
            "sweep_reclaim_strength": self.sweep_reclaim_strength,
            "atr_last": self.atr_last,
            # -- schema 2: lineage (appended; C-16.1) --
            "source": self.source,
            "run_id": self.run_id,
            "cycle_index": self.cycle_index,
            "universe_rank": self.universe_rank,
            "contract_version": self.contract_version,
            "data_snapshot_id": self.data_snapshot_id,
            "d1_rows": self.d1_rows,
            "h1_rows": self.h1_rows,
            "m5_rows": self.m5_rows,
        }

    @classmethod
    def from_jsonl_dict(cls, data: Dict[str, Any]) -> "SignalLedgerRecord":
        """Parse one JSONL line (already json-decoded) back into a record, for
        OFFLINE evaluation tooling. Tolerant of older lines: any field absent in
        an earlier ``schema_version`` falls back to the dataclass default — so a
        schema-1 line yields ``source == "live"`` and the other eight lineage
        fields ``None`` (C-16.5). Unknown keys from a *newer* schema are ignored,
        which is what makes the format forward-compatible too."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs: Dict[str, Any] = {k: v for k, v in data.items() if k in known}
        if "notes" in kwargs and kwargs["notes"] is not None:
            kwargs["notes"] = tuple(kwargs["notes"])
        return cls(**kwargs)
