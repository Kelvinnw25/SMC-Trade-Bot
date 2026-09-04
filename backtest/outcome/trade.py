"""The trade vocabulary of §3, and the Outcome Engine's input boundary.

## The state vocabulary is closed

C-3.1 defines it exhaustively. Nothing here invents a state, and two labels the
contract explicitly refuses are absent by design:

- **`OPEN` is not a state**, it is a reporting label for "data ended while the
  trade was still `PENDING` or `ACTIVE`" (C-3.1). So the record keeps the real
  state and `OutcomeRecord.is_open` derives the label. Encoding it as a state
  would make a non-terminal trade look terminal.
- **`BREAKEVEN` is not a state** — V2 has no break-even logic. It is a derived
  reporting bucket, `|realized_r| < 0.1` (C-3.1), exposed as a property.
- **`PARTIAL` does not exist** — V2 has no partial exits.

## `TradeIntent` — why the engine takes trades rather than finding them

C-9.2 builds trades from the **captured `LifecycleDecision` stream (§15)**, and
C-2.1 makes that capture the source of truth, explicitly *not* the notifier
stream. The capture layer landed in M9 and `backtest.outcome.builder` populates
this boundary from it — but the boundary itself remains, because it is what keeps
"evaluate a trade" separable from "discover which trades existed". The live-data
path of C-9.1 reconstructs its trades a different way and will populate the same
boundary without touching the walker.

A `TradeIntent` is exactly the projection of a lifecycle decision that a price
walk needs: what was frozen at instantiation (C-2.1), and the lifecycle events
that would end it before or during the walk (C-4.8/C-4.9). It holds no analysis,
no scoring input and no market data — the engine reads prices from the repository
itself (C-9.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backtest.outcome.errors import TradeIntentError

__all__ = [
    "CANCEL_REASONS",
    "Cancellation",
    "DIRECTIONS",
    "ENTRY_MODES",
    "EXIT_REASONS",
    "FILL_KINDS",
    "FILL_REJECT_REASONS",
    "NOT_FILLED_REASONS",
    "REJECT_STOP_REACHED_AT_FILL",
    "REJECT_SUB_TICK_RISK",
    "SHADOW_KINDS",
    "STATE_ACTIVE",
    "STATE_EXPIRED",
    "STATE_INVALIDATED_EXIT",
    "STATE_INVALID_LEVELS",
    "STATE_LOSS",
    "STATE_NOT_FILLED",
    "STATE_PENDING",
    "STATE_WIN",
    "TERMINAL_STATES",
    "TradeIntent",
    "thesis_key",
]

# -- states (C-3.1) ---------------------------------------------------------

STATE_PENDING = "PENDING"
STATE_ACTIVE = "ACTIVE"
STATE_WIN = "WIN"
STATE_LOSS = "LOSS"
STATE_NOT_FILLED = "NOT_FILLED"
STATE_INVALIDATED_EXIT = "INVALIDATED_EXIT"
STATE_EXPIRED = "EXPIRED"
STATE_INVALID_LEVELS = "INVALID_LEVELS"

#: Exactly the terminal states of C-3.1. `PENDING` and `ACTIVE` are not terminal
#: even when data ends — that is the `OPEN` reporting label.
TERMINAL_STATES = frozenset({
    STATE_WIN, STATE_LOSS, STATE_NOT_FILLED, STATE_INVALIDATED_EXIT,
    STATE_EXPIRED, STATE_INVALID_LEVELS,
})

#: C-3.1: the lifecycle events that can end a *pending* trade (C-4.8). A
#: `Cancellation` must name one of these; they all originate outside the walk.
CANCEL_REASONS = frozenset({
    "KILL", "DECAY", "TTL", "REPLACED", "SUPERSEDED", "SHADOW_WINDOW",
})

#: C-4.1 / C-4.14: the walk's own refusals to fill. Categorically **not**
#: cancellations — no lifecycle event occurred; the order was never placeable at
#: the execution instant. They share `NOT_FILLED` and the `cancel_reason` column
#: because that is where the contract records "why this decision never became a
#: trade", and they are kept distinct from each other so a study can segment the
#: crossed stop from the unrepresentable one.
#:
#: `INVALID_LEVELS` is deliberately not reused: the levels are internally sound
#: in both cases (C-4.14 continues to own genuinely impossible geometry), and
#: what fails is admissibility of the *fill*, not of the decision.
REJECT_STOP_REACHED_AT_FILL = "STOP_REACHED_AT_FILL"
REJECT_SUB_TICK_RISK = "SUB_TICK_RISK"

FILL_REJECT_REASONS = frozenset({
    REJECT_STOP_REACHED_AT_FILL, REJECT_SUB_TICK_RISK,
})

#: Everything `NOT_FILLED` can carry, from either origin.
NOT_FILLED_REASONS = CANCEL_REASONS | FILL_REJECT_REASONS

#: C-4.1 / C-4.2.
FILL_KINDS = frozenset({
    "LIMIT_TOUCH", "LIMIT_GAP", "IMMEDIATE", "MARKET_WALK_ORIGIN",
})

#: The contract names `SL_WORST_CASE_M5` and `SL_WORST_CASE_NO_M5` (C-4.3) and
#: leaves the rest unnamed; the remainder is this implementation's fixed
#: vocabulary, one value per distinct mechanism in §4. See finding O4.
EXIT_REASONS = frozenset({
    "TP",                     # TP touched, exit at tp (C-4.3)
    "SL",                     # SL touched, exit at sl (C-4.3)
    "GAP_TP",                 # candle opened through TP, exit at open (C-4.5)
    "GAP_SL",                 # candle opened through SL, exit at open (C-4.5)
    "SL_WORST_CASE_M5",       # both hit inside one M5 candle (C-4.3 rule 3)
    "SL_WORST_CASE_NO_M5",    # both hit in H1, M5 set incomplete (C-4.3 rule 4)
    "HTF_KILL",               # HTF-flip kill while ACTIVE (C-4.9)
    "MAX_HOLDING",            # holding cap reached (C-4.10)
    "END_OF_DATA",            # data ended while PENDING or ACTIVE (C-4.10)
})

#: C-2.2. `None` is the alert population — the only one in headline metrics.
SHADOW_KINDS = frozenset({"UNALERTED_INSERT", "CAPPED", "KILL_COOLDOWN"})

#: The two shadow classes whose pending lifetime is a fixed window rather than
#: the real lifecycle, because no memory record exists to evict (C-2.2).
FIXED_WINDOW_SHADOWS = frozenset({"CAPPED", "KILL_COOLDOWN"})

DIRECTIONS = frozenset({"LONG", "SHORT"})
ENTRY_MODES = frozenset({"RETRACEMENT", "CONTINUATION"})


def thesis_key(symbol: str, direction: str, entry_mode: str) -> str:
    """`symbol|direction|entry_mode`, mirroring the lifecycle's own key
    ([killed_thesis.py:36](v2/domain/killed_thesis.py#L36)). At most one
    non-terminal trade exists per thesis (C-2.3)."""
    return f"{symbol}|{direction}|{entry_mode}"


# -- lifecycle interventions ------------------------------------------------

@dataclass(frozen=True)
class Cancellation:
    """A lifecycle event that ends a **pending** trade (C-4.8).

    `boundary` is the scan boundary `T_c` of the cycle that produced the event.
    C-4.13 fixes what that means for the walk: intra-candle price events precede
    the close-scan lifecycle events, so a fill in the candle that *closes* at
    `T_c` beats this cancellation. The walk therefore evaluates every candle with
    `open_time + 3600 <= T_c` before applying it.
    """

    boundary: int
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in CANCEL_REASONS:
            raise TradeIntentError(
                f"cancel reason {self.reason!r} is not one of "
                f"{sorted(CANCEL_REASONS)} (C-3.1)")
        if self.boundary % 3600 != 0:
            raise TradeIntentError(
                f"cancellation boundary {self.boundary} is not a scan boundary "
                f"(T mod 3600 == 0, C-5.1)")


@dataclass(frozen=True)
class TradeIntent:
    """One trade as frozen at instantiation (C-2.1), plus its lifecycle end.

    Every field is either frozen by C-2.1, required by `OutcomeRecord` (C-9.4),
    or a lifecycle intervention the walk must honour (C-4.8/C-4.9). Nothing here
    is derived from prices — the engine reads those itself.
    """

    # identity
    run_id: str
    source: str                       # "replay" | "live"
    symbol: str                       # live format, e.g. "BTC/USDT"
    direction: str
    entry_mode: str
    boundary: int                     # instantiation scan boundary `T`
    h1_last_ts: int                   # open time of `C_ref` (C-5.3)
    cycle_index: Optional[int] = None
    universe_rank: Optional[int] = None
    alert_tier: Optional[str] = None  # VALID | A+ | ... ; None for shadows
    shadow_kind: Optional[str] = None
    in_burn_in: bool = False
    signal_id: Optional[str] = None   # the ledger's own id, for joining

    # frozen levels
    entry_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    score: Optional[int] = None
    tp_source: Optional[str] = None
    entry_source: Optional[str] = None

    # lifecycle end
    cancellation: Optional[Cancellation] = None
    htf_kill_boundary: Optional[int] = None

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise TradeIntentError(
                f"direction {self.direction!r} must be one of "
                f"{sorted(DIRECTIONS)}")
        if self.entry_mode not in ENTRY_MODES:
            raise TradeIntentError(
                f"entry_mode {self.entry_mode!r} must be one of "
                f"{sorted(ENTRY_MODES)}; only these two have a fill rule "
                f"(C-4.1/C-4.2)")
        if self.boundary % 3600 != 0:
            raise TradeIntentError(
                f"instantiation boundary {self.boundary} is not a scan boundary "
                f"(T mod 3600 == 0, C-5.1)")
        if self.h1_last_ts % 3600 != 0:
            raise TradeIntentError(
                f"h1_last_ts {self.h1_last_ts} is not an H1 open time")
        if self.h1_last_ts >= self.boundary:
            raise TradeIntentError(
                f"h1_last_ts {self.h1_last_ts} must precede the boundary "
                f"{self.boundary}: it is the OPEN time of C_ref (C-5.3)")
        if self.shadow_kind is not None and self.shadow_kind not in SHADOW_KINDS:
            raise TradeIntentError(
                f"shadow_kind {self.shadow_kind!r} must be one of "
                f"{sorted(SHADOW_KINDS)} or None (C-2.2)")
        if self.htf_kill_boundary is not None:
            if self.htf_kill_boundary % 3600 != 0:
                raise TradeIntentError(
                    f"htf_kill_boundary {self.htf_kill_boundary} is not a scan "
                    f"boundary")
            if self.htf_kill_boundary < self.boundary:
                raise TradeIntentError(
                    f"htf_kill_boundary {self.htf_kill_boundary} precedes "
                    f"instantiation {self.boundary}")
        if (self.cancellation is not None
                and self.cancellation.boundary < self.boundary):
            raise TradeIntentError(
                f"cancellation at {self.cancellation.boundary} precedes "
                f"instantiation {self.boundary}")

    @property
    def thesis_key(self) -> str:
        return thesis_key(self.symbol, self.direction, self.entry_mode)

    @property
    def native_symbol(self) -> str:
        """`BTC/USDT` -> `BTCUSDT`, the repository's spelling."""
        return self.symbol.replace("/", "")

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def has_fixed_shadow_window(self) -> bool:
        """C-2.2: `CAPPED` and `KILL_COOLDOWN` shadows have no memory record, so
        no lifecycle event can cancel them — only the fixed window."""
        return self.shadow_kind in FIXED_WINDOW_SHADOWS
