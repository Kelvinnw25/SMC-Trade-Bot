"""backtest.outcome — the Outcome Engine (Milestone M8).

Replay creates history. **This package evaluates it.** Metrics aggregate it, and
that third stage does not exist yet. Three responsibilities, deliberately not
merged (C-9.1).

    TradeIntent + KlineRepository ──► price walk (§4) ──► OutcomeRecord ──► outcomes.jsonl

Five modules, one responsibility each:

- `config`  — `OutcomeConfig`, the canonical field list of C-9.7, and its hash
- `trade`   — the closed state vocabulary of C-3.1 and the `TradeIntent` boundary
- `walker`  — the price walk: fill, SL/TP resolution, worst-case tie-breaks (§4)
- `record`  — the immutable `OutcomeRecord` of C-9.4
- `engine`  — resolution and the single new artifact it writes

## What this package never does

No analysis, no signal generation, no aggregation, no modification of replay.
Nothing in `v2/` knows it exists. Outcomes are never written into the signal
ledger and no runtime module may read them: the ledger records *decisions*, and
outcomes are a re-derivable projection that can be regenerated whenever the
labeling rules improve (C-9.5).

## Input boundary

C-9.2 builds trades from the captured `LifecycleDecision` stream of §15. That
capture layer landed in M9, so `builder` now closes the loop: it replays a run's
`lifecycle_events.jsonl` into `TradeIntent`s, and the engine resolves them. The
engine itself still takes intents from its caller — the boundary is what keeps
"evaluate history" separable from "read history", and it is what a live-data
rehydrator (C-9.1) will populate without touching the walker.

`builder` reads the capture files directly and imports nothing from
`backtest.replay`; the wire format lives in the shared leaf
`backtest.capture_schema`, so §10's dependency direction holds.
"""
from backtest.outcome.builder import (
    BuildReport,
    TradeIntentBuilder,
    TradeSourceError,
    build_trade_intents,
)
from backtest.outcome.config import (
    CONTINUATION_FILLS,
    OutcomeConfig,
    config_hash,
)
from backtest.outcome.engine import (
    OUTCOMES_FILENAME,
    OutcomeEngine,
    OutcomeReport,
)
from backtest.outcome.errors import (
    OutcomeConfigError,
    OutcomeDataError,
    OutcomeError,
    TradeIntentError,
)
from backtest.outcome.record import OUTCOME_SCHEMA_VERSION, OutcomeRecord
from backtest.outcome.trade import (
    CANCEL_REASONS,
    Cancellation,
    EXIT_REASONS,
    FILL_KINDS,
    SHADOW_KINDS,
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_INVALIDATED_EXIT,
    STATE_INVALID_LEVELS,
    STATE_LOSS,
    STATE_NOT_FILLED,
    STATE_PENDING,
    STATE_WIN,
    TERMINAL_STATES,
    TradeIntent,
    thesis_key,
)
from backtest.outcome.walker import M5_PER_H1, Resolution, WalkCounters

__all__ = [
    "BuildReport",
    "CANCEL_REASONS",
    "TradeIntentBuilder",
    "TradeSourceError",
    "build_trade_intents",
    "CONTINUATION_FILLS",
    "Cancellation",
    "EXIT_REASONS",
    "FILL_KINDS",
    "M5_PER_H1",
    "OUTCOMES_FILENAME",
    "OUTCOME_SCHEMA_VERSION",
    "OutcomeConfig",
    "OutcomeConfigError",
    "OutcomeDataError",
    "OutcomeEngine",
    "OutcomeError",
    "OutcomeRecord",
    "OutcomeReport",
    "Resolution",
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
    "TradeIntentError",
    "WalkCounters",
    "config_hash",
    "thesis_key",
]
