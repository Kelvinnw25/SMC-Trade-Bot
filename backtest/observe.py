"""Operational observability for long-running research workflows.

A five-year replay runs for days. A survivorship-free acquisition walks tens of
thousands of archives. During those hours the operator has exactly one question —
*is this working?* — and the platform previously answered it with silence.

This module supplies the answer, and nothing else. It is deliberately the least
scientific code in the repository: nothing here is read by replay, by scoring, by
the outcome engine, or by metrics. Deleting every file it writes changes no
result. That property is what makes it safe to add to a frozen platform.

## The four categories (and which one this module owns)

| Category | Test: if deleted, the result… | Owner |
|---|---|---|
| **Progress reporting** | is unchanged | **this module** — transient, stderr |
| **Operational logging** | is unchanged, but harder to explain | **this module** — `ops.jsonl` |
| **Scientific evidence** | *changes* | the sinks, the outcome engine |
| **Audit trail** | becomes unidentifiable | the manifests |

Progress and operational logging are the two disposable tiers. This module writes
only those. It never opens an evidence file, never touches a manifest, and never
computes anything a report would quote.

## Blindness to results

An operator watching a replay must be able to see that the machine is healthy
without seeing how the strategy is doing. Watching expectancy accumulate over
thirty hours is how a researcher forms a conclusion before the falsification
protocol is applied — and having formed it, they will read the finished report
looking for confirmation.

The rule this module enforces mechanically:

    **Counts are permitted. Ratios and R-denominated quantities are not.**

A cumulative ledger-row count is throughput: it says the machine is working, and
zero rows after a thousand cycles is a genuine fault signal. A win rate is
performance. `assert_operational` rejects the second class by name, so a future
caller cannot quietly widen the heartbeat into a live scoreboard.

## Stall detection

"Alive" and "progressing" are different questions, and conflating them makes the
worst failure mode — hung but running — look healthy. Every heartbeat therefore
carries both a wall-clock timestamp *and* a monotonic work counter. An unchanged
timestamp means the process died; an unchanged counter across heartbeats means it
stalled.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Optional

__all__ = [
    "FORBIDDEN_FIELDS",
    "FORBIDDEN_SUFFIXES",
    "HEARTBEAT_INTERVAL_SECONDS",
    "Heartbeat",
    "JobRecorder",
    "OPS_LOG_NAME",
    "OperationalLog",
    "PerformanceLeak",
    "Progress",
    "STALL_AFTER_SECONDS",
    "assert_operational",
    "cpu_fraction",
    "human_duration",
    "preserve_failed_run",
    "rss_bytes",
    "stderr_sink",
]

#: One line per minute. A 100-hour replay renders ~6,000 lines: enough to
#: reconstruct a throughput curve afterwards, few enough to read. Per-cycle
#: reporting would be 43,800 lines of noise that nobody greps twice.
HEARTBEAT_INTERVAL_SECONDS = 60.0

#: No advance for this long, while work remains, is a stall rather than slowness.
#: Deliberately well above the heartbeat interval: a single slow unit is not a
#: fault, and crying stall on one is how operators learn to ignore the signal.
STALL_AFTER_SECONDS = 300.0

OPS_LOG_NAME = "ops.jsonl"

#: Field names that would turn a heartbeat into a live scoreboard. Substring
#: matching, because `current_win_rate` and `win_rate_so_far` are the same
#: mistake wearing different names.
FORBIDDEN_FIELDS: tuple[str, ...] = (
    "win_rate", "loss_rate", "expectancy", "profit_factor", "payoff",
    "drawdown", "equity", "pnl", "r_multiple",
    "gross_profit", "gross_loss", "avg_win", "avg_loss", "mae", "mfe",
    "sharpe", "metrics_hash", "profitability",
)
# Deliberately absent: "edge". It matches no field the metrics layer defines,
# and as a substring it rejects `ledger_rows` — the single most useful
# throughput count a replay operator has. A guard that blocks the information
# the operator needs teaches them to work around it.

#: R-denominated by naming convention (`max_drawdown_r`, `avg_excursion_r`).
#: Matched as a *suffix*, not a substring: `_r` anywhere would reject
#: `ledger_rows` and `bytes_read`, which are throughput counts and exactly the
#: information the operator is entitled to see.
FORBIDDEN_SUFFIXES: tuple[str, ...] = ("_r",)


class PerformanceLeak(Exception):
    """An operational record carried a strategy-performance field."""


def assert_operational(payload: dict[str, Any]) -> None:
    """Reject a record that would expose intermediate strategy performance.

    Called on every heartbeat and every operational-log record. The check is on
    *names*, not values, because a name is what a future caller writes when they
    decide the heartbeat should show "just the win rate, it's useful".
    """
    for key in payload:
        lowered = key.lower()
        hit = any(banned in lowered for banned in FORBIDDEN_FIELDS) or \
            any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
        if hit:
            raise PerformanceLeak(
                f"operational record carries {key!r}, which exposes strategy "
                f"performance while the job is still running. Counts are "
                f"permitted; ratios and R-denominated quantities are not.")


# ---------------------------------------------------------------------------
# Resource sampling — stdlib only, best effort, never fatal
# ---------------------------------------------------------------------------

def rss_bytes() -> Optional[int]:
    """Resident set size, or `None` where it cannot be read.

    Best effort by design: memory is a diagnostic, and a platform that will not
    report it is not a reason to fail a hundred-hour job. No dependency is
    introduced for it (`psutil` is not installed and is not worth installing for
    one number).
    """
    if sys.platform == "win32":                             # pragma: no cover
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            # Without these, ctypes defaults the return of `GetCurrentProcess`
            # to a C `int` and truncates the pseudo-handle (-1) on 64-bit, so
            # the call fails and reports zero bytes. Measured: it returns 0
            # before these three lines and 1 after.
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters),
                counters.cb)
            return int(counters.WorkingSetSize) if ok else None
        except Exception:
            return None
    try:                                                    # pragma: no cover
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, the BSDs and macOS report bytes.
        return int(usage) if sys.platform == "darwin" else int(usage) * 1024
    except Exception:
        return None


def cpu_fraction(cpu_seconds: float, wall_seconds: float) -> Optional[float]:
    """CPU seconds per wall second — 1.0 is one core saturated.

    Derived from `time.process_time()`, so it needs no platform support and no
    dependency. Below ~1.0 on a single-threaded job means time is going to I/O,
    which is exactly the distinction an operator needs when throughput drops.
    """
    if wall_seconds <= 0:
        return None
    return round(cpu_seconds / wall_seconds, 3)


def human_duration(seconds: Optional[float]) -> str:
    """`H:MM:SS`, or `-` when there is nothing to say."""
    if seconds is None or seconds < 0 or seconds != seconds:    # NaN-safe
        return "-"
    total = int(seconds)
    return f"{total // 3600}:{(total // 60) % 60:02d}:{total % 60:02d}"


def _human_bytes(value: Optional[int]) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"                                 # pragma: no cover


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The heartbeat record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Heartbeat:
    """One observation of a running job. Progress tier: transient by nature.

    Both rates are reported deliberately. `rate_cumulative` is the average since
    the job started; `rate_trailing` is measured over a recent window. Their
    *divergence* is the diagnostic — trailing below cumulative means throughput is
    degrading (memory pressure, cache eviction, a structure growing without
    bound), and that is the signal an operator needs at hour 20 rather than at
    hour 37. A single blended number would hide it.
    """

    sequence: int
    at: str
    phase: str
    unit: str
    completed: int
    total: Optional[int]
    elapsed_seconds: float
    rate_trailing: Optional[float]
    rate_cumulative: Optional[float]
    eta_seconds: Optional[float]
    stalled: bool
    detail: str = ""
    retries: int = 0
    bytes_done: int = 0
    skipped: int = 0
    failures: int = 0
    rss_bytes: Optional[int] = None
    cpu_fraction: Optional[float] = None
    #: How to label the throughput figure. Replay sets `" replay-h/s"`, because
    #: one cycle is exactly one H1 and "1.51/s" hides the fact that the operator
    #: is being told simulated hours per wall second.
    rate_name: str = "/s"

    @property
    def percent(self) -> Optional[float]:
        """Only ever computed against an enumerated total.

        A percentage of un-enumerated work is a guess wearing a number's
        clothing; every pipeline here plans before it executes, so the honest
        answer is always available or absent, never invented.
        """
        if not self.total:
            return None
        return round(100.0 * self.completed / self.total, 2)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "heartbeat",
            "sequence": self.sequence,
            "at": self.at,
            "phase": self.phase,
            "unit": self.unit,
            "completed": self.completed,
            "total": self.total,
            "percent": self.percent,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "rate_trailing": self.rate_trailing,
            "rate_cumulative": self.rate_cumulative,
            "eta_seconds": self.eta_seconds,
            "stalled": self.stalled,
            "detail": self.detail,
            "retries": self.retries,
            "skipped": self.skipped,
            "failures": self.failures,
            "bytes_done": self.bytes_done,
            "rss_bytes": self.rss_bytes,
            "cpu_fraction": self.cpu_fraction,
        }
        assert_operational(payload)
        return payload

    def render(self) -> str:
        """One compact line for a human reading stderr over SSH.

        **ASCII only, deliberately.** The primary console here is Windows, whose
        default code page renders `·` and `—` as mojibake — and a status line an
        operator has learned to squint at is a status line they stop reading.
        Nothing in this string needs a character outside 7-bit ASCII.
        """
        head = f"[{self.phase}]"
        if self.total:
            body = f"{self.percent:5.1f}% {self.completed:,}/{self.total:,} {self.unit}"
        else:
            body = f"{self.completed:,} {self.unit}"
        parts = [head, body]
        if self.rate_trailing is not None:
            cumulative = ("" if self.rate_cumulative is None
                          else f" (cum {self.rate_cumulative:.2f})")
            parts.append(
                f"{self.rate_trailing:.2f}{self.rate_name}{cumulative}")
        parts.append(f"elapsed {human_duration(self.elapsed_seconds)}")
        if self.eta_seconds is not None:
            parts.append(f"eta {human_duration(self.eta_seconds)}")
        if self.bytes_done:
            parts.append(_human_bytes(self.bytes_done))
        if self.retries:
            parts.append(f"retries {self.retries}")
        if self.skipped:
            parts.append(f"skipped {self.skipped}")
        if self.failures:
            parts.append(f"FAILED {self.failures}")
        if self.rss_bytes is not None:
            parts.append(f"rss {_human_bytes(self.rss_bytes)}")
        if self.stalled:
            parts.append("** STALLED **")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


def stderr_sink(line: str) -> None:
    """The default progress sink. stderr, flushed, so it survives redirection of
    stdout into a results file and appears immediately under `nohup`."""
    print(line, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Operational log — persistent, disposable
# ---------------------------------------------------------------------------

class OperationalLog:
    """Append-only JSONL of what the machine did, beside the artifacts it made.

    Persistent but disposable: losing it makes a run harder to explain and does
    not make it wrong. That is precisely why it is a separate file from every
    evidence sink — so an operator can delete it without a moment's thought about
    whether they have just damaged a result.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        assert_operational(record)
        payload = dict(record)
        payload.setdefault("at", _utc_now())
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True,
                                    ensure_ascii=True, default=str) + "\n")


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

