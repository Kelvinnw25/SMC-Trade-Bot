"""HTTP transport — the acquisition layer's single boundary to the network.

One method, one Protocol, one production implementation. It exists because every
test in this project is hermetic: no test may touch the network, so the thing that
does must be injectable.

The transport knows nothing about Binance, klines, symbols, or archives. It
fetches bytes from a URL and distinguishes exactly one condition from failure:
**absence** (HTTP 404), because "this daily archive has not been published yet" is
an ordinary, expected answer during acquisition planning, not an error.

## Connection reuse

`RequestsTransport` holds one `requests.Session` **per thread** and reuses it for
every request that thread makes. Before this, each `get()` went through the
module-level `requests.get()`, which builds a throwaway `Session` and therefore a
fresh TCP connection and TLS handshake per call. Measured against the Binance
archive host: **394.0 ms per request without pooling, 214.3 ms with it — 1.84×**,
across ~79,000 requests in one acquisition.

Nothing observable changes. The same headers are merged in the same order (they are
still passed per-request, not set on the session, so the merge path is untouched),
the same bytes come back, the same statuses are classified the same way, and the
transport still performs no retrying. Connection reuse lives below HTTP semantics.

The session is thread-local rather than shared because `requests.Session` is not
documented thread-safe. A single-threaded caller sees exactly one session; the
structure is what lets a future concurrent caller reuse this class unchanged.
"""
from __future__ import annotations

import threading
from typing import Optional, Protocol, runtime_checkable

import requests

__all__ = [
    "HttpTransport",
    "RequestsTransport",
    "TransportBanned",
    "TransportError",
    "TransportNotFound",
    "TransportRateLimited",
]


#: Binance error code for an instrument the exchange does not know. Returned
#: with HTTP 400, not 404, so it needs naming explicitly to be classified as
#: absence rather than as a retryable fault.
_BINANCE_UNKNOWN_SYMBOL = '"code":-1121'

#: Binance answers a broken rate limit with 429, and an IP it has auto-banned for
#: *continuing* after a 429 with 418. Both carry `Retry-After`.
_RATE_LIMITED_STATUS = 429
_BANNED_STATUS = 418


class TransportError(Exception):
    """A request could not be completed: connection failure, timeout, or a
    non-404 error status. Retryable at the caller's discretion."""


class TransportNotFound(TransportError):
    """The resource does not exist (HTTP 404).

    Distinct from `TransportError` because absence is a *routine* answer when
    probing for an archive that has not been published: the planner falls through
    to the next source rather than treating it as a failure.
    """


