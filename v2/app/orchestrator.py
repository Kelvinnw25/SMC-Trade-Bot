"""app.orchestrator — the thin replacement for V1's ``run()`` loop.

Architecture ref: 02-software-architecture.md §3 (`app.orchestrator`), §5 (the
runtime pipeline: fetch → analyze → reconcile → apply → dispatch → persist), §10
Principle 12 (the orchestrator branches only on operational conditions, never on
anything that changes a signal's outcome, tier, or lifecycle transition).

This module is **coordination only**. It owns the scan loop, dependency wiring,
the data-sufficiency guard, `session_active` (computed from the injected clock),
per-symbol error isolation, and the invocation order of the four collaborators
(market data → analyzer → lifecycle → persistence/notifier). Every *decision* —
what a signal is, whether it kills/decays/inserts, how an alert is formatted — is
delegated to the existing components. Nothing here re-implements or duplicates
their logic.

## Why it does not branch on score for M5

V1 fetched M5 only when the pre-M5 score reached its gate (>= 8). That gate is an
**analysis** eligibility rule (Arch §5: "the eligibility rule belongs to
analysis"; `V2Analyzer.analyze` owns it) and a branch that changes the signal's
tier — so, per Principle 12, it must NOT live here. The orchestrator therefore
requests the snapshot *with* M5 and lets `analyze` decide whether to use it: the
produced Signal is identical to V1's, and only the fetch cadence differs (M5 is
fetched for every symbol rather than post-gate). This is a documented,
behavior-neutral divergence in I/O cadence, in the same spirit as the adapters'
error-surfacing divergence — signals, alerts, and lifecycle state are unchanged.

## The fresh HTF the lifecycle kill/stale predicate needs

`lifecycle.reconciler.reconcile` takes the fresh cycle's `HTFContext` (the kill /
stale predicate reads the fresh D1/H4 biases regardless of the decision tier).
V1's `is_poi_invalidated` read `context["htf_map"]`, which V1 populated only once
a direction resolved *or* vetoed — it is ABSENT (→ "CHOPPY") for the
no_direction / invalid exits. `_kill_htf` reproduces exactly that: the candidate's
HTF when scored/not-ready, `None` when no direction resolved, and — for the
candidate-less *veto* case where V1's context still carried `htf_map` — the fresh
HTF recovered by a single pure `pipeline.run` (no state, no I/O, no M5).

## Error handling (V1 parity)

V1 swallowed failures and continued: a per-coin `try/except` skipped a bad
symbol, an empty universe was retried, and load/save/notify failures were
printed and ignored. The V2 adapters *surface* those as typed exceptions (the one
sanctioned divergence); this orchestrator re-applies V1's swallow-and-continue
policy at the edge, so lifecycle behavior is unchanged.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from v2.analysis.context import AnalysisContext
from v2.analysis.engine import V2Analyzer
from v2.domain import HTFContext, LifecycleDecision, Signal
from v2.infra.clock import Clock, SystemClock
from v2.infra.market_data import MarketDataAdapter, MarketDataError
from v2.infra.notifier import Notifier, NotifierError, TelegramNotifier
from v2.infra.persistence import PersistenceAdapter, PersistenceError
from v2.lifecycle import LifecycleConfig, Reconciler, SignalStore

__all__ = ["Orchestrator"]

# Operational constants (V1 config / run() literals). Orchestrator-owned because
# they gate *coordination*, not signal outcomes.
_TOP_COINS_LIMIT = 30       # config.TOP_COINS_LIMIT
_D1_MIN_ROWS = 10           # main.py:291 (len(df_d1) < 10 -> skip)
_H1_MIN_ROWS = 50           # main.py:291 (len(df_h1) < 50 -> skip)
_CYCLE_SLEEP_SECONDS = 60.0  # main.py:782 (sleep 60 between cycles)


class Orchestrator:
    """Coordinates one scan cycle (and the loop) across the V2 components.

    All collaborators are injected (constructor DI, Arch A6): tests supply fakes
    and never touch the network, filesystem, or Telegram; `create()` builds the
    production wiring. The lifecycle `Reconciler` is injectable but defaults to a
    real one over the injected clock; the `SignalStore` is created at
    :meth:`startup` from the persisted memory (it *is* that memory).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        market_data: MarketDataAdapter,
        persistence: PersistenceAdapter,
        notifier: Notifier,
        analyzer: V2Analyzer,
        config: LifecycleConfig = LifecycleConfig(),
        top_coins_limit: int = _TOP_COINS_LIMIT,
        reconciler: Optional[Reconciler] = None,
    ) -> None:
        self._clock = clock
        self._market = market_data
        self._persistence = persistence
        self._notifier = notifier
        self._analyzer = analyzer
        self._config = config
        self._top_coins_limit = top_coins_limit
        self._reconciler = reconciler or Reconciler(clock, config)
        self._store: Optional[SignalStore] = None

    # -- production wiring --------------------------------------------------

    @classmethod
    def create(cls, *, base_dir: str = ".") -> "Orchestrator":
        """Wire the real adapters (production). Not for tests — this constructs
        the live `BinanceHandler` (network) via `MarketDataAdapter.create()`."""
        return cls(
            clock=SystemClock(),
            market_data=MarketDataAdapter.create(),
            persistence=PersistenceAdapter(base_dir),
            notifier=TelegramNotifier(),
            analyzer=V2Analyzer(),
        )

    @property
    def store(self) -> Optional[SignalStore]:
        """The lifecycle memory (None until :meth:`startup`). For inspection."""
        return self._store

    # -- startup ------------------------------------------------------------

    def startup(self) -> None:
        """Load persisted memory into a fresh `SignalStore` (Arch §5 startup 1-2).

        Reproduces V1's `load_memory` / `load_killed_thesis`, including their
        swallow-to-empty behavior: the adapter *surfaces* a malformed store as
        `PersistenceError`; V1's policy was to start empty, so that is applied
        here. A missing file already yields an empty mapping (no error)."""
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

    def run_cycle(self) -> None:
        """One full scan cycle: fetch universe → per-symbol reconcile → maintain
        → persist (Arch §5 steps 3-5). Mirrors one iteration of V1's `run()`
        loop body."""
        if self._store is None:
            self.startup()

        try:
            universe = self._market.get_top_volume_symbols(self._top_coins_limit)
        except MarketDataError:
            # V1: get_top_volume_coins returns [] on failure -> `if not
            # top_coins: continue` (no maintenance, no save that cycle).
            return

        if not universe:
            return

        for entry in universe:
            self._process_symbol(entry.symbol)

        self._end_of_cycle()

    def run_forever(
        self,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        cycle_interval: float = _CYCLE_SLEEP_SECONDS,
        max_cycles: Optional[int] = None,
    ) -> None:
        """The production scan loop (Arch §5 step 6). `sleeper` and `max_cycles`
        are injected so tests bound and observe the loop without real sleeping;
        the inter-cycle sleep is cadence only and reads no clock."""
        self.startup()
        count = 0
        while max_cycles is None or count < max_cycles:
            self.run_cycle()
            count += 1
            if max_cycles is not None and count >= max_cycles:
                break
            sleeper(cycle_interval)

    # -- per symbol ---------------------------------------------------------

    def _process_symbol(self, symbol: str) -> None:
        """Analyze one symbol and reconcile it, isolated from the rest of the
        scan. Any failure (a surfaced infra error, or an unexpected one) skips
        just this symbol — V1's per-coin `try/except ... continue` (main.py:
        766-768). Terminal logging of the failure is Phase 5.2."""
        try:
            snapshot = self._market.get_market_snapshot(symbol, include_m5=True)
            if snapshot is None:
                return  # V1: D1/H1 None/empty -> continue
            if len(snapshot.d1) < _D1_MIN_ROWS or len(snapshot.h1) < _H1_MIN_ROWS:
                return  # V1: data-sufficiency guard (main.py:291)

            session_active = self._session_active()
            signal = self._analyzer.analyze(snapshot, session_active=session_active)
            htf = self._kill_htf(snapshot, signal, session_active)

            decision = self._reconciler.reconcile(
                signal,
                self._store.view(),
                htf=htf,
                live_price=snapshot.live_price,
                h1_last_ts=snapshot.h1_last_ts,
            )
            self._store.apply(decision)
            self._dispatch(decision)
        except Exception:
            # Per-symbol isolation: one bad symbol must not end the scan.
            return

    def _dispatch(self, decision: LifecycleDecision) -> None:
        """Route the decision's Alerts to the notifier (Arch §9: lifecycle
        decides, delivery executes). V1 marked signals alerted regardless of
        send success, so a `NotifierError` is swallowed (best-effort delivery) —
        the store already carries the alert metadata the reconciler baked in.
        LogEvent terminal delivery is Phase 5.2."""
        for alert in decision.alerts:
            try:
                self._notifier.send(alert)
            except NotifierError:
                continue

    # -- coordination helpers ----------------------------------------------

    def _session_active(self) -> bool:
        """V1's `get_session_mode() == "SNIPER"` (strategy.py:14-19), computed
        from the injected clock instead of the wall clock: SNIPER is a weekday
        (Mon-Fri) between 07:00 and 22:00 UTC; everything else is STRICT."""
        now = self._clock.now()
        return now.weekday() < 5 and 7 <= now.hour < 22

    def _kill_htf(
        self, snapshot, signal: Signal, session_active: bool
    ) -> Optional[HTFContext]:
        """The fresh `HTFContext` the lifecycle kill/stale predicate must see —
        reproducing which biases V1's `is_poi_invalidated` read from
        `context["htf_map"]` (present only past the direction gate):

        - scored / not-ready (candidate present): the candidate's HTF;
        - no_direction / invalid (`direction is None`): None — V1's context
          carried no `htf_map` there, so the predicate saw "CHOPPY";
        - veto (candidate-less but `direction` set): V1's context DID carry
          `htf_map`, so recover the fresh HTF via one pure `pipeline.run` (no M5,
          no state) — the analysis layer's own output, not a re-derivation here.
        """
        if signal.candidate is not None:
            return signal.candidate.htf
        if signal.direction is None:
            return None
        context = self._analyzer.pipeline.run(
            AnalysisContext.initial(snapshot, session_active=session_active)
        )
        return context.htf

    # -- end of cycle -------------------------------------------------------

    def _end_of_cycle(self) -> None:
        """TTL sweep → killed-thesis prune → persist (Arch §5 step 5; main.py:
        770-779). Dashboard rendering is Phase 5.2. A save failure is swallowed
        (V1's `save_memory` try/except printed and continued)."""
        assert self._store is not None
        self._store.apply(self._store.ttl_sweep(self._clock))
        self._store.prune_killed_theses(self._clock)
        try:
            self._persistence.save_active_signals(self._store.active)
            self._persistence.save_killed_theses(self._store.killed_theses)
        except PersistenceError:
            return
