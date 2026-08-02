"""Host-scoped request pacing — the one place a rate limit is enforced.

Separate from the downloader because a rate limit is a property of the *server*,
not of our execution strategy. Binance publishes two very different regimes and
conflating them would either throttle the fast path pointlessly or overrun the
slow one:

- **`data.binance.vision`** is a static object store with no documented request
  limit. It carries ~99% of an acquisition. Not paced here.
- **`fapi.binance.com`** allows **2,400 request-weight per minute per IP**. A
  `klines` call at `limit=1500` costs weight 10, so the ceiling is **4 requests
  per second** — and that is a ceiling, not a target.

## Why worker count is the wrong control for fapi

At the measured ~214 ms round-trip, even *two* workers issue ~9.3 req/s, which is
2.3x over the limit. Bounding concurrency cannot bound a rate when the rate
depends on latency. The bucket bounds the rate directly, so the guarantee holds at
any worker count — including one, where Stage 1 already collected six 418s.

`DEFAULT_FAPI_RATE` is 3/s rather than 4/s: 1,800 weight/min against a 2,400
budget leaves **25% headroom**, which is what absorbs the shared-egress bursts
that produced those 418s.

## Determinism

Pacing changes *when* a request is issued, never *what* is fetched or how it is
classified. Every artifact, checksum, manifest and identity is unaffected. The
clock and the sleeper are injected so tests observe the schedule instead of
waiting on it.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Mapping, Optional

__all__ = [
    "DEFAULT_FAPI_RATE",
    "FAPI_HOST",
    "HostRateLimiter",
    "TokenBucket",
    "default_rate_limiter",
    "host_of",
]

#: The REST fallback host. The bulk archive host is deliberately absent.
FAPI_HOST = "fapi.binance.com"

#: Requests per second against `FAPI_HOST`. See the module docstring for the
#: arithmetic behind 3 rather than 4.
DEFAULT_FAPI_RATE = 3.0


def host_of(url: str) -> str:
    """The hostname of `url`, lowercased, without port.

    Parsed by hand rather than with `urllib.parse` on purpose: the acquisition
    package keeps every network-library import inside `transport.py`, and a test
    asserts it (`TestBoundaries.test_only_the_transport_touches_the_network`).
    Splitting a string is not network access, and this keeps that boundary honest.
    """
    remainder = url.split("://", 1)[-1]
    authority = remainder.split("/", 1)[0]
    authority = authority.rsplit("@", 1)[-1]        # strip any userinfo
    if authority.startswith("["):                   # IPv6 literal
        return authority[1:authority.find("]")].lower() if "]" in authority \
            else authority.lower()
    return authority.split(":", 1)[0].lower()


class TokenBucket:
    """A thread-safe token bucket: at most `rate_per_second` acquisitions/second.

    Shared across workers, so the guarantee is about the *process*, not about any
    one thread. `capacity` is the burst allowance and defaults to one second's
    worth — enough to absorb jitter, too small to spend a minute's budget at once.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError(f"rate_per_second must be > 0, got {rate_per_second}")
        self._rate = float(rate_per_second)
        self._interval = 1.0 / self._rate
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._next_free = monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Reserve the next slot, waiting if it is in the future. Seconds waited.

        Strict spacing — every grant is at least `1/rate` after the previous one,
        with no burst allowance. A burst is the one thing a *headroom* margin is
        meant to avoid spending, and 3/s against a 4/s ceiling is already the
        margin.

        Two properties matter more than the arithmetic:

        - **The slot is reserved under the lock; the sleep happens outside it.**
          Holding the lock across a sleep would serialize every worker behind the
          first one to run dry, turning a rate limit into a concurrency limit.
        - **It cannot spin.** The wait is computed from a reservation rather than
          re-read from the clock, so a caller whose sleeper does nothing — every
          test in this suite — computes the same schedule instead of busy-looping
          until real time catches up.
        """
        with self._lock:
            now = self._monotonic()
            start = self._next_free if self._next_free > now else now
            self._next_free = start + self._interval
            wait = start - now
        if wait > 0:
            self._sleeper(wait)
            return wait
        return 0.0


class HostRateLimiter:
    """Applies a `TokenBucket` per host; hosts without one are not paced."""

    def __init__(self, buckets: Optional[Mapping[str, TokenBucket]] = None) -> None:
        self._buckets = dict(buckets or {})

    def acquire(self, url: str) -> float:
        """Pace `url` by its host. Returns seconds waited (0.0 when unpaced)."""
        bucket = self._buckets.get(host_of(url))
        return bucket.acquire() if bucket is not None else 0.0

    def bucket_for(self, host: str) -> Optional[TokenBucket]:
        return self._buckets.get(host)

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(sorted(self._buckets))


def default_rate_limiter(
    *,
    fapi_rate: float = DEFAULT_FAPI_RATE,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> HostRateLimiter:
    """The production limiter: fapi paced, the bulk archive unpaced."""
    return HostRateLimiter({
        FAPI_HOST: TokenBucket(
            fapi_rate, monotonic=monotonic, sleeper=sleeper),
    })
