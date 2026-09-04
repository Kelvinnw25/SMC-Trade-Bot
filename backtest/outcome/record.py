"""`OutcomeRecord` — one immutable line per trade (C-9.4).

Every field is one the contract lists, in the five groups it names: identity,
frozen levels, fill, exit, path, lineage. There are **no mutable fields and no
derived metrics** — expectancy, win rate, drawdown and every other aggregate
belong to the metrics engine, which does not exist yet.

Two labels the contract defines as *derived* are exposed as properties rather
than stored, because storing them would make a reporting convention part of the
data: `is_open` (data ended while `PENDING`/`ACTIVE` — C-3.1) and `is_breakeven`
(`|realized_r| < 0.1` — C-3.1).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from backtest.outcome.trade import STATE_ACTIVE, STATE_PENDING, TERMINAL_STATES

__all__ = ["OUTCOME_SCHEMA_VERSION", "OutcomeRecord"]

#: Bumped only when the record's own schema changes incompatibly. Independent of
#: the ledger's `schema_version`: outcomes are a re-derivable projection and may
#: be regenerated whenever the labeling rules improve (C-9.5).
#:
#: **2 (M11):** `burn_in_h1` and `universe_mode` added to the lineage group so a
#: record carries all five of C-5.7.2's comparison preconditions on its own. The
#: addition is *backward* compatible — a schema-1 line parses, both fields
#: defaulting to `None` — but not *forward* compatible, because this repository's
#: reader rejects unknown keys rather than dropping columns silently
#: (`backtest.dataset.read_outcomes`). A strict reader is the right choice for an
#: evaluation dataset, and the cost of that choice is this version bump.
#:
#: **3 (market-fill admissibility):** `tick_size_unavailable` added to the fill
#: group. C-4.14's sub-tick floor cannot be evaluated for a symbol absent from
#: the reference `exchangeInfo` snapshot, and the eleven such symbols in the
#: frozen corpus were delisted before any snapshot exists to hold them. The flag
#: says *the floor did not run here* — as distinct from *the floor ran and the
#: fill passed* — because a reader cannot otherwise tell those apart, and the
#: difference is exactly what a later historical tick source would change.
#: Backward compatible in the same way `burn_in_h1` was: a schema-2 line parses
#: with the field defaulting to `False`.
OUTCOME_SCHEMA_VERSION = 3

#: `|realized_r| < BREAKEVEN_R` is the derived break-even bucket (C-3.1).
BREAKEVEN_R = 0.1


@dataclass(frozen=True)
class OutcomeRecord:
    """The resolved outcome of one trade. Immutable by construction."""

    # -- identity (C-9.4) --------------------------------------------------
    run_id: str
    source: str
    thesis_key: str
    symbol: str
    direction: str
    entry_mode: str
    boundary: int                          # instantiation `T`
    h1_last_ts: int
    cycle_index: Optional[int]
    universe_rank: Optional[int]
    alert_tier: Optional[str]
    shadow_kind: Optional[str]
    in_burn_in: bool

    # -- frozen levels (C-9.4) --------------------------------------------
    entry_price: float
    sl: float
    tp: float
    structural_rr: Optional[float]
    tp_source: Optional[str]
    entry_source: Optional[str]
    score: Optional[int]

    # -- fill (C-9.4) ------------------------------------------------------
    filled: bool
    fill_kind: Optional[str]
    fill_T: Optional[int]
    fill_price: Optional[float]
    candles_to_fill: Optional[int]

    # -- exit (C-9.4) ------------------------------------------------------
    state: str
    cancel_reason: Optional[str]
    exit_reason: Optional[str]
    exit_T: Optional[int]
    exit_price: Optional[float]
    realized_r: Optional[float]
    realized_r_net: Optional[float]
    holding_candles: Optional[int]

    # -- path (C-9.4) ------------------------------------------------------
    mae_r: Optional[float]
    mfe_r: Optional[float]
    excursion_degraded: bool

    # -- lineage (C-9.4) ---------------------------------------------------
    contract_version: str
    strategy_version: str
    ledger_schema_version: int
    outcome_schema_version: int
    outcome_config_hash: str
    data_snapshot_id: str
    env: dict[str, str] = field(default_factory=dict)

    #: The run's lifecycle burn-in, in H1 candles (C-5.7.1), and the universe
    #: policy that decided which symbols were scanned (C-6.9).
    #:
    #: **Why these are lineage and not configuration.** C-5.7.2 names exactly five
    #: preconditions for comparing two runs — `data_snapshot_id`, `T_0`, `T_n`,
    #: `burn_in_h1`, `universe_mode` — and C-16.6.4 makes refusing an incomparable
    #: diff mandatory. The first three were already recoverable from a record
    #: (`data_snapshot_id` directly, the window from `boundary`); these two existed
    #: only in the run manifest, so a comparison could be enforced only by trusting
    #: whoever typed them in. Both change which trades exist and what they are worth
    #: — a different burn-in relabels trades `in_burn_in`, a different universe
    #: policy scans different symbols — which is the definition of lineage here.
    #: Neither is read by any computation: they segment and they gate comparison.
    #:
    #: `burn_in_h1` is additionally an `OutcomeConfig` field (C-9.7) and so already
    #: bound into `outcome_config_hash`; stamping it explicitly makes it *readable*
    #: rather than merely hashed. A hash can prove two runs differ but cannot say
    #: how, and a refusal that cannot name the mismatching value is not actionable.
    #:
    #: Both default to `None` — the honest value for a record written before this
    #: field existed. `None` means "undeclared", never a guessed default, and
    #: `metrics.freeze()` refuses an undeclared comparison key rather than
    #: inventing one (finding N2, M10).
    burn_in_h1: Optional[int] = None
    universe_mode: Optional[str] = None

    #: The ledger's own signal id, carried through so a record joins back to the
    #: row it came from without re-deriving the canonical key of C-9.4.
    signal_id: Optional[str] = None

    #: C-4.14: the symbol was absent from the configured tick reference, so the
    #: sub-tick floor was **not evaluated** for this record. The crossed-stop
    #: guard still ran — it needs no market metadata. `False` on every record
    #: whose floor was evaluated, including every rejected one.
    tick_size_unavailable: bool = False

    # -- derived reporting labels (never stored) ---------------------------

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        """C-3.1: `OPEN` is a reporting label, not a state — data ended while the
        trade was still pending or active. Excluded from expectancy (C-4.10)."""
        return self.state in (STATE_PENDING, STATE_ACTIVE)

    @property
    def is_breakeven(self) -> bool:
        """C-3.1: a derived bucket, not a state. V2 has no break-even logic."""
        return (self.realized_r is not None
                and abs(self.realized_r) < BREAKEVEN_R)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
