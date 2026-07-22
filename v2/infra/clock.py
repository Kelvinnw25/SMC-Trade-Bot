"""infra.clock — the injectable time source for V2.

Architecture ref: 02-software-architecture.md §3 (`infra.clock`: "provide
wall-clock time and epoch conversions … exists so time is injectable and
testable"), §6 (the clock is owned by `infra.clock` and injected everywhere
time is needed), and Principle 10 (Injectable time and I/O). No module in the
core reads the wall clock directly; consumers accept a `Clock` so lifecycle
outcomes (TTL, decay, cooldown) are deterministic under test — the capability
V1 structurally lacked when it called `time.time()` / `datetime.utcnow()`
inside its logic.

This module knows nothing domain-related (§3 "Must NOT know"): it depends only
on the standard library and hands back plain `datetime` / `float` values.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FakeClock"]


@runtime_checkable
class Clock(Protocol):
    """The minimal time-source contract every consumer depends on.

    Two readings of the same instant:
      - ``now()``       — a timezone-aware UTC ``datetime``.
      - ``timestamp()`` — POSIX epoch seconds as a ``float`` (the value V1
        obtained from ``time.time()``, used for TTL/decay/cooldown metadata).

    Consumers accept a ``Clock`` and never read the wall clock themselves.
    ``SystemClock`` and ``FakeClock`` both satisfy this Protocol structurally;
    a future implementation (e.g. a replay clock for backtesting) only needs to
    provide these two methods, without any consumer changing.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...

    def timestamp(self) -> float:
        """Return the current instant as POSIX epoch seconds (UTC)."""
        ...


class SystemClock:
    """Production clock backed by the operating-system wall clock (UTC).

    Stateless. ``now()`` uses ``datetime.now(timezone.utc)`` — timezone-aware,
    not the deprecated naive ``utcnow()`` — and ``timestamp()`` is the POSIX
    epoch of that reading, equivalent to V1's ``time.time()``.
    """

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def timestamp(self) -> float:
        return self.now().timestamp()


class FakeClock:
    """Deterministic clock for tests. Not for production use.

    Seeded with a fixed timezone-aware moment; it does not move on its own.
    Call ``advance()`` to move it forward by a known amount — this is what makes
    time-dependent behavior reproducible in unit tests. The seed must be
    timezone-aware and is normalized to UTC, so ``now()`` always returns a UTC
    datetime, matching ``SystemClock``.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._current: datetime = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def timestamp(self) -> float:
        return self._current.timestamp()

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds`` (must be >= 0)."""
        if seconds < 0:
            raise ValueError(
                "advance() only moves time forward; seconds must be >= 0"
            )
        self._current = self._current + timedelta(seconds=seconds)
