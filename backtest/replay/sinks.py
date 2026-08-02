"""Replay's output destinations — the third of the three swapped dependencies.

"Replay is the same bot with a different clock, a different market-data source
and a different **persistence destination**." This module is that destination.

## ReplayPersistence

C-15.6: the six methods the orchestrator uses, with two behavioural differences
and no others.

1. **Loads return empty.** Replay MUST start from empty lifecycle memory
   (C-5.7.3) — never from live `active_pois.json`. Otherwise the first cycles
   would inherit whatever the live bot happened to be holding, and the run would
   not be a function of its own window.
2. **Everything is written under `runs/{run_id}/`.** Production live state is
   never touched by a replay.

Everything else *delegates to the production `PersistenceAdapter`*, pointed at
the run directory. That is deliberate: the on-disk format, the atomic-save
discipline, the JSONL append semantics and the error type are then identical to
live by construction rather than by a second implementation that has to be kept
in step. The only thing that changes is where the files land.

## Both sinks are thin forwarders (M9)

C-15.2.1 makes `CaptureRecorder` the **sole writer** of every file in the run
bundle, so neither class here holds a file of its own any more: they forward to
the recorder, which owns the `PersistenceAdapter` described above and therefore
still produces byte-identical output. A recorder is created implicitly when none
is supplied, so the single-owner invariant holds however these are constructed.

`save_active_signals` and `save_killed_theses` additionally forward the C-15.4
memory snapshot — **before** the write that may raise, on the same principle as
C-15.2's record-before-return: a failed file write must not also cost us the
observation.

## RecordingNotifier

C-15.7: records alerts instead of delivering them, and **never raises**. It is a
cross-check channel, not the source of truth — `Alert` carries no time or
symbol-cycle identity of its own, and dispatch happens after two steps that could
raise, so the decision stream remains authoritative for trade instantiation
(C-2.1).

It cannot reach a network: there is no client, no token, and no transport in this
module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from backtest.replay.capture import ALERTS_FILENAME, CaptureRecorder
from v2.domain.active_signal import ActiveSignal
from v2.domain.killed_thesis import KilledThesisRecord
from v2.domain.lifecycle import Alert
from v2.domain.ob_audit import OBAuditRecord
from v2.domain.signal_ledger import SignalLedgerRecord

__all__ = ["ALERTS_FILENAME", "RecordingNotifier", "ReplayPersistence"]


class ReplayPersistence:
    """A `PersistencePort` whose destination is one run's directory."""

    def __init__(
        self,
        run_dir: Path | str,
        *,
        recorder: Optional[CaptureRecorder] = None,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._recorder = (recorder if recorder is not None
                          else CaptureRecorder(self._run_dir))
        self._active: dict[str, ActiveSignal] = {}
        self._killed: dict[str, KilledThesisRecord] = {}

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def recorder(self) -> CaptureRecorder:
        return self._recorder

    @property
    def ledger_rows(self) -> int:
        return self._recorder.ledger_rows

    @property
    def ob_audit_rows(self) -> int:
        return self._recorder.ob_audit_rows

    @property
    def saves(self) -> int:
        return self._recorder.saves

    # -- loads: always empty (C-5.7.3) ------------------------------------

    def load_active_signals(
        self, *, default_timestamp: Optional[float] = None
    ) -> dict[str, ActiveSignal]:
        """Empty lifecycle memory. A replay never inherits live state, and never
        inherits a previous replay's state either — re-running a window must
        reproduce it, not continue it."""
        return {}

    def load_killed_theses(self) -> dict[str, KilledThesisRecord]:
        return {}

    # -- saves and appends: the production writer, run-scoped -------------

    def save_active_signals(self, signals: Mapping[str, ActiveSignal]) -> None:
        self._active = dict(signals)
        self._recorder.record_memory_snapshot(self._active, self._killed)
        self._recorder.save_active_signals(signals)

    def save_killed_theses(self, theses: Mapping[str, KilledThesisRecord]) -> None:
        self._killed = dict(theses)
        self._recorder.record_memory_snapshot(self._active, self._killed)
        self._recorder.save_killed_theses(theses)

    def append_ob_audit(self, record: OBAuditRecord) -> None:
        self._recorder.record_ob_audit(record)

    def append_signal_ledger(self, record: SignalLedgerRecord) -> None:
        self._recorder.record_ledger_row(record)


class RecordingNotifier:
    """A `Notifier` that records alerts and delivers nothing.

    `send` never raises. The orchestrator already swallows `NotifierError`
    ([orchestrator.py:307](v2/app/orchestrator.py#L307)), so raising here would be
    invisible *and* would lose the alert — the opposite of what a cross-check
    channel is for.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        recorder: Optional[CaptureRecorder] = None,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._recorder = (recorder if recorder is not None
                          else CaptureRecorder(self._run_dir))

    @property
    def recorder(self) -> CaptureRecorder:
        return self._recorder

    @property
    def alerts(self) -> list[Alert]:
        return self._recorder.alerts

    @property
    def run_id(self) -> str:
        return self._recorder.run_id

    def set_cycle(self, *, run_id: str, cycle_index: int) -> None:
        """Position the recorder. `Alert` has no identity of its own, so the run,
        cycle and sequence are stamped from outside (C-15.7).

        Retained for the M7 call site and for symmetry with the port surface; the
        driver now brackets cycles on the recorder directly (C-8.3), which is the
        one place that knows the cycle index.
        """
        self._recorder.set_identity(run_id=run_id)
        if not self._recorder.in_cycle:
            self._recorder.begin_cycle(cycle_index, 0)

    def send(self, alert: Alert) -> None:
        self._recorder.record_alert(alert)
