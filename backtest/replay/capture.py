"""Lifecycle capture — §15. Every lifecycle transition becomes observable, with
zero change to production behaviour.

REPLAY_CONTRACT.md C-15.1 … C-15.8, C-16.4.

## The shape of it

```
Orchestrator(reconciler=RecordingReconciler(Reconciler(clock, config), recorder))
                                │                                        │
                                │  record_decision(...)  before returning │
                                ▼                                        ▼
ReplayPersistence ──► record_ledger_row / record_ob_audit / record_memory_snapshot
RecordingNotifier ──► record_alert                                CaptureRecorder
capturing writer  ──► record_writer_line                    (the single file owner)
                                                                         │
                                        lifecycle_events.jsonl ◄─────────┤
                                        memory_snapshots.jsonl ◄─────────┤
                                        signal_ledger.jsonl    ◄─────────┤
                                        retracement_ob_audit.jsonl ◄─────┤
                                        alerts.jsonl           ◄─────────┤
                                        active_pois.json, killed_thesis.json ◄─┘
```

## Why it cannot change behaviour

`RecordingReconciler` adds no branch, mutates nothing, and returns the inner
decision object unchanged — the same instance, not a copy (C-15.2). The single
production seam it uses is the `reconciler=` constructor slot that already exists
([orchestrator.py:120](v2/app/orchestrator.py#L120)), which is decision R-6: zero
production change, therefore zero risk. Everything else the recorder sees, it sees
because a sink it was already behind forwarded it.

It records **before** returning, so an exception in rendering or dispatch — both
of which happen after `reconcile` and are both swallowed by `_process_symbol` —
cannot lose a decision that was already applied to memory. That asymmetry is
precisely why C-2.1 makes this stream the source of truth for trade
instantiation and demotes the notifier to a cross-check.

## Why events are buffered for one cycle

C-15.2.1 / R-18. `seq`, `universe_rank` and the C-15.5 TTL residue all need
cycle-complete information: the universe the adapter served (symbols skipped
before `reconcile` never call `record_decision`, so call order is not rank
order), and the end-of-cycle memory snapshot. Buffer size is bounded by
`top_coins_limit`.

Buffering applies to exactly those artifacts whose schema needs that information
— `lifecycle_events.jsonl` and `memory_snapshots.jsonl`. The ledger, the OB
audit, the state files and `alerts.jsonl` are written through immediately,
because nothing in their schema depends on the rest of the cycle and their bytes
must stay identical to a pre-capture run.

## Single ownership

C-15.2.1: one object owns every file, so two components can never disagree about
a run's contents. The ledger, audit and state writes are delegated to a
production `PersistenceAdapter` that this recorder owns — the on-disk format,
the atomic-save discipline and the error type are then identical to live by
construction rather than by a second implementation kept in step by hand.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from backtest.capture_schema import (
    CapturedEvent,
    KIND_ALERT,
    KIND_KILLED_THESIS,
    KIND_LOG,
    KIND_MUTATION,
    LIFECYCLE_EVENTS_FILENAME,
    MEMORY_SNAPSHOTS_FILENAME,
    MemorySnapshot,
    PHASE_END_OF_CYCLE,
    PHASE_SYMBOL,
)
from v2.domain.active_signal import ActiveSignal
from v2.domain.killed_thesis import KilledThesisRecord
from v2.domain.lifecycle import (
    Alert,
    EvictMutation,
    LifecycleDecision,
    ReplaceMutation,
)
from v2.domain.ob_audit import OBAuditRecord
from v2.domain.signal_ledger import SignalLedgerRecord
from v2.infra.persistence import PersistenceAdapter

__all__ = [
    "ALERTS_FILENAME",
    "CaptureError",
    "CaptureRecorder",
    "CycleCapture",
    "LIFECYCLE_EVENTS_FILENAME",
    "MEMORY_SNAPSHOTS_FILENAME",
    "RecordingReconciler",
    "TTL_RENDERED_MARKER",
]

ALERTS_FILENAME = "alerts.jsonl"

H1_SECONDS = 3600

#: The rendered TTL line ([rendering.py:252-259](v2/app/rendering.py#L252)). Used
#: **only** to count lines for the C-15.5 cross-check — never parsed for data.
TTL_RENDERED_MARKER = "[⏰ TTL EXPIRED]"


class CaptureError(Exception):
    """The capture layer cannot vouch for what it recorded.

    Raised only for a broken *invariant*, never for ordinary market conditions:
    the C-15.5 TTL cross-check failing, or the C-15.7 alert cross-check failing.
    Either means the dataset a run produced does not describe what the run did,
    which is worse than no dataset at all.
    """


# ---------------------------------------------------------------------------
# The observing reconciler (C-15.2)
# ---------------------------------------------------------------------------

class RecordingReconciler:
    """A `Reconciler` that also tells the recorder what it decided.

    Deliberately **not** a subclass: it wraps. Subclassing would inherit the
    reconciler's internals and make it possible to accidentally re-implement one
    of them; wrapping can only forward. The orchestrator types this slot as
    `Optional[Reconciler]` but calls exactly one method on it, so structural
    substitution is all that is required — and a test asserts the two signatures
    still match.
    """

    def __init__(self, inner: Any, recorder: "CaptureRecorder") -> None:
        self.inner = inner
        self.recorder = recorder

    def reconcile(
        self,
        signal,
        memory,
        *,
        htf,
        live_price: float,
        h1_last_ts: float,
    ) -> LifecycleDecision:
        decision = self.inner.reconcile(
            signal, memory, htf=htf, live_price=live_price, h1_last_ts=h1_last_ts
        )
        # Recorded before returning (C-15.2): everything downstream of this call
        # can raise and be swallowed, but the decision has already been made and
        # is about to be applied.
        self.recorder.record_decision(
            signal.symbol, h1_last_ts, decision, memory_size=len(memory)
        )
        return decision


# ---------------------------------------------------------------------------
# Per-cycle result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CycleCapture:
    """What one cycle produced, returned by `end_cycle` for the health line.

    `decisions` is the C-15.8 count: symbols for which a `LifecycleDecision` was
    captured. `ttl_inferred` and `ttl_log_lines` are the two sides of the C-15.5
    cross-check, recorded even when they agree so that a healthy run carries the
    evidence of its own health.
    """

    cycle_index: int
    scan_T: int
    events: int
    decisions: int
    ttl_inferred: int
    ttl_log_lines: int
    alerts: int
    alerts_from_decisions: int
    snapshot_written: bool


# ---------------------------------------------------------------------------
# The recorder (C-15.2.1)
# ---------------------------------------------------------------------------

class CaptureRecorder:
    """The sole writer of a run's capture bundle.

    Construction creates the run directory and the production persistence
    adapter it delegates to; nothing is written until something is recorded.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        run_id: str = "",
        source: str = "replay",
        capture_console: bool = False,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._persistence = PersistenceAdapter(str(self._run_dir))
        self._run_id = run_id
        self._source = source
        self._capture_console = capture_console

        self._events_path = self._run_dir / LIFECYCLE_EVENTS_FILENAME
        self._snapshots_path = self._run_dir / MEMORY_SNAPSHOTS_FILENAME
        self._alerts_path = self._run_dir / ALERTS_FILENAME

        # -- run-level tallies (observability, never inputs) ---------------
        self.ledger_rows = 0
        self.ob_audit_rows = 0
        self.saves = 0
        self.events_written = 0
        self.alerts: list[Alert] = []
        self.console: list[str] = []

        # -- per-cycle state ----------------------------------------------
        self._cycle_index: int = -1
        self._scan_T: int = 0
        self._open = False
        self._buffer: list[tuple[str, float, LifecycleDecision]] = []
        self._cycle_alerts: list[Alert] = []
        self._ttl_log_lines = 0
        self._snapshot: Optional[tuple[dict[str, dict], dict[str, float]]] = None
        self._previous_active: dict[str, dict] = {}
        self._alert_seq = 0

    # -- identity -----------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def snapshots_path(self) -> Path:
        return self._snapshots_path

    @property
    def in_cycle(self) -> bool:
        """Whether a cycle bracket is currently open."""
        return self._open

    @property
    def cycle_index(self) -> int:
        return self._cycle_index

    def set_identity(self, *, run_id: str, source: str = "replay") -> None:
        """Stamp the run identity. Separate from construction because the sinks
        are built before the driver knows nothing else about them — the `run_id`
        is a content hash of the configuration, computed once, upstream."""
        self._run_id = run_id
        self._source = source

    # -- cycle brackets (C-8.3) --------------------------------------------

    def begin_cycle(self, cycle_index: int, scan_T: int) -> None:
        """Open a cycle: reset `seq` and the per-cycle buffers."""
        self._cycle_index = int(cycle_index)
        self._scan_T = int(scan_T)
        self._open = True
        self._buffer = []
        self._cycle_alerts = []
        self._ttl_log_lines = 0
        self._snapshot = None

    def end_cycle(
        self,
        processed: bool = True,
        *,
        universe: tuple[str, ...] = (),
    ) -> CycleCapture:
        """Flush the cycle: expand decisions into lines, attribute the TTL
        residue, write the snapshot, and run both cross-checks.

        `universe` is the symbol order the adapter served this cycle. It is the
        source for `universe_rank` — **not** the order of `record_decision`
        calls, which omits every symbol skipped before `reconcile` (C-15.2.1).
        """
        if not self._open:
            raise CaptureError(
                "end_cycle without begin_cycle: the cycle brackets assign `seq` "
                "and cannot be inferred after the fact (C-15.2.1)")

        rank_of = {symbol: index for index, symbol in enumerate(universe, 1)}
        lines: list[CapturedEvent] = []
        seq = 0
        for symbol, h1_last_ts, decision in self._buffer:
            for event in self._expand(
                symbol, h1_last_ts, decision, rank_of.get(symbol)
            ):
                lines.append(_with_seq(event, seq))
                seq += 1

        alerts_from_decisions = [
            alert
            for _symbol, _ts, decision in self._buffer
            for alert in decision.alerts
        ]

        # -- C-15.5: the TTL residue, then the snapshot it was derived from --
        ttl_symbols: tuple[str, ...] = ()
        if self._snapshot is not None:
            ttl_symbols = self._infer_ttl(self._snapshot[0])
            for symbol in ttl_symbols:
                lines.append(_with_seq(self._ttl_event(symbol), seq))
                seq += 1

        self._append_events(lines)

        snapshot_written = False
        if self._snapshot is not None:
            active, killed = self._snapshot
            self._append_snapshot(active, killed)
            self._previous_active = active
            snapshot_written = True

        self._cross_check_ttl(len(ttl_symbols))
        self._cross_check_alerts(alerts_from_decisions)

        capture = CycleCapture(
            cycle_index=self._cycle_index,
            scan_T=self._scan_T,
            events=len(lines),
            decisions=len(self._buffer),
            ttl_inferred=len(ttl_symbols),
            ttl_log_lines=self._ttl_log_lines,
            alerts=len(self._cycle_alerts),
            alerts_from_decisions=len(alerts_from_decisions),
            snapshot_written=snapshot_written,
        )
        self._open = False
        return capture

    # -- recording surfaces -------------------------------------------------

    def record_decision(
        self,
        symbol: str,
        h1_last_ts: float,
        decision: LifecycleDecision,
        *,
        memory_size: int = 0,
    ) -> None:
        """Buffer one `LifecycleDecision` (C-15.2).

        `memory_size` is accepted because C-15.2's signature specifies it. It is
        not written to the event schema: C-15.4's snapshot records memory
        exactly, per cycle, and a per-event copy of its size would be a second
        source for one fact.
        """
        if not self._open:
            raise CaptureError(
                f"record_decision for {symbol} outside a cycle bracket: `seq` is "
                f"assigned per cycle (C-15.3)")
        self._buffer.append((symbol, float(h1_last_ts), decision))

    def record_memory_snapshot(
        self,
        active: Mapping[str, ActiveSignal],
        killed: Mapping[str, KilledThesisRecord],
    ) -> None:
        """C-15.4. Called once per processed cycle, after the TTL sweep."""
        self._snapshot = (
            {symbol: signal.to_v1_dict() for symbol, signal in active.items()},
            {key: float(record.killed_ts) for key, record in killed.items()},
        )

    def record_alert(self, alert: Alert) -> None:
        """C-15.7 — the cross-check channel. Written through immediately so the
        file's bytes are what a pre-capture run produced."""
        self.alerts.append(alert)
        self._cycle_alerts.append(alert)
        self._alert_seq += 1
        line = {
            "run_id": self._run_id,
            "cycle_index": self._cycle_index,
            "seq": self._alert_seq,
            "alert": _to_jsonable(alert),
        }
        with open(self._alerts_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")

    def record_writer_line(self, line: str) -> None:
        """The captured console. A **validation channel only** (C-15.5): the TTL
        lines are counted, never parsed. Retaining the text is opt-in because a
        five-year run renders tens of millions of lines."""
        if TTL_RENDERED_MARKER in line:
            self._ttl_log_lines += 1
        if self._capture_console:
            self.console.append(line)

    def record_ledger_row(self, record: SignalLedgerRecord) -> None:
        self._persistence.append_signal_ledger(record)
        self.ledger_rows += 1

    def record_ob_audit(self, record: OBAuditRecord) -> None:
        self._persistence.append_ob_audit(record)
        self.ob_audit_rows += 1

    def save_active_signals(self, signals: Mapping[str, ActiveSignal]) -> None:
        self.saves += 1
        self._persistence.save_active_signals(signals)

    def save_killed_theses(self, theses: Mapping[str, KilledThesisRecord]) -> None:
        self._persistence.save_killed_theses(theses)

    # -- expansion (C-15.3) -------------------------------------------------

    def _expand(
        self,
        symbol: str,
        h1_last_ts: float,
        decision: LifecycleDecision,
        universe_rank: Optional[int],
    ) -> list[CapturedEvent]:
        """One decision to one line per mutation, alert, log event and
        killed-thesis record — in the tuple order of `LifecycleDecision`
        ([lifecycle.py:181-184](v2/domain/lifecycle.py#L181)).

        C-15.3 states that order twice and the two statements disagree: the prose
        says "per mutation, per log event, per alert", the cross-reference says
        "mirroring the tuple order of `LifecycleDecision`", which is mutations,
        **alerts**, log_events, killed_theses. The cross-reference names a
        specific source location and is therefore the verifiable one, so it wins
        — and the choice is pinned by a test rather than left to whoever reads
        the clause next. See the M9 findings.
        """
        base = dict(
            run_id=self._run_id,
            source=self._source,
            cycle_index=self._cycle_index,
            scan_T=self._scan_T,
            h1_last_ts=h1_last_ts,
            symbol=symbol,
            universe_rank=universe_rank,
            phase=PHASE_SYMBOL,
            attribution="reconciler",
        )
        events: list[CapturedEvent] = []

        for mutation in decision.mutations:
            events.append(CapturedEvent(
                seq=0, kind=KIND_MUTATION,
                mutation_type=type(mutation).__name__,
                mutation_payload=_mutation_payload(mutation),
                evict_reason=getattr(mutation, "reason", None),
                target_symbol=(mutation.target_symbol
                               if isinstance(mutation, ReplaceMutation) else None),
                **base,
            ))

        for alert in decision.alerts:
            events.append(CapturedEvent(
                seq=0, kind=KIND_ALERT,
                alert_kind=alert.kind,
                alert_tier=alert.dedup_key,
                alert_payload=_alert_payload(alert),
                **base,
            ))

        for log_event in decision.log_events:
            events.append(CapturedEvent(
                seq=0, kind=KIND_LOG,
                log_event_type=log_event.event_type,
                log_details=dict(log_event.details),
                **base,
            ))

        for record in decision.killed_theses:
            events.append(CapturedEvent(
                seq=0, kind=KIND_KILLED_THESIS,
                mutation_payload={"thesis_key": record.thesis_key,
                                  "killed_ts": float(record.killed_ts)},
                **base,
            ))

        if not events:
            # "reconcile ran and decided nothing" must stay distinguishable from
            # "reconcile never ran" — required by C-15.8's `decisions` count.
            events.append(CapturedEvent(seq=0, kind=KIND_LOG, **base))
        return events

    # -- C-15.5: TTL attribution by elimination -----------------------------

    def _infer_ttl(self, current_active: Mapping[str, dict]) -> tuple[str, ...]:
        """`TTL_k = R_k \\ A_k` — the symbols that left memory this cycle without
        a captured reconciler mutation to account for them.

        Complete, not heuristic: Lemma 1 of C-15.5 shows a symbol can leave
        `SignalStore._active` only through `ReplaceMutation` or `EvictMutation`,
        and Lemma 2 shows the reconciler is the origin of every such mutation
        except the store's own TTL sweep. So a removal this layer did not observe
        can only be the sweep.
        """
        removed = [symbol for symbol in self._previous_active
                   if symbol not in current_active]
        if not removed:
            return ()
        accounted = set()
        for _symbol, _ts, decision in self._buffer:
            for mutation in decision.mutations:
                if isinstance(mutation, ReplaceMutation):
                    accounted.add(mutation.target_symbol)
                elif isinstance(mutation, EvictMutation):
                    accounted.add(mutation.symbol)
        # Iteration follows the previous snapshot's insertion order, so the
        # residue is deterministic without sorting (§18: dict order is a pure
        # function of the cycle history).
        return tuple(symbol for symbol in removed if symbol not in accounted)

    def _ttl_event(self, symbol: str) -> CapturedEvent:
        return CapturedEvent(
            run_id=self._run_id,
            source=self._source,
            cycle_index=self._cycle_index,
            scan_T=self._scan_T,
            # No per-symbol reconcile produced this line, so the contract's own
            # formula supplies the reference candle: `h1_last_ts = T - 3600`.
            h1_last_ts=float(self._scan_T - H1_SECONDS),
            seq=0,
            symbol=symbol,
            universe_rank=None,
            phase=PHASE_END_OF_CYCLE,
            kind=KIND_MUTATION,
            mutation_type="EvictMutation",
            mutation_payload={"symbol": symbol, "reason": "ttl"},
            evict_reason="ttl",
            attribution="inferred_ttl",
        )

    def _cross_check_ttl(self, inferred: int) -> None:
        """C-15.5's mandatory cross-check: the residue must equal the number of
        rendered TTL lines. A mismatch fails the run."""
        if inferred != self._ttl_log_lines:
            raise CaptureError(
                f"TTL attribution mismatch in cycle {self._cycle_index} "
                f"(T={self._scan_T}): the snapshot differ inferred {inferred} TTL "
                f"eviction(s) but the writer rendered {self._ttl_log_lines} TTL "
                f"line(s). C-15.5's elimination argument is complete, so a "
                f"mismatch means a symbol left memory through a path this layer "
                f"cannot see — the capture would understate the run.")

    def _cross_check_alerts(self, from_decisions: list[Alert]) -> None:
        """C-15.7: the notifier's content must equal the alerts carried by the
        captured decisions.

        A mismatch is exactly the failure C-2.1 anticipates — `_render_symbol`
        raising between `store.apply` and `_dispatch`, so an applied decision
        never reaches the notifier. The decision stream is still correct, but the
        run has a swallowed exception in it, and C-15.8's policy for those is to
        fail rather than let a partial dataset look complete.
        """
        if self._cycle_alerts == from_decisions:
            return
        raise CaptureError(
            f"alert cross-check failed in cycle {self._cycle_index} "
            f"(T={self._scan_T}): the notifier received {len(self._cycle_alerts)} "
            f"alert(s) but the captured decisions carry "
            f"{len(from_decisions)}. Dispatch happens after rendering, which the "
            f"orchestrator swallows, so the difference is a swallowed exception "
            f"between `store.apply` and `_dispatch` (C-15.7/C-15.8).")

    # -- writing ------------------------------------------------------------

    def _append_events(self, events: list[CapturedEvent]) -> None:
        if not events:
            return
        with open(self._events_path, "a", encoding="utf-8") as handle:
            for event in events:
                handle.write(event.to_json_line())
        self.events_written += len(events)

    def _append_snapshot(self, active: dict[str, dict],
                         killed: dict[str, float]) -> None:
        snapshot = MemorySnapshot(
            run_id=self._run_id,
            source=self._source,
            cycle_index=self._cycle_index,
            scan_T=self._scan_T,
            active=active,
            killed=killed,
        )
        with open(self._snapshots_path, "a", encoding="utf-8") as handle:
            handle.write(snapshot.to_json_line())


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _with_seq(event: CapturedEvent, seq: int) -> CapturedEvent:
    """`seq` is assigned at flush time, so the buffered events carry a
    placeholder and are stamped here. Frozen dataclasses are replaced, never
    mutated."""
    return dataclasses.replace(event, seq=seq)


def _mutation_payload(mutation: Any) -> dict:
    """The mutation dataclass as a dict, verbatim, with any nested
    `ActiveSignal` rendered via `to_v1_dict()` (C-15.3).

    `asdict` would recurse into the nested `ActiveSignal` and produce its *field*
    shape, not its persisted shape. C-15.3 asks for the persisted shape, so the
    nesting is handled explicitly — one schema for an `ActiveSignal` everywhere
    it appears, which is what makes `memory_snapshots` and `lifecycle_events`
    comparable line for line.
    """
    payload: dict[str, Any] = {}
    for name, value in vars(mutation).items():
        if isinstance(value, ActiveSignal):
            payload[name] = value.to_v1_dict()
        else:
            payload[name] = value
    return payload


def _alert_payload(alert: Alert) -> dict:
    """The full `Alert` as a dict, with `htf` as a list of `[name, value]` lists
    (C-15.3) — JSON has no tuples, and a list of pairs round-trips where a dict
    would silently drop a duplicated debug key."""
    payload = asdict(alert)
    payload["htf"] = [[name, value] for name, value in alert.htf]
    return payload


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value
