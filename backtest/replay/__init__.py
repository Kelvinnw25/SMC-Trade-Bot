"""backtest.replay — historical execution of the production bot.

Three milestones live here:

- **M6** `HistoricalMarketDataAdapter` — the historical implementation of the
  `MarketDataPort` the V2 orchestrator already depends on.
- **M7** `ReplayDriver` — the deterministic time machine that advances a
  `FakeClock` one closed H1 boundary at a time and calls the **production**
  `Orchestrator.run_cycle()`.
- **M9** `CaptureRecorder` + `RecordingReconciler` — §15 lifecycle capture, which
  makes every lifecycle transition observable with zero change to behaviour, and
  supplies the trade source C-2.1 mandates.

Replay is not a second bot. It is the same bot with a different clock, a
different market-data source and a different persistence destination; the
analyzer, scoring, entry, risk, lifecycle and ledger are the same objects running
the same code. Capture adds a fourth difference of the same kind — an observer on
the existing injection slot — and no fifth.

The Outcome Engine consumes what this package captures, but lives in
`backtest.outcome` and is never imported from here (§10). The metrics engine does
not exist yet.
"""
from backtest.replay.adapter import (
    CycleCounters,
    D1_ROWS,
    DataLineage,
    FRAME_COLUMNS,
    H1_ROWS,
    HistoricalMarketDataAdapter,
    M5_MIN_CLOSED_ROWS,
    M5_ROWS,
    ReplayDataError,
    UNIVERSE_TRAILING_ROWS,
)
from backtest.replay.capture import (
    ALERTS_FILENAME,
    CaptureError,
    CaptureRecorder,
    CycleCapture,
    LIFECYCLE_EVENTS_FILENAME,
    MEMORY_SNAPSHOTS_FILENAME,
    RecordingReconciler,
    TTL_RENDERED_MARKER,
)
from backtest.replay.config import (
    BURN_IN_H1_DEFAULT,
    ConfigError,
    H1_SECONDS,
    ReplayConfig,
    STRATEGY_VERSION,
    UNIVERSE_MODES,
    environment,
)
from backtest.replay.driver import (
    CycleHealth,
    HEALTH_FILENAME,
    MANIFEST_FILENAME,
    ReplayAborted,
    ReplayDriver,
    ReplayError,
    ReplayResult,
    RunWindowError,
    WarmupReport,
)
from backtest.replay.sinks import RecordingNotifier, ReplayPersistence

__all__ = [
    "ALERTS_FILENAME",
    "BURN_IN_H1_DEFAULT",
    "CaptureError",
    "CaptureRecorder",
    "ConfigError",
    "CycleCapture",
    "CycleCounters",
    "CycleHealth",
    "D1_ROWS",
    "LIFECYCLE_EVENTS_FILENAME",
    "MEMORY_SNAPSHOTS_FILENAME",
    "RecordingReconciler",
    "TTL_RENDERED_MARKER",
    "DataLineage",
    "FRAME_COLUMNS",
    "H1_ROWS",
    "H1_SECONDS",
    "HEALTH_FILENAME",
    "HistoricalMarketDataAdapter",
    "M5_MIN_CLOSED_ROWS",
    "M5_ROWS",
    "MANIFEST_FILENAME",
    "RecordingNotifier",
    "ReplayAborted",
    "ReplayConfig",
    "ReplayDataError",
    "ReplayDriver",
    "ReplayError",
    "ReplayPersistence",
    "ReplayResult",
    "RunWindowError",
    "STRATEGY_VERSION",
    "UNIVERSE_MODES",
    "UNIVERSE_TRAILING_ROWS",
    "WarmupReport",
    "environment",
]