class TransportRateLimited(TransportError):
    """The server refused because a rate limit was broken (HTTP 429).

    A subclass of `TransportError` so every existing `except TransportError` site
    keeps working unchanged; the downloader handles it earlier only to substitute
    the server's own `Retry-After` for its exponential backoff.

    `retry_after` is the header in seconds, or `None` when the server sent none or
    sent a form we do not parse — in which case the caller keeps its own backoff.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransportBanned(TransportError):
    """The server has banned this IP for continuing after a 429 (HTTP 418).

    Binance documents the escalation explicitly: ignore a 429 and the next answer
    is a 418, and repeat offences lengthen the ban **from 2 minutes to 3 days**.
    The old code classified this as a generic `TransportError` and retried it at
    1 s, 2 s and 4 s — which is precisely the behaviour that lengthens the ban.

    `retry_after` is the ban's remaining duration in seconds when the server
    stated it, else `None`. The downloader treats an unstated or long ban as
    terminal rather than guessing.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """`Retry-After` as seconds, or `None` when it cannot be read as such.

    Only the delta-seconds form is parsed. RFC 9110 also allows an HTTP-date, but
    Binance sends delta-seconds, and reading a date would require a clock this
    module deliberately does not have — the transport performs no timing, it only
    reports what the server said. An unparseable value returns `None`, which makes
    the caller fall back to its own backoff rather than to a fabricated number.

    Negative values are rejected: a wait cannot be negative, and treating one as
    zero would busy-loop against a server that is already refusing.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (AttributeError, ValueError):
        return None
    if seconds < 0 or seconds != seconds:            # negative, or NaN
        return None
    return seconds


@runtime_checkable
class HttpTransport(Protocol):
    """Fetch the bytes at a URL.

    Invariants an implementation must honour:

    - Returns the complete response body, or raises. Never a partial body.
    - Raises `TransportNotFound` for HTTP 404 and `TransportError` for every
      other failure. No other exception type escapes.
    - Performs no retrying: retry policy belongs to the downloader, which owns
      ordering and backoff.
    """

    def get(self, url: str) -> bytes:
        """The response body, or raise."""
        ...


class RequestsTransport:
    """Production transport over `requests`, with a pooled connection per thread.

    `timeout` is per-request. Retries are deliberately absent (see the Protocol).
    """

    def __init__(self, *, timeout: float = 60.0, user_agent: str = "Mozilla/5.0") -> None:
        self._timeout = timeout
        self._headers = {"User-Agent": user_agent}
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        """This thread's session, created on first use.

        Exposed so a test can assert reuse without reaching into a private
        attribute; callers have no reason to touch it.
        """
        session: Optional[requests.Session] = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def close(self) -> None:
        """Release this thread's pooled connections.

        Not on the `HttpTransport` Protocol: closing is an implementation detail of
        holding a connection open, and putting it on the Protocol would force every
        test double to implement a method that means nothing to it.

        It exists because this class now owns a resource it did not own before.
        `requests.get()` opened and closed a session per call; a pooled session
        lives until something closes it. A CLI process that exits reclaims the
        sockets either way, so this is hygiene rather than a leak fix — but a
        resource with no way to release it is a defect regardless.
        """
        session: Optional[requests.Session] = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None

    def get(self, url: str) -> bytes:
        try:
            # Headers stay per-request rather than moving onto the session: that
            # keeps `Session.prepare_request`'s merge order identical to what
            # `requests.get(url, headers=...)` produced, so the wire request is
            # unchanged and only the connection underneath it is reused.
            response = self.session.get(
                url, headers=self._headers, timeout=self._timeout)
        except Exception as exc:                      # connection, DNS, timeout
            raise TransportError(f"GET {url} failed: {exc}") from exc

        if response.status_code == 404:
            raise TransportNotFound(f"GET {url} -> 404")
        if response.status_code == 400 and _BINANCE_UNKNOWN_SYMBOL in response.text:
            # Binance's REST kline endpoint answers a delisted or never-listed
            # symbol with `400 {"code":-1121,"msg":"Invalid symbol."}` rather
            # than 404. Semantically that is absence — the exchange is saying
            # the instrument does not exist — so it belongs with 404 and must
            # not be retried.
            #
            # Treating it as a generic error cost the first Stage 1 run four
            # attempts each on 26 delisted-symbol periods and, worse, reported
            # them as acquisition failures when nothing was obtainable.
            raise TransportNotFound(
                f"GET {url} -> HTTP 400 unknown symbol: {response.text[:120]!r}")
        if response.status_code in (_RATE_LIMITED_STATUS, _BANNED_STATUS):
            # Classified here, but not acted on here: the transport still performs
            # no waiting and no retrying (see the Protocol). It reports what the
            # server said; the downloader owns what to do about it.
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            stated = "" if retry_after is None else f" retry_after={retry_after:g}s"
            failure = (TransportRateLimited
                       if response.status_code == _RATE_LIMITED_STATUS
                       else TransportBanned)
            raise failure(
                f"GET {url} -> HTTP {response.status_code}{stated}: "
                f"{response.text[:200]!r}",
                retry_after=retry_after,
            )
        if response.status_code != 200:
            raise TransportError(
                f"GET {url} -> HTTP {response.status_code}: "
                f"{response.text[:200]!r}"
            )
        return response.content