class Progress:
    """A rate-limited heartbeat over an enumerated unit of work.

    Not a progress bar. Bars assume a TTY, break when output is redirected, do
    not survive detachment, and carry no history — and the operator of a
    multi-day job is checking in tomorrow morning, from another machine, after
    the terminal has closed. An append-only line at a fixed interval works in a
    file, over SSH, after `nohup`, and preserves the throughput curve for
    diagnosis afterwards.

    `total` may be `None` for work that genuinely cannot be enumerated; the
    percentage and ETA are then omitted rather than invented.
    """

    def __init__(
        self,
        total: Optional[int] = None,
        *,
        phase: str = "job",
        unit: str = "items",
        sink: Optional[Callable[[str], None]] = stderr_sink,
        log: Optional[OperationalLog] = None,
        interval: float = HEARTBEAT_INTERVAL_SECONDS,
        stall_after: float = STALL_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        cpu_clock: Callable[[], float] = time.process_time,
        window: int = 64,
        rate_name: str = "/s",
    ) -> None:
        self._total = total
        self._phase = phase
        self._unit = unit
        self._rate_name = rate_name
        self._sink = sink
        self._log = log
        self._interval = float(interval)
        self._stall_after = float(stall_after)
        self._clock = clock
        self._cpu_clock = cpu_clock
        self._started = clock()
        self._cpu_started = cpu_clock()
        self._samples: Deque[tuple[float, int]] = deque(maxlen=max(2, window))
        self._samples.append((self._started, 0))
        self._completed = 0
        self._retries = 0
        self._skipped = 0
        self._failures = 0
        self._bytes = 0
        self._detail = ""
        self._sequence = 0
        self._last_emit = self._started
        self._last_advance = self._started
        self._closed = False

    # -- state ------------------------------------------------------------

    @property
    def completed(self) -> int:
        return self._completed

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def sequence(self) -> int:
        return self._sequence

    def set_phase(self, phase: str, *, total: Optional[int] = None,
                  unit: Optional[str] = None) -> None:
        """Enter a named sub-phase, forcing a heartbeat at the transition.

        Sub-phases matter more than they look: "corpus build, 4 hours elapsed" is
        uninterpretable, while "corpus build / download" is actionable, because
        discover, plan, download, canonical and verify have wildly different
        expected durations.

        The outgoing phase gets a closing heartbeat only if it actually did
        something. Emitting one for a phase that completed no work — which is
        every phase transition at the start of a job — is two identical lines
        saying nothing, and noise at a transition is exactly where an operator
        stops reading.
        """
        if self._completed:
            self.emit(force=True)
        self._phase = phase
        if unit is not None:
            self._unit = unit
        if total is not None:
            self._total = total
            self._completed = 0
            self._samples.clear()
            self._samples.append((self._clock(), 0))
        self._detail = ""
        self.emit(force=True)

    def advance(
        self,
        n: int = 1,
        *,
        detail: str = "",
        retries: int = 0,
        nbytes: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> None:
        """Record completed work, then emit if the interval has elapsed."""
        self._completed += int(n)
        self._retries += int(retries)
        self._skipped += int(skipped)
        self._failures += int(failed)
        self._bytes += int(nbytes)
        if detail:
            self._detail = detail
        now = self._clock()
        if n:
            self._last_advance = now
        self._samples.append((now, self._completed))
        self.emit()

    def note(self, message: str, **fields: Any) -> None:
        """Record an operational event (a retry, a resume, a skip) without
        advancing the work counter. Goes to the log; reaches the human only when
        a sink is attached, because a per-event console stream at this scale is
        the noise the heartbeat exists to replace."""
        if self._log is not None:
            record = {"kind": "event", "phase": self._phase,
                      "message": message}
            record.update(fields)
            self._log.append(record)

    # -- derived ----------------------------------------------------------

    def _rates(self, now: float) -> tuple[Optional[float], Optional[float]]:
        elapsed = now - self._started
        cumulative = (round(self._completed / elapsed, 4)
                      if elapsed > 0 and self._completed else None)
        trailing = cumulative
        if len(self._samples) >= 2:
            first_at, first_done = self._samples[0]
            span = now - first_at
            delta = self._completed - first_done
            if span > 0 and delta > 0:
                trailing = round(delta / span, 4)
        return trailing, cumulative

    def _eta(self, trailing: Optional[float]) -> Optional[float]:
        """From trailing throughput, never cumulative — an ETA that cannot react
        to a slowdown is worse than none, because it is believed."""
        if not self._total or not trailing:
            return None
        remaining = self._total - self._completed
        return round(remaining / trailing, 1) if remaining > 0 else 0.0

    @property
    def stalled(self) -> bool:
        if self._total is not None and self._completed >= self._total:
            return False
        return (self._clock() - self._last_advance) > self._stall_after

    def snapshot(self) -> Heartbeat:
        now = self._clock()
        trailing, cumulative = self._rates(now)
        elapsed = now - self._started
        return Heartbeat(
            sequence=self._sequence,
            at=_utc_now(),
            phase=self._phase,
            unit=self._unit,
            completed=self._completed,
            total=self._total,
            elapsed_seconds=elapsed,
            rate_trailing=trailing,
            rate_cumulative=cumulative,
            eta_seconds=self._eta(trailing),
            stalled=self.stalled,
            detail=self._detail,
            retries=self._retries,
            skipped=self._skipped,
            failures=self._failures,
            bytes_done=self._bytes,
            rss_bytes=rss_bytes(),
            cpu_fraction=cpu_fraction(
                self._cpu_clock() - self._cpu_started, elapsed),
            rate_name=self._rate_name,
        )

    # -- emission ---------------------------------------------------------

    def emit(self, *, force: bool = False) -> Optional[Heartbeat]:
        now = self._clock()
        if not force and (now - self._last_emit) < self._interval:
            return None
        self._last_emit = now
        self._sequence += 1
        beat = self.snapshot()
        if self._sink is not None:
            self._sink(beat.render())
        if self._log is not None:
            self._log.append(beat.to_dict())
        return beat

    def close(self, *, detail: str = "") -> Optional[Heartbeat]:
        """Final heartbeat. Idempotent, so it is safe in a `finally`."""
        if self._closed:
            return None
        self._closed = True
        if detail:
            self._detail = detail
        return self.emit(force=True)


# ---------------------------------------------------------------------------
# Job recording and failure forensics
# ---------------------------------------------------------------------------

@dataclass
class JobRecorder:
    """Brackets a long job with a start record, a failure record, or a finish.

    The failure record is the point of the class. A hundred-hour job that dies at
    hour 37 must leave behind enough to diagnose it without re-running: the
    configuration it was given, how far it got, when it stopped, and the
    traceback. Without the traceback an operator learns *that* something failed,
    not *what* — which is the difference between a ten-minute fix and a day of
    bisection.
    """

    log: OperationalLog
    job: str
    config: dict[str, Any] = field(default_factory=dict)
    progress: Optional[Progress] = None
    _started: float = field(default=0.0, init=False)

    def __enter__(self) -> "JobRecorder":
        self._started = time.monotonic()
        self.log.append({
            "kind": "job_start", "job": self.job,
            "config": self.config, "pid": os.getpid(),
            "python": sys.version.split()[0], "platform": sys.platform,
        })
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = round(time.monotonic() - self._started, 3)
        beat = None
        if self.progress is not None:
            # Close the progress *before* the job record so the log reads in the
            # order things happened: final heartbeat, then finish-or-failure.
            # `close` is idempotent, so a caller keeping its own `finally` is
            # harmless.
            self.progress.close()
            beat = self.progress.snapshot()
        if exc_type is None:
            self.log.append({"kind": "job_finish", "job": self.job,
                             "elapsed_seconds": elapsed,
                             "progress": beat.to_dict() if beat else None})
            return False
        self.log.append({
            "kind": "job_failure",
            "job": self.job,
            "elapsed_seconds": elapsed,
            "error_type": exc_type.__name__,
            "error": str(exc),
            "traceback": "".join(
                traceback.format_exception(exc_type, exc, tb)),
            "config": self.config,
            "progress": beat.to_dict() if beat else None,
        })
        return False        # never swallow: a failed job must fail loudly


def preserve_failed_run(run_dir: Path | str) -> Optional[Path]:
    """Move an existing run bundle aside instead of letting it be overwritten.

    `ReplayDriver` refuses to write into a directory that already holds output,
    and `overwrite=True` deletes it — which is correct for a *finished* run being
    deliberately replaced, and destructive for a *crashed* one. The partial
    sinks, the per-cycle health file and the abort record are the only evidence
    of the failure that exists, and they are deleted at precisely the moment
    someone restarts to try again.

    Renaming is not a policy decision about the caller's data: nothing is
    removed, and the archived bundle keeps a timestamp so repeated failures do
    not collide. Returns the archive path, or `None` when there was nothing to
    preserve.
    """
    source = Path(run_dir)
    if not source.exists() or not any(source.iterdir()):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = source.with_name(f"{source.name}.failed-{stamp}")
    suffix = 1
    while archive.exists():                                 # pragma: no cover
        archive = source.with_name(f"{source.name}.failed-{stamp}-{suffix}")
        suffix += 1
    source.rename(archive)
    return archive
