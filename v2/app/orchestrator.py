"""app.orchestrator — the thin replacement for V1's ``run()`` loop.

Architecture ref: 02-software-architecture.md §3 (`app.orchestrator`), §5 (the
runtime pipeline: fetch → analyze → reconcile → apply → dispatch → persist), §10
Principle 12 (the orchestrator branches only on operational conditions, never on
anything that changes a signal's outcome, tier, or lifecycle transition).

This module is **coordination + delivery routing only**. It owns the scan loop,
dependency wiring, the data-sufficiency guard, `session_active` (from the
injected clock), per-symbol error isolation, the invocation order of the
collaborators, and — added in Phase 5.2 — routing the analysis/lifecycle
*outputs* to presentation: the dashboard, the scan/lifecycle terminal logs (via
`app.rendering`), the notifier (Alerts), and the OB-audit JSONL sink (via
`app.audit` + `infra.persistence`). Every *decision* is delegated; nothing here
recomputes analysis or lifecycle logic.

## Why it does not branch on score for M5

V1 fetched M5 only when the pre-M5 score reached its gate (>= 8). That gate is an
**analysis** eligibility rule (`V2Analyzer.analyze` owns it) and a branch that
changes the signal's tier — so, per Principle 12, it must NOT live here. The
orchestrator requests the snapshot *with* M5 and lets `analyze` decide whether to
use it: the produced Signal is identical to V1's; only the fetch cadence differs
(a documented, behavior-neutral I/O divergence, like the adapters' error
surfacing).

## One pipeline run for the observability/kill data, one analyze for the Signal

The frozen `V2Analyzer.analyze` returns only the final Signal — not the analysis
context. The delivery layer and the lifecycle kill/stale predicate both need the
fresh cycle's `HTFContext` (and the pre-M5 signal for the RAW log line), which
live in that context. So the orchestrator obtains the context with one pure
`pipeline.run` and the final post-M5 Signal with `analyze`. Both are pure
functions of the snapshot, so the double evaluation is behavior-neutral; it keeps
the M5 gate inside analysis (no Principle-12 leak) and lets the delivery layer
read domain objects only (no re-analysis in delivery).

- `kill_htf` (fed to the reconciler) = `context.htf` when a direction resolved or
  vetoed, else None — reproducing exactly which biases V1's `is_poi_invalidated`
  saw in `context["htf_map"]` (absent → "CHOPPY" — for the no_direction/invalid
  exits).
- the HTF log line uses `context.htf` directly (V1 printed it whenever
  `debug["htf"]` was set — including no_direction, where the kill predicate still
  saw CHOPPY). These two are deliberately different, matching V1.

## Error handling (V1 parity)

The adapters *surface* failures as typed exceptions (the one sanctioned
divergence); this orchestrator re-applies V1's swallow-and-continue policy at the
edge (per-symbol skip, empty-universe retry, load/save/notify best-effort), so
lifecycle behavior is unchanged.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from v2.analysis.context import AnalysisContext
from v2.analysis.engine import V2Analyzer
from v2.app.audit import AUDIT_ENTRY_MODE, assemble_ob_audit
from v2.app.ledger import assemble_ledger_record
from v2.app.rendering import render_dashboard, render_log_event, render_scan
from v2.domain import LifecycleDecision, Signal
from v2.infra.clock import Clock, SystemClock
from v2.infra.market_data import MarketDataAdapter, MarketDataError, MarketDataPort
from v2.infra.notifier import Notifier, NotifierError, TelegramNotifier
from v2.infra.persistence import PersistenceAdapter, PersistenceError, PersistencePort
from v2.lifecycle import LifecycleConfig, Reconciler, SignalStore

__all__ = ["Orchestrator"]

# Operational constants (V1 config / run() literals) — they gate *coordination*,
# not signal outcomes.
_TOP_COINS_LIMIT = 30            # config.TOP_COINS_LIMIT
_D1_MIN_ROWS = 10               # main.py:291 (len(df_d1) < 10 -> skip)
_H1_MIN_ROWS = 50               # main.py:291 (len(df_h1) < 50 -> skip)
_CYCLE_SLEEP_SECONDS = 60.0     # main.py:782 (sleep 60 between cycles)
_EMPTY_SLEEP_SECONDS = 30.0     # main.py:274 (sleep 30 on empty universe)
_FETCH_PAUSE_SECONDS = 0.20     # main.py:280/283/353 (inter-fetch rate limit)


class Orchestrator:
    """Coordinates one scan cycle (and the loop) across the V2 components.

    All collaborators are injected (constructor DI, Arch A6): tests supply fakes
    and never touch the network, filesystem, Telegram, or the wall clock;
    `create()` builds the production wiring. `writer` is the terminal sink
    (default `print`); `sleeper` is the cadence sink (default `time.sleep`) —
    both injected so tests observe output/cadence without side effects.

    ## Ports vs. concrete collaborators (Milestone M2)

    Every I/O collaborator is typed as its **port**, never as a concrete adapter:
    `Clock`, `MarketDataPort`, `PersistencePort`, `Notifier`, plus the two callable
    sinks. This class therefore names no infra implementation except inside
    `create()`, which is the one place production wiring belongs — so an alternative
    data source or storage backend is substituted by construction, with nothing here
    to change.

    `analyzer` and `reconciler` are deliberately **concrete** (`V2Analyzer`,
    `Reconciler`). They are not I/O: they are the public entry points of the
    analysis and lifecycle layers, and `app -> analysis` / `app -> lifecycle` is the
    sanctioned dependency direction (PROJECT_STATE.md §2). Inverting them would
    invert the layering, not decouple it. Both are still *injected*, so a decorator
    (e.g. one that records decisions) substitutes structurally without a Protocol
    and without touching this class.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        market_data: MarketDataPort,
        persistence: PersistencePort,
        notifier: Notifier,
        analyzer: V2Analyzer,
        config: LifecycleConfig = LifecycleConfig(),
        top_coins_limit: int = _TOP_COINS_LIMIT,
        reconciler: Optional[Reconciler] = None,
        writer: Callable[[str], None] = print,
        sleeper: Callable[[float], None] = time.sleep,
        fetch_pause_seconds: float = _FETCH_PAUSE_SECONDS,
        cycle_pause_seconds: float = _CYCLE_SLEEP_SECONDS,
        empty_pause_seconds: float = _EMPTY_SLEEP_SECONDS,
        source: str = "live",
        run_id: Optional[str] = None,
        data_snapshot_id: Optional[str] = None,
    ) -> None:
        self._clock = clock
        self._market = market_data
        self._persistence = persistence
        self._notifier = notifier
        self._analyzer = analyzer
        self._config = config
        self._top_coins_limit = top_coins_limit
        self._reconciler = reconciler or Reconciler(clock, config)
        self._writer = writer
        self._sleeper = sleeper
        self._fetch_pause = fetch_pause_seconds
        self._cycle_pause = cycle_pause_seconds
        self._empty_pause = empty_pause_seconds
        self._store: Optional[SignalStore] = None
        # V1's in-memory `ob_audit_seen`: per-process, resets on restart
        # (Decision #010 — orchestrator-owned transient dedup state).
        self._ob_audit_seen: set = set()
        # Ledger lineage (REPLAY_CONTRACT.md C-16.1/C-16.3.1). These are carried,
        # never interpreted: no branch in this class or below reads them, so they
        # cannot change a signal outcome, tier, or lifecycle transition
        # (Principle 12 holds). `-1` so the first `run_cycle` is cycle 0.
        self._source = source
        self._run_id = run_id
        self._data_snapshot_id = data_snapshot_id
        self._cycle_index = -1

    # -- production wiring --------------------------------------------------

    @classmethod
    def create(cls, *, base_dir: str = ".") -> "Orchestrator":
        """Wire the real adapters (production). Not for tests — this constructs
        the live `BinanceHandler` (network) via `MarketDataAdapter.create()`.

        The live `run_id` is minted here, once per process, per REPLAY_CONTRACT.md
        C-16.2 (`"live-<iso8601-utc>-<pid>"`). It is read from the same
        `SystemClock` this wiring injects, so the clock stays the single time
        source, and it is observability lineage only — nothing branches on it.
        """
        clock = SystemClock()
        return cls(
            clock=clock,
            market_data=MarketDataAdapter.create(),
            persistence=PersistenceAdapter(base_dir),
            notifier=TelegramNotifier(),
            analyzer=V2Analyzer(),
            source="live",
            run_id=f"live-{clock.now().strftime('%Y-%m-%dT%H:%M:%SZ')}-{os.getpid()}",
        )

    @property
    def store(self) -> Optional[SignalStore]:
        """The lifecycle memory (None until :meth:`startup`). For inspection."""
        return self._store

    # -- startup ------------------------------------------------------------

    def startup(self) -> None:
        """Load persisted memory into a fresh `SignalStore` (Arch §5 startup).

        Reproduces V1's `load_memory` / `load_killed_thesis`, including their
        swallow-to-empty behavior on a malformed store (the adapter surfaces
        `PersistenceError`; V1's policy was to start empty)."""
        try:
            active = self._persistence.load_active_signals(
                default_timestamp=self._clock.timestamp()
            )
            killed = self._persistence.load_killed_theses()
        except PersistenceError:
            active, killed = {}, {}
        self._store = SignalStore(
            active=active, killed_theses=killed, config=self._config
        )

    # -- one scan cycle -----------------------------------------------------

    def run_cycle(self) -> bool:
        """One full scan cycle. Returns True if the universe was processed,
        False on an empty/failed universe (V1's `if not top_coins: continue`,
        which skipped maintenance and saving that cycle)."""
        # Ledger lineage only (C-16.3.1): every call consumes an index, including
        # the ones that return False, so an empty-universe cycle is still a cycle.
        self._cycle_index += 1

        if self._store is None:
            self.startup()

        try:
            universe = self._market.get_top_volume_symbols(self._top_coins_limit)
        except MarketDataError:
            return False

        if not universe:
            return False

        # `enumerate(..., 1)` records the 1-based processing order as ledger
        # lineage (C-16.1 `universe_rank`). The iteration order itself is
        # unchanged — it is the adapter's descending-volume order, which decides
        # slot-cap and REPLACE outcomes and must never be re-sorted (C-5.6.2).
        for rank, entry in enumerate(universe, 1):
            self._process_symbol(entry.symbol, rank)

        self._end_of_cycle()
        return True

    def run_forever(self, *, max_cycles: Optional[int] = None) -> None:
        """The production scan loop (Arch §5 step 6; main.py:267-782). Sleeps 60s
        between full cycles and 30s after an empty universe (V1's cadence), via
        the injected `sleeper`. `max_cycles` bounds it for tests."""
        self.startup()
        count = 0
        while max_cycles is None or count < max_cycles:
            processed = self.run_cycle()
            count += 1
            if max_cycles is not None and count >= max_cycles:
                break
            self._sleeper(self._cycle_pause if processed else self._empty_pause)

    # -- per symbol ---------------------------------------------------------

    def _process_symbol(self, symbol: str, universe_rank: Optional[int] = None) -> None:
        """Analyze one symbol, reconcile it, and route its output — isolated
        from the rest of the scan. Any failure skips just this symbol (V1's
        per-coin `try/except ... continue`, main.py:766-768).

        `universe_rank` is the symbol's 1-based position in this cycle's universe.
        It is forwarded to the ledger record and nowhere else."""
        try:
            self._sleeper(self._fetch_pause)  # V1 inter-fetch rate limit (cadence)
            snapshot = self._market.get_market_snapshot(symbol, include_m5=True)
            if snapshot is None:
                return  # V1: D1/H1 None/empty -> continue
            if len(snapshot.d1) < _D1_MIN_ROWS or len(snapshot.h1) < _H1_MIN_ROWS:
                return  # V1: data-sufficiency guard (main.py:291)

            session_active = self._session_active()
            context = self._analyzer.pipeline.run(
                AnalysisContext.initial(snapshot, session_active=session_active)
            )
            signal = self._analyzer.analyze(snapshot, session_active=session_active)

            # Evaluation ledger (Phase 7.1): record EVERY completed analysis, all
            # tiers, before any lifecycle/delivery step — so a completed analysis
            # is captured even if a later step is skipped. Write-only; the record
            # never re-enters a decision.
            self._record_ledger(
                snapshot, context, signal, session_active, universe_rank
            )

            # kill/stale predicate biases: present only when a direction resolved
            # or vetoed (V1's context["htf_map"]); None -> "CHOPPY".
            kill_htf = context.htf if signal.direction is not None else None
            decision = self._reconciler.reconcile(
                signal,
                self._store.view(),
                htf=kill_htf,
                live_price=snapshot.live_price,
                h1_last_ts=snapshot.h1_last_ts,
            )
            self._store.apply(decision)

            self._render_symbol(signal, context, decision)
            self._dispatch(decision)
            self._audit(snapshot, signal, decision)
        except Exception:
            return

    def _render_symbol(self, signal: Signal, context, decision: LifecycleDecision) -> None:
        """Terminal output for one symbol: the SCAN block (unless the symbol was
        killed — V1 `continue`d before printing it), then the lifecycle logs, in
        V1's order."""
        killed = any(le.event_type == "KILL" for le in decision.log_events)
        if not killed:
            for line in render_scan(signal, context.htf, context.signal):
                self._writer(line)
        for event in decision.log_events:
            self._writer(render_log_event(event))

    def _dispatch(self, decision: LifecycleDecision) -> None:
        """Route the decision's Alerts to the notifier (Arch §9). V1 marked
        signals alerted regardless of send success, so a `NotifierError` is
        swallowed (best-effort delivery)."""
        for alert in decision.alerts:
            try:
                self._notifier.send(alert)
            except NotifierError:
                continue

    def _audit(
        self, snapshot, signal: Signal, decision: LifecycleDecision
    ) -> None:
        """Assemble and append the retracement OB-audit line (Decision #010).

        V1 wrote it only when the symbol was neither killed nor stale (it
        `continue`d before the audit in both cases). The eligibility + payload
        come from `app.audit`; the dedup set is orchestrator-owned; the write is
        the persistence sink. V1 adds the dedup key regardless of write success
        (its append swallows failures), so the key is recorded even if the
        surfaced append fails."""
        if any(le.event_type in ("KILL", "STALE") for le in decision.log_events):
            return
        record = assemble_ob_audit(snapshot, signal)
        if record is None:
            return
        key = (snapshot.symbol, snapshot.h1_last_ts, AUDIT_ENTRY_MODE)
        if key in self._ob_audit_seen:
            return
        try:
            self._persistence.append_ob_audit(record)
        except PersistenceError:
            pass
        self._ob_audit_seen.add(key)

    def _record_ledger(
        self,
        snapshot,
        context,
        signal: Signal,
        session_active: bool,
        universe_rank: Optional[int] = None,
    ) -> None:
        """Append the evaluation-ledger record for one completed analysis
        (Phase 7.1). Best-effort: a `PersistenceError` is swallowed so a ledger
        write never disturbs the scan. Write-only observability — the record is
        assembled from analysis outputs and never read back into a decision.

        The schema-2 lineage (C-16.1) is passed straight through: the constructor
        values plus this cycle's index and the symbol's universe rank."""
        record = assemble_ledger_record(
            snapshot,
            context,
            signal,
            session_active=session_active,
            recorded_at=self._clock.timestamp(),
            source=self._source,
            run_id=self._run_id,
            cycle_index=self._cycle_index,
            universe_rank=universe_rank,
            data_snapshot_id=self._data_snapshot_id,
        )
        try:
            self._persistence.append_signal_ledger(record)
        except PersistenceError:
            pass

    # -- coordination helpers ----------------------------------------------

    def _session_active(self) -> bool:
        """V1's `get_session_mode() == "SNIPER"` (strategy.py:14-19) from the
        injected clock: a weekday (Mon-Fri) between 07:00 and 22:00 UTC."""
        now = self._clock.now()
        return now.weekday() < 5 and 7 <= now.hour < 22

    # -- end of cycle -------------------------------------------------------

    def _end_of_cycle(self) -> None:
        """TTL sweep (+ its logs) → killed-thesis prune → persist → dashboard
        (Arch §5 step 5; main.py:770-780). A save failure is swallowed (V1's
        `save_memory` printed and continued)."""
        assert self._store is not None
        ttl_decision = self._store.ttl_sweep(self._clock)
        self._store.apply(ttl_decision)
        for event in ttl_decision.log_events:
            self._writer(render_log_event(event))

        self._store.prune_killed_theses(self._clock)

        try:
            self._persistence.save_active_signals(self._store.active)
            self._persistence.save_killed_theses(self._store.killed_theses)
        except PersistenceError:
            pass

        for line in render_dashboard(self._store.active):
            self._writer(line)
