"""The Replay Driver — the deterministic time machine (Milestone M7).

REPLAY_CONTRACT.md C-5.0 … C-5.2 (the window, the boundary, the clock), C-5.7
(burn-in), C-6.11 (warmup), C-8.1 … C-8.3 (orchestrator reuse, the swap table,
the loop), C-15.6/C-15.7 (replay sinks), C-15.8 (coverage visibility), C-16.2/16.4
(run identity and the output bundle).

## Replay is not a second bot

    ReplayDriver
        ├── FakeClock                      seeded at T_0 + 1s, advanced 3600 s/cycle
        ├── HistoricalMarketDataAdapter    positioned at each boundary
        ├── ReplayPersistence              run-scoped, empty at start
        ├── RecordingNotifier              records, never delivers
        └── Orchestrator                   ← the production class, unchanged
              └── V2Analyzer, Reconciler, SignalStore, ledger  ← all unchanged

The driver constructs the production `Orchestrator` and calls its production
`run_cycle()`. It re-implements no part of the scan loop, because nine conventions
live only inside `_process_symbol` / `_end_of_cycle` and each silently changes
results if re-derived (C-8.1). This module contains **no strategy logic at all**:
no signal, no scoring, no entry, no risk, no lifecycle rule, no outcome.

## One cycle

```
for k, T in enumerate(boundaries):     # strictly increasing, step 3600, no gaps
    if k > 0: clock.advance(3600)      # the ONLY way time moves
    adapter.set_time(T)
    processed = orchestrator.run_cycle()
    record health; stop on corruption
```

The clock does not move *within* a cycle (C-5.2), so every value a cycle stamps —
`ActiveSignal.timestamp`, `alert_sent_ts`, `killed_ts`, `recorded_at` — equals
`T + 1s`. That is more deterministic than live, where `SystemClock` drifts across
the ~45 s a real cycle takes, and is neutral for every threshold that reads it.

## Why the driver watches counters instead of catching exceptions

`_process_symbol` catches **every** exception and returns silently
([orchestrator.py:293](v2/app/orchestrator.py#L293)) — deliberate per-symbol
isolation in live, where one bad symbol must not stop the scan. In replay it means
a corrupt dataset cannot stop the run by propagating: it looks exactly like a
symbol the strategy skipped (finding A51). So the adapter counts data errors and
the driver checks that counter after every cycle. That is the *only* channel
through which "dataset corruption STOPS replay" is implementable without
modifying the orchestrator, which C-8.1 forbids.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from backtest.data.repository import (
    DatasetNotFound, KlineRepository, RepositoryError,
)
from backtest.capture_schema import (
    LIFECYCLE_EVENTS_FILENAME, MEMORY_SNAPSHOTS_FILENAME,
)
from backtest.lineage import optimizations
from backtest.replay.adapter import HistoricalMarketDataAdapter
from backtest.replay.capture import CaptureRecorder, RecordingReconciler
from backtest.replay.config import H1_SECONDS, ReplayConfig
from backtest.replay.sinks import (
    ALERTS_FILENAME, RecordingNotifier, ReplayPersistence,
)
from backtest.replay.caching import build_analyzer
from v2.app.orchestrator import Orchestrator
from v2.infra.clock import FakeClock
from v2.lifecycle import Reconciler

# The orchestrator's data-sufficiency thresholds. Imported rather than restated:
# C-5.0 requires the manifest to record how many symbols were guard-skipped at
# `T_0`, which cannot be reported without knowing the guard, and a second copy of
# a strategy constant is exactly the kind of drift this project keeps out. See
# finding R4 — these should be public.
from v2.app.orchestrator import _D1_MIN_ROWS, _H1_MIN_ROWS

__all__ = [
    "CycleHealth",
    "HEALTH_FILENAME",
    "MANIFEST_FILENAME",
    "ReplayAborted",
    "ReplayDriver",
    "ReplayError",
    "ReplayResult",
    "RunWindowError",
    "WarmupReport",
]

MANIFEST_FILENAME = "manifest.json"
HEALTH_FILENAME = "run_health.jsonl"
CONSOLE_FILENAME = "console.log"

#: C-6.11 warmup, expressed as the **closed** rows an adapter actually serves.
#: The contract's 500/120/200 are live's raw request sizes, which include the
#: forming candle it strips; the closed equivalents are one less.
WARMUP_H1_ROWS = 499
WARMUP_D1_ROWS = 119
WARMUP_M5_ROWS = 199


class ReplayError(Exception):
    """Base for every replay failure."""


class RunWindowError(ReplayError):
    """The requested window cannot be replayed correctly. Raised **before** any
    cycle runs, so a misconfigured run costs nothing and says why."""


class ReplayAborted(ReplayError):
    """A run stopped mid-flight because the data could not be trusted."""


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WarmupReport:
    """What the repository can support at `T_0` (C-6.11 / C-5.0).

    Warmup is enforced **per symbol by the existing guards**, not globally: a
    symbol short of 50 closed H1 or 10 closed D1 rows is skipped that cycle
    exactly as in live. So this is a *report*, not a gate — except that a window
    where nothing at all can be analysed is refused, because such a run produces
    an empty result that reads as "the strategy found nothing".
    """

    symbols_total: int
    eligible_at_start: tuple[str, ...]
    fully_warmed: tuple[str, ...]
    guard_skipped_at_start: tuple[str, ...]
    last_boundary_with_data: Optional[int]

    def to_dict(self) -> dict:
        return {
            "symbols_total": self.symbols_total,
            "eligible_at_start": list(self.eligible_at_start),
            "fully_warmed": list(self.fully_warmed),
            "guard_skipped_at_start": list(self.guard_skipped_at_start),
            "guard_skips_at_T0": len(self.guard_skipped_at_start),
            "last_boundary_with_data": self.last_boundary_with_data,
        }


@dataclass(frozen=True)
class CycleHealth:
    """One line of `run_health.jsonl` (C-15.8).

    Completed in M9. Until the capture layer existed, `unaccounted` conflated two
    causes C-15.8 separates — symbols the sufficiency guard skipped, and symbols
    whose analysis raised and was swallowed — and finding R2 recorded that the
    split needed §15. It now exists, so the identity is evaluated in full:

        expected_analyzed       = universe_size - snapshots_none - guard_skips
        swallowed_before_ledger = expected_analyzed - ledger_rows
        swallowed_after_ledger  = ledger_rows      - decisions

    Both `swallowed_*` values are 0 in a healthy run, and the run fails if either
    exceeds `max_swallowed_errors`. `unaccounted` is retained so a line from
    before this milestone remains comparable with one from after it.
    """

    cycle_index: int
    scan_T: int
    processed: bool
    universe_size: int
    snapshots_served: int
    snapshots_none: int
    guard_skips: int
    m5_attached: int
    m5_guard_failures: int
    data_errors: int
    universe_error: Optional[str]
    ledger_rows: int
    decisions: int
    lifecycle_events: int
    ttl_inferred: int
    ttl_log_lines: int
    alerts: int
    expected_analyzed: int
    swallowed_before_ledger: int
    swallowed_after_ledger: int
    unaccounted: int
    in_burn_in: bool

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def healthy(self) -> bool:
        return (self.swallowed_before_ledger == 0
                and self.swallowed_after_ledger == 0
                and self.universe_error is None)


@dataclass(frozen=True)
class ReplayResult:
    """Everything a completed run produced. No profit, no loss, no trades, no
    metrics — the Outcome Engine owns those (Task 7)."""

    run_id: str
    data_snapshot_id: str
    run_dir: Path
    start: int
    end: int
    cycles: int
    cycles_processed: int
    simulated_seconds: int
    wall_seconds: float
    ledger_rows: int
    ob_audit_rows: int
    alerts: int
    lifecycle_events: int = 0
    health: tuple[CycleHealth, ...] = field(repr=False, default=())

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "data_snapshot_id": self.data_snapshot_id,
            "run_dir": str(self.run_dir),
            "T_0": self.start,
            "T_n": self.end,
            "cycles": self.cycles,
            "cycles_processed": self.cycles_processed,
            "simulated_seconds": self.simulated_seconds,
            "ledger_rows": self.ledger_rows,
            "ob_audit_rows": self.ob_audit_rows,
            "alerts": self.alerts,
            "lifecycle_events": self.lifecycle_events,
        }

    @property
    def decisions(self) -> int:
        return sum(entry.decisions for entry in self.health)

    @property
    def swallowed(self) -> int:
        """C-15.8's two swallowed-exception counts, summed. 0 in a healthy run —
        and a run in which it is not 0 has already failed."""
        return sum(entry.swallowed_before_ledger + entry.swallowed_after_ledger
                   for entry in self.health)


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class ReplayDriver:
    """Runs the production orchestrator over historical time.

    Construction opens the repository and computes the run identity; nothing is
    written and no cycle runs until `run()`. `validate()` may be called first to
    check a window without executing it.
    """

    def __init__(
        self,
        config: ReplayConfig,
        *,
        repository: Optional[KlineRepository] = None,
        progress: Optional[Callable[[int, int, int], None]] = None,
        overwrite: bool = False,
    ) -> None:
        self._config = config
        self._progress = progress
        self._overwrite = overwrite
        try:
            self._repository = repository if repository is not None else \
                KlineRepository(config.repository_root)
        except RepositoryError as exc:
            raise RunWindowError(
                f"cannot open the repository at {config.repository_root}: {exc}"
            ) from exc
        self._run_id = config.run_id(
            data_snapshot_id=self._repository.data_snapshot_id)

    # -- identity ----------------------------------------------------------

    @property
    def config(self) -> ReplayConfig:
        return self._config

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def data_snapshot_id(self) -> str:
        return self._repository.data_snapshot_id

    @property
    def run_dir(self) -> Path:
        """`<output_root>/runs/<run_id>/` — deterministic, so re-running the same
        specification lands in the same place (C-16.4)."""
        return self._config.output_root / "runs" / self._run_id

    # -- Task 3: run-window validation -------------------------------------

    def validate(self) -> WarmupReport:
        """Check the window before spending a single cycle on it.

        Alignment and ordering are already enforced by `ReplayConfig`. What
        remains is everything that depends on the *data*: that the repository
        holds symbols at all, that something can actually be analysed at `T_0`,
        and that the window does not run past the end of history.
        """
        config = self._config
        start_ms = config.start * 1000

        try:
            symbols = self._repository.symbols("1h")
        except RepositoryError as exc:
            raise RunWindowError(f"cannot enumerate the repository: {exc}") from exc
        if not symbols:
            raise RunWindowError(
                f"the repository at {self._repository.root} holds no 1h datasets; "
                f"nothing can be replayed"
            )

        eligible: list[str] = []
        warmed: list[str] = []
        guard_skipped: list[str] = []
        last_boundary: Optional[int] = None

        for native in symbols:
            h1 = self._closed_rows(native, "1h", start_ms, WARMUP_H1_ROWS)
            d1 = self._closed_rows(native, "1d", start_ms, WARMUP_D1_ROWS)
            m5 = self._closed_rows(native, "5m", start_ms, WARMUP_M5_ROWS)
            if h1 >= _H1_MIN_ROWS and d1 >= _D1_MIN_ROWS:
                eligible.append(native)
            else:
                guard_skipped.append(native)
            if (h1 >= WARMUP_H1_ROWS and d1 >= WARMUP_D1_ROWS
                    and m5 >= WARMUP_M5_ROWS):
                warmed.append(native)
            boundary = self._last_boundary(native)
            if boundary is not None and (last_boundary is None
                                         or boundary > last_boundary):
                last_boundary = boundary

        if not eligible:
            raise RunWindowError(
                f"no symbol satisfies the data-sufficiency guards at T_0="
                f"{_iso(config.start)} (need >= {_H1_MIN_ROWS} closed H1 and "
                f">= {_D1_MIN_ROWS} closed D1 rows). The window starts before "
                f"the data does, or the repository is too thin to replay. "
                f"Checked {len(symbols)} symbol(s)."
            )
        if last_boundary is not None and config.end > last_boundary:
            raise RunWindowError(
                f"T_n={_iso(config.end)} is past the end of history: the last "
                f"boundary at which any 1h candle had closed is "
                f"{_iso(last_boundary)}. Replaying beyond it would produce empty "
                f"cycles that read as 'the strategy found nothing'."
            )

        return WarmupReport(
            symbols_total=len(symbols),
            eligible_at_start=tuple(eligible),
            fully_warmed=tuple(warmed),
            guard_skipped_at_start=tuple(guard_skipped),
            last_boundary_with_data=last_boundary,
        )

    def _closed_rows(self, native: str, timeframe: str, end_ms: int,
                     count: int) -> int:
        """How many closed rows the adapter would serve at `end_ms`, capped at
        `count`. Uses the repository's own slicing, so the number is the one the
        run will actually see."""
        try:
            return len(self._repository.window(
                native, timeframe, end_ms=end_ms, count=count))
        except DatasetNotFound:
            return 0
        except RepositoryError as exc:
            raise RunWindowError(
                f"{native} {timeframe}: {exc}"
            ) from exc

    def _last_boundary(self, native: str) -> Optional[int]:
        """The last scan boundary at which an H1 candle had closed."""
        try:
            coverage = self._repository.coverage(native, "1h")
        except RepositoryError:
            return None
        return (coverage.last_open_ms + coverage.interval_ms) // 1000

    # -- Task 2 / Task 4: the run ------------------------------------------

    def run(self) -> ReplayResult:
        """Validate, wire, and replay every boundary in the window."""
        config = self._config
        warmup = self.validate()

        run_dir = self.run_dir
        self._prepare_run_dir(run_dir)

        clock = FakeClock(
            datetime.fromtimestamp(config.start + 1, tz=timezone.utc))
        adapter = HistoricalMarketDataAdapter(
            self._repository,
            static_universe=config.static_universe,
            # M13: injected, never resolved here. `backtest.universe` owns the
            # decision; the driver only forwards it.
            eligible=config.eligible,
        )
        # C-15.2.1: one object owns every file in the bundle. The sinks below hold
        # none of their own — they forward here.
        recorder = CaptureRecorder(
            run_dir,
            run_id=self._run_id,
            source="replay",
            capture_console=config.capture_console,
        )
        persistence = ReplayPersistence(run_dir, recorder=recorder)
        notifier = RecordingNotifier(run_dir, recorder=recorder)

        orchestrator = Orchestrator(
            clock=clock,
            market_data=adapter,
            persistence=persistence,
            notifier=notifier,
            analyzer=build_analyzer(caching=config.caching_analyzer,
                                    data_snapshot_id=self.data_snapshot_id),
            config=config.lifecycle_config,
            top_coins_limit=config.top_coins_limit,
            # C-15.2 / R-6: the one production seam capture uses — the injection
            # slot that already existed. The wrapper adds no branch and returns
            # the inner decision unchanged, so behaviour is provably identical.
            reconciler=RecordingReconciler(
                Reconciler(clock, config.lifecycle_config), recorder),
            writer=recorder.record_writer_line,
            sleeper=_no_sleep,
            source="replay",
            run_id=self._run_id,
            data_snapshot_id=self.data_snapshot_id,
        )

        health: list[CycleHealth] = []
        processed_count = 0
        started = time.perf_counter()
        boundaries = config.boundaries

        for index, boundary in enumerate(boundaries):
            if index > 0:
                clock.advance(float(H1_SECONDS))
            self._assert_clock(clock, boundary)
            adapter.set_time(boundary)
            recorder.begin_cycle(index, boundary)

            before_rows = persistence.ledger_rows
            processed = orchestrator.run_cycle()
            processed_count += int(processed)

            counters = adapter.counters
            rows = persistence.ledger_rows - before_rows
            # C-15.2.1: `universe_rank` comes from the universe the adapter
            # served, never from the order in which decisions arrived.
            capture = recorder.end_cycle(processed, universe=adapter.universe)

            guard_skips = sum(
                1 for _symbol, d1_rows, h1_rows in counters.served_shapes
                if d1_rows < _D1_MIN_ROWS or h1_rows < _H1_MIN_ROWS
            )
            expected = (counters.universe_size - counters.snapshots_none
                        - guard_skips)
            health.append(CycleHealth(
                cycle_index=index,
                scan_T=boundary,
                processed=processed,
                universe_size=counters.universe_size,
                snapshots_served=counters.snapshots_served,
                snapshots_none=counters.snapshots_none,
                guard_skips=guard_skips,
                m5_attached=counters.m5_attached,
                m5_guard_failures=counters.m5_guard_failures,
                data_errors=counters.data_errors,
                universe_error=counters.universe_error,
                ledger_rows=rows,
                decisions=capture.decisions,
                lifecycle_events=capture.events,
                ttl_inferred=capture.ttl_inferred,
                ttl_log_lines=capture.ttl_log_lines,
                alerts=capture.alerts,
                expected_analyzed=expected,
                swallowed_before_ledger=expected - rows,
                swallowed_after_ledger=rows - capture.decisions,
                unaccounted=counters.universe_size - counters.snapshots_none - rows,
                in_burn_in=boundary < config.burn_in_until,
            ))

            self._check_health(run_dir, health, index, boundary)
            if self._progress is not None:
                self._progress(index, len(boundaries), boundary)

        wall = time.perf_counter() - started
        self._write_health(run_dir, health)
        if config.capture_console:
            (run_dir / CONSOLE_FILENAME).write_text(
                "\n".join(recorder.console) + "\n", encoding="utf-8")

        result = ReplayResult(
            run_id=self._run_id,
            data_snapshot_id=self.data_snapshot_id,
            run_dir=run_dir,
            start=config.start,
            end=config.end,
            cycles=len(boundaries),
            cycles_processed=processed_count,
            simulated_seconds=config.simulated_seconds,
            wall_seconds=wall,
            ledger_rows=persistence.ledger_rows,
            ob_audit_rows=persistence.ob_audit_rows,
            alerts=len(notifier.alerts),
            lifecycle_events=recorder.events_written,
            health=tuple(health),
        )
        self._write_manifest(run_dir, result, warmup)
        return result

    def _prepare_run_dir(self, run_dir: Path) -> None:
        """Make the run directory safe to write into.

        `run_id` is deterministic, so re-running the same specification targets
        the same directory — and the ledger and audit sinks *append*. Writing
        into a finished run would silently double every row, producing a file
        that looks like a longer run. So a completed run is refused unless
        `overwrite=True`, and overwriting removes exactly the files this driver
        writes, never the directory: deleting a path a caller supplied is not
        this component's decision to make.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = run_dir / MANIFEST_FILENAME
        outputs = [
            manifest, run_dir / HEALTH_FILENAME, run_dir / CONSOLE_FILENAME,
            run_dir / ALERTS_FILENAME, run_dir / "signal_ledger.jsonl",
            run_dir / "retracement_ob_audit.jsonl", run_dir / "active_pois.json",
            run_dir / "killed_thesis.json",
            run_dir / LIFECYCLE_EVENTS_FILENAME,
            run_dir / MEMORY_SNAPSHOTS_FILENAME,
        ]
        existing = [path for path in outputs if path.exists()]
        if not existing:
            return
        if not self._overwrite:
            raise RunWindowError(
                f"{run_dir} already holds output from this run "
                f"({', '.join(p.name for p in existing)}). The ledger and audit "
                f"sinks append, so re-running here would double their rows. Pass "
                f"overwrite=True to replace it, or use a different output_root."
            )
        for path in existing:
            path.unlink()

    # -- invariants --------------------------------------------------------

    def _check_health(self, run_dir: Path, health: list[CycleHealth],
                      index: int, boundary: int) -> None:
        """Everything that must hold after a cycle, checked before the next one.

        Each failure writes the health file first, so the run that stopped can be
        diagnosed from its own output rather than from this exception alone.
        """
        entry = health[-1]
        limit = self._config.max_swallowed_errors

        if entry.data_errors:
            self._abort(run_dir, health, index, boundary, (
                f"the adapter reported {entry.data_errors} dataset error(s). The "
                f"orchestrator isolates per-symbol failures, so a corrupt "
                f"repository would otherwise look like skipped symbols. "
                f"Re-verify the repository before re-running."))

        if entry.universe_error is not None:
            # C-8.3: `run_cycle` swallows a universe-level `MarketDataError` and
            # returns False, which is indistinguishable from an empty market.
            self._abort(run_dir, health, index, boundary, (
                f"the universe call failed with {entry.universe_error}. "
                f"`run_cycle` swallows it and reports an empty universe, so a "
                f"broken repository would read as 'no symbols qualified'."))

        if entry.swallowed_before_ledger > limit:
            self._abort(run_dir, health, index, boundary, (
                f"{entry.swallowed_before_ledger} symbol(s) were analysed but "
                f"produced no ledger row (C-15.8: expected_analyzed="
                f"{entry.expected_analyzed}, ledger_rows={entry.ledger_rows}). "
                f"`_process_symbol` swallows every exception with no output, so "
                f"this is a silent analysis failure, not a quiet market."))

        if entry.swallowed_after_ledger > limit:
            self._abort(run_dir, health, index, boundary, (
                f"{entry.swallowed_after_ledger} ledger row(s) have no captured "
                f"lifecycle decision (C-15.8: ledger_rows={entry.ledger_rows}, "
                f"decisions={entry.decisions}). The analysis completed and then "
                f"something between the ledger write and `reconcile` raised and "
                f"was swallowed."))

    def _abort(self, run_dir: Path, health: list[CycleHealth], index: int,
               boundary: int, why: str) -> None:
        self._write_health(run_dir, health)
        raise ReplayAborted(
            f"replay stopped at cycle {index} (T={_iso(boundary)}): {why}")

    @staticmethod
    def _assert_clock(clock: FakeClock, boundary: int) -> None:
        """C-5.2: the clock reads exactly `T + 1s` for this cycle.

        Checked every cycle rather than trusted: the clock and the adapter are
        advanced by two separate statements, and a drift between them would
        shift `session_active` — a scoring input — without any other symptom.
        """
        expected = boundary + 1
        actual = clock.timestamp()
        if actual != float(expected):
            raise ReplayAborted(
                f"clock/boundary drift at T={_iso(boundary)}: the clock reads "
                f"{actual} but the cycle requires {expected} (C-5.2)"
            )

    # -- Task 7: outputs ---------------------------------------------------

    def _write_health(self, run_dir: Path, health: list[CycleHealth]) -> None:
        path = run_dir / HEALTH_FILENAME
        with open(path, "w", encoding="utf-8") as handle:
            for entry in health:
                handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")

    def _write_manifest(self, run_dir: Path, result: ReplayResult,
                        warmup: WarmupReport) -> None:
        """The run manifest (C-16.4).

        `generated_at` and `wall_seconds` are the **only** non-reproducible keys,
        so the determinism test compares the manifest after removing exactly
        those two.
        """
        manifest: dict[str, Any] = {
            "run_id": self._run_id,
            "core": self._config.core(data_snapshot_id=self.data_snapshot_id),
            # M13: the identity of the classification that produced the ranked
            # candidate set. `data_snapshot_id` does not determine it, because
            # `corpus_id` excludes discovery artifacts. Recorded outside `core`
            # because C-16.2's `core` is a closed list — see `ReplayConfig`.
            "universe_id": self._config.universe_id,
            "universe_size": len(self._config.eligible or ()),
            # Optimization provenance. Outside `core` for the same reason
            # `universe_id` is: the claim these flags make is that they do not
            # change results, and `core` is C-16.2's closed list. A
            # cached-vs-uncached comparison therefore excludes this key along
            # with the two wall-clock keys — it *is* the variable under test.
            #
            # Assembled by `backtest.lineage`, not here: `vectorized_swings`
            # reads `analysis.structure`, and this package's `v2` imports are a
            # closed allowlist that must not be widened for a provenance field.
            "optimizations": optimizations(
                caching_analyzer=self._config.caching_analyzer),
            "builder_version": self._repository.builder_version,
            "repository_root": str(self._repository.root),
            "cycles": result.cycles,
            "cycles_processed": result.cycles_processed,
            "simulated_seconds": result.simulated_seconds,
            "ledger_rows": result.ledger_rows,
            "ob_audit_rows": result.ob_audit_rows,
            "alerts": result.alerts,
            "warmup": warmup.to_dict(),
            "burn_in_until": self._config.burn_in_until,
            "declared_biases": _DECLARED_BIASES,
            "git_commit": _git_commit(),
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "wall_seconds": round(result.wall_seconds, 3),
        }
        path = run_dir / MANIFEST_FILENAME
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")


# ---------------------------------------------------------------------------

#: The fidelity losses every replay run inherits (C-5.5). Recorded in the
#: manifest so a result is never read as if it were live.
_DECLARED_BIASES = [
    "live_price = close(C_ref); the forming-candle price is unreconstructible "
    "at H1 cadence (C-5.5.3)",
    "kill/cancel decisions react to closes, not intra-candle wicks (C-5.5.1)",
    "M5 enrichment reaches every symbol, whereas live reaches only those scanned "
    "before hh:05 (C-5.5.2)",
    "universe ranking is hourly and closed-only, whereas live uses Binance's "
    "rolling 24h ticker including the forming hour (C-6.9)",
    "a symbol with no closed candle in the trailing 24h is dropped from the "
    "ranking, whereas live's ticker still lists it and ranks it at ~0 volume "
    "(A1). Exclusion and rank-at-zero differ only when fewer than "
    "top_coins_limit symbols hold recent data; the declared window's candidate "
    "pool is 148-768, so the ranked universe is never short (C-6.9)",
]


def _discard(_line: str) -> None:
    """The default console sink. A five-year run renders ~50 lines per cycle;
    keeping them is opt-in."""


def _no_sleep(_seconds: float) -> None:
    """Replay never sleeps (Task 2). The orchestrator's pacing calls become
    no-ops; nothing else about the loop changes."""


def _iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _git_commit() -> Optional[str]:
    """HEAD, read from `.git` without spawning a process. `None` outside a
    checkout — lineage, never a dependency."""
    root = Path(__file__).resolve().parents[2]
    head = root / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("ref:"):
        ref = root / ".git" / text.split(" ", 1)[1].strip()
        try:
            return ref.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return text or None
