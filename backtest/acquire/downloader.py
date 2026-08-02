"""The resumable downloader — execute a plan, one period at a time.

Owns exactly three things the planner and the store do not: **the order work is
attempted in**, **the retry policy**, and **turning a fetched payload into a
committed artifact**. It decides nothing about *what* to fetch (the planner) and
nothing about *how bytes are stored* (the store).

## Per item

Candidates are tried in the planner's preference order. For each:

- a published `.CHECKSUM` is fetched first when the source has one, so the bytes
  are verified against Binance's own digest rather than only against themselves;
- ZIP payloads are structurally verified (readable archive, every member's CRC,
  the expected CSV member present) *before* being committed;
- REST payloads must be a JSON array. An **empty array is a legitimate answer**,
  committed as evidence: "we asked, and the exchange had nothing for this period".
  That is what lets a delisted symbol's post-delisting days be covered rather than
  retried forever.

A 404 is not a failure — it means "this source has not published that period", and
the next candidate is tried. Only exhausting every candidate is a failure.

## Retries

Transport errors and integrity failures are retried with exponential backoff
(`backoff_base * 2**attempt`), because both can be transient. `TransportNotFound`
is never retried (absence is not a transient condition) and `DuplicateArtifact` is
never retried (immutable storage refusing an overwrite is a hard stop). Sleeping
goes through an injected callable so tests are instant and observe the schedule.

## Failures are reported, never swallowed

`run()` does not abort a 400-symbol acquisition because one day of one symbol
failed. Every failure is recorded in the returned `DownloadReport` with its reason;
`report.ok` is False whenever any item failed, and `raise_for_failures()` turns
that into an exception for a caller that wants one. Nothing is ever dropped
silently.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from backtest.acquire.discovery import DiscoveryResult
from backtest.acquire.planner import Candidate, DownloadPlan, PlanItem
from backtest.acquire.ratelimit import HostRateLimiter, default_rate_limiter
from backtest.acquire.storage import (
    AcquisitionError,
    ArtifactRecord,
    DuplicateArtifact,
    IntegrityError,
    RAW_SCHEMA_VERSION,
    RawStore,
    StorageTransient,
    parse_binance_checksum,
    sha256_hex,
    verify_archive,
)
from backtest.acquire.transport import (
    HttpTransport,
    TransportBanned,
    TransportError,
    TransportNotFound,
    TransportRateLimited,
)
from v2.infra.clock import Clock, SystemClock

__all__ = [
    "DOWNLOADER_VERSION",
    "DownloadReport",
    "Downloader",
    "ItemOutcome",
]

#: Stamped into every artifact record. Bump when acquisition behaviour changes in
#: a way that could make two artifacts of the same period differ.
DOWNLOADER_VERSION = "m4.1-1.0.0"

STATUS_DOWNLOADED = "downloaded"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"

#: The archive was never published for this period — the symbol was not listed
#: yet, was already delisted, or the period predates Binance's coverage.
#:
#: This is an **observation about the market**, not a fault. It was previously
#: reported as `failed`, because `acquire()` had one terminal status for "no
#: candidate succeeded" regardless of why. On the first real Stage 1 run that
#: produced 13,052 `failed` items out of 45,201 — every one of them a month in
#: which the symbol did not trade — and `strict` aborted a healthy acquisition.
#:
#: The distinction is not cosmetic: `strict` exists to refuse a corpus that is
#: silently short of data it should have. A period with no data to have cannot
#: make a corpus short, and treating it as if it could makes the guard fire on
#: exactly the survivorship-free inventory it was built to support.
STATUS_ABSENT = "absent"

#: How long a stated HTTP 418 ban may be before the run stops instead of waiting.
#:
#: Binance escalates a repeat ban from 2 minutes to **3 days**. A short ban is
#: worth pausing through; a long one means the run is already in trouble, and
#: sleeping on it would hold the process for hours while producing nothing. Above
#: this, `TransportBanned` propagates and terminates the run with the duration in
#: its message, so an operator sees the ban rather than a hung job.
BAN_PAUSE_LIMIT_SECONDS = 300.0

#: The highest worker count IMPLEMENTATION_PLAN sanctions for the bulk archive
#: host. `data.binance.vision` publishes no request limit, so the ceiling is
#: empirical rather than documented: at the measured ~214 ms round-trip, 6 workers
#: is ~28 req/s, and community reports put SSL/EOF failures above that band.
#: 4 -> 6 buys 42 minutes on Stage 2; 6 -> 8 buys 21 more against a risk with no
#: published boundary. Enforced by the CLI, not by this class, which stays a
#: library.
RECOMMENDED_MAX_WORKERS = 6


@dataclass(frozen=True)
class ItemOutcome:
    """What happened to one planned period."""

    symbol: str
    timeframe: str
    period: str
    status: str                       # downloaded | empty | absent | failed
    kind: str = ""                    # the candidate that satisfied it
    relative_path: str = ""
    attempts: int = 0                 # fetch operations performed
    detail: str = ""                  # the reason, when absent or failed
    retries: int = 0                  # re-attempts after a transient error

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DownloadReport:
    """The complete, ordered result of a run. Nothing is omitted."""

    outcomes: tuple[ItemOutcome, ...] = ()
    already_covered: int = 0
    started_at: str = ""
    finished_at: str = ""

    @property
    def downloaded(self) -> int:
        return sum(1 for o in self.outcomes if o.status == STATUS_DOWNLOADED)

    @property
    def empty(self) -> int:
        return sum(1 for o in self.outcomes if o.status == STATUS_EMPTY)

    @property
    def absent(self) -> int:
        """Periods with no published archive. Evidence, never failure."""
        return sum(1 for o in self.outcomes if o.status == STATUS_ABSENT)

    @property
    def retries(self) -> int:
        """Re-attempts after a transient error, across every item.

        Distinct from the sum of `attempts`, which counts *operations* — a bulk
        candidate performs two (its checksum and its payload), so a flawless run
        reports two attempts and zero retries per item. Conflating them made a
        perfect network look like 31,738 retries on the first Stage 1 run, which
        would have masked genuine rate limiting precisely when it mattered.
        """
        return sum(o.retries for o in self.outcomes)

    @property
    def failures(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == STATUS_FAILED)

    @property
    def ok(self) -> bool:
        return not self.failures

    def raise_for_failures(self) -> None:
        """Turn a report with failures into an exception, for a caller that
        wants the run to be all-or-nothing."""
        if self.ok:
            return
        head = ", ".join(
            f"{o.symbol}/{o.timeframe}/{o.period}: {o.detail}"
            for o in self.failures[:5]
        )
        more = "" if len(self.failures) <= 5 else f" (+{len(self.failures) - 5} more)"
        raise AcquisitionError(f"{len(self.failures)} item(s) failed: {head}{more}")

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "already_covered": self.already_covered,
            "downloaded": self.downloaded,
            "empty": self.empty,
            "absent": self.absent,
            "retries": self.retries,
            "failed": len(self.failures),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }

    def summary(self) -> dict:
        """The breakdown without the per-item detail.

        `to_dict` carries every outcome, which is ~45,000 entries at Stage 1
        scale — correct for an artifact, useless on a terminal. This is what the
        CLI prints, so an operator can separate "acquired", "legitimately
        absent", "already committed" and "failed" without opening a log.
        """
        return {
            "planned": len(self.outcomes),
            "downloaded": self.downloaded,
            "absent": self.absent,
            "empty": self.empty,
            "already_covered": self.already_covered,
            "retries": self.retries,
            "failed": len(self.failures),
            "ok": self.ok,
        }


class Downloader:
    """Executes a `DownloadPlan` against a `RawStore`."""

    def __init__(
        self,
        transport: HttpTransport,
        store: RawStore,
        *,
        clock: Optional[Clock] = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        backoff_base: float = 1.0,
        version: str = DOWNLOADER_VERSION,
        workers: int = 1,
        rate_limiter: Optional[HostRateLimiter] = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        self._transport = transport
        self._store = store
        self._clock = clock if clock is not None else SystemClock()
        self._sleeper = sleeper
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._version = version
        #: `workers == 1` takes the sequential path below — not a pool of one — so
        #: `--workers 1` is a true rollback to pre-Step-3b behaviour with no code
        #: revert and no executor in the picture at all.
        self._workers = workers
        #: **Unpaced by default.** Pacing is an operational policy about a
        #: specific server, not a property of executing a plan, so the library
        #: stays inert and `build_corpus` — the production entry point — installs
        #: `default_rate_limiter()`. Defaulting to a paced limiter here would make
        #: every test that plans real fapi URLs sleep against a wall clock, and
        #: would silently change `--workers 1` from "the sequential path exactly"
        #: into "the sequential path, throttled".
        self._rate_limiter = (rate_limiter if rate_limiter is not None
                              else HostRateLimiter())

    # -- running -----------------------------------------------------------

    def run(
        self,
        plan: DownloadPlan,
        *,
        progress: Optional[Callable[[ItemOutcome, int, int], None]] = None,
    ) -> DownloadReport:
        """Acquire every item of `plan`, in plan order.

        Deterministic ordering: items are attempted exactly in the order the
        planner emitted them, so two runs over the same plan touch the same URLs
        in the same sequence — **and `outcomes` is ordered by plan index rather
        than by completion**, so the report says the same thing regardless of the
        order results arrive in.

        `progress` is called after each item with `(outcome, index, total)`. It is
        observation only — it cannot influence what is fetched, in what order, or
        whether the run continues — so a callback that raises would turn a
        reporting fault into an acquisition failure. It is therefore the caller's
        responsibility to supply one that does not, and this loop does not guard
        it: silently swallowing an exception here would hide a genuine bug in the
        one place the operator is looking for information.
        """
        started = self._now_iso()
        total = len(plan.items)
        # Assigned by plan index, never appended. Appending records *completion*
        # order, which is identical to plan order only while items are attempted
        # one at a time — the report would silently reorder the moment they are
        # not, and two runs over one plan would disagree on a file that is meant
        # to be evidence. Indexing makes plan order a property of the structure
        # rather than a consequence of the schedule.
        results: list[Optional[ItemOutcome]] = [None] * total
        if self._workers == 1:
            for index, item in enumerate(plan.items, start=1):
                outcome = self.acquire(item)
                results[index - 1] = outcome
                if progress is not None:
                    progress(outcome, index, total)
        else:
            self._run_parallel(plan, results, progress)
        missing = [i for i, outcome in enumerate(results) if outcome is None]
        if missing:
            # Unreachable while every item is attempted, and asserted rather than
            # trusted: a short report reads as a smaller plan, not as a bug, and
            # `verify_coverage` would not catch it because the gap is in the
            # report rather than in the store.
            raise AcquisitionError(
                f"{len(missing)} planned item(s) produced no outcome "
                f"(first at index {missing[0]}); the report would be incomplete")
        return DownloadReport(
            outcomes=tuple(results),
            already_covered=plan.already_covered,
            started_at=started,
            finished_at=self._now_iso(),
        )

    def _run_parallel(
        self,
        plan: DownloadPlan,
        results: "list[Optional[ItemOutcome]]",
        progress: Optional[Callable[[ItemOutcome, int, int], None]],
    ) -> None:
        """Acquire `plan` across a bounded pool, writing into `results` by index.

        ## What makes this safe

        Items are independent by construction: an artifact's path encodes
        `(symbol, timeframe, period, kind)`, so two items cannot target one file.
        The commit protocol is untouched — temp -> fsync -> `os.replace`, sidecar
        last — and `os.replace` is atomic on every supported platform. Re-committing
        identical bytes is a no-op; differing bytes raise `DuplicateArtifact`.

        ## What makes it deterministic

        Two things, and neither is incidental:

        1. **Results are stored at `futures[future]`**, the item's plan index, so
           the report is ordered by the plan and not by the schedule (Step 3a).
        2. **`progress` is called from this thread only.** `as_completed` yields
           here, so the callback is never entered concurrently and callers need no
           locking. Its index is a completion counter — "N of M done" — which at one
           worker is also the plan position.

        ## Every future is observed exactly once

        `observed` records each future whose outcome or exception this loop
        consumed. On the happy path that is all of them, asserted below. On an
        abort, the remainder are cancelled and any that had *already failed* have
        their exceptions attached to the raised error as notes — a worker
        exception is never discarded, which is the guarantee the module docstring
        makes and the one a pool is most likely to break.

        ## Two abort classes, one mechanism

        A worker exception surfaces from `future.result()` and is caught by the
        inner guard. An **operator interrupt** does not: `KeyboardInterrupt` is
        raised while the `for` statement is evaluating `as_completed`, which is
        *outside* that guard. Without the outer guard below it would escape to
        `ThreadPoolExecutor.__exit__`, whose `shutdown(wait=True)` queues its
        stop sentinel **behind** every pending work item and therefore executes
        the entire remaining plan before re-raising — measured at 900/900 items.
        Ctrl+C would appear to hang for the length of the acquisition.

        Both classes therefore cancel through `_cancel_pending`. Cancelling does
        not abort work already running: a worker mid-request, or parked in a
        `Retry-After` sleep, finishes naturally and commits normally. Abort
        latency is bounded by the longest **in-flight** item — worst case
        `BAN_PAUSE_LIMIT_SECONDS`, and nothing here shortens it, because cutting
        a commit short is the one thing that could leave a partial artifact.
        """
        total = len(plan.items)
        completed = 0
        observed: set[Future] = set()
        failure: Optional[BaseException] = None

        with ThreadPoolExecutor(max_workers=self._workers,
                                thread_name_prefix="acquire") as pool:
            futures: dict[Future, int] = {
                pool.submit(self.acquire, item): index
                for index, item in enumerate(plan.items)
            }
            try:
                for future in as_completed(futures):
                    try:
                        outcome = future.result()
                    except BaseException as exc:  # noqa: BLE001 - re-raised below
                        observed.add(future)
                        failure = exc
                        self._cancel_pending(futures)
                        break
                    observed.add(future)
                    results[futures[future]] = outcome
                    completed += 1
                    if progress is not None:
                        progress(outcome, completed, total)
            except BaseException:                 # noqa: BLE001 - re-raised as-is
                # Reached only by an exception the inner guard cannot see: an
                # operator interrupt, or a `progress` callback that raised. The
                # `break` above leaves the loop normally and does not come here,
                # so the worker-exception path is untouched.
                self._cancel_pending(futures)
                raise
        # The pool has shut down here, so every future is done or cancelled and
        # nothing below can block.
        if failure is not None:
            for note in self._unobserved_failures(futures, observed):
                failure.add_note(note)
            raise failure
        unobserved = [i for f, i in futures.items() if f not in observed]
        if unobserved:
            raise AcquisitionError(
                f"{len(unobserved)} future(s) were never observed "
                f"(first at plan index {min(unobserved)}); a dropped future "
                f"would silently shorten the report")

    @staticmethod
    def _cancel_pending(futures: "dict[Future, int]") -> int:
        """Cancel every future not yet started. Returns how many were cancelled.

        `Future.cancel()` is a no-op on an item already running or done, so this
        is safe to call twice and safe to call on an abort that raced a
        completion. Cancelled items stay in the executor's queue and are skipped
        by the worker in constant time, which is what turns `shutdown(wait=True)`
        from "execute the rest of the plan" into "join six threads".
        """
        return sum(1 for pending in futures if pending.cancel())

    @staticmethod
    def _unobserved_failures(
        futures: "dict[Future, int]", observed: "set[Future]"
    ) -> list[str]:
        """Exceptions from futures the abort path never consumed.

        Without this they would be swallowed by the executor and the run would
        report a single cause when several items had in fact failed.
        """
        notes: list[str] = []
        for future, index in futures.items():
            if future in observed or future.cancelled() or not future.done():
                continue
            error = future.exception()
            if error is not None:
                notes.append(
                    f"also failed (plan index {index}): "
                    f"{type(error).__name__}: {error}")
        return notes

    def acquire(self, item: PlanItem) -> ItemOutcome:
        """Acquire one period by trying its candidates in order.

        When no candidate succeeds, *why* decides the status. If every candidate
        reported `_SourceAbsent` the archive simply does not exist for this
        period, which is `STATUS_ABSENT` — an observation. If any candidate
        exhausted its retries on a transport or integrity error, the data should
        have been obtainable and was not, which is `STATUS_FAILED`.

        A mixed item is a failure: a daily period whose ZIP is unpublished (absent)
        but whose REST fallback then times out (fault) is *not* known to be
        absent, and reporting it as such would assert something unmeasured.
        """
        attempts = 0
        retries = 0
        reasons: list[str] = []
        faulted = False
        for candidate in item.candidates:
            try:
                outcome, used, retried = self._acquire_candidate(item, candidate)
            except _SourceAbsent as absent:
                reasons.append(str(absent))
                continue
            except _CandidateFailed as failed:
                attempts += failed.attempts
                retries += failed.retries
                reasons.append(str(failed))
                faulted = True
                continue
            attempts += used
            retries += retried
            return ItemOutcome(
                symbol=item.symbol, timeframe=item.timeframe, period=item.period,
                status=outcome, kind=candidate.kind,
                relative_path=candidate.relative_path, attempts=attempts,
                retries=retries,
            )
        if item.candidates and not faulted:
            return ItemOutcome(
                symbol=item.symbol, timeframe=item.timeframe, period=item.period,
                status=STATUS_ABSENT, attempts=attempts, retries=retries,
                detail="; ".join(reasons),
            )
        return ItemOutcome(
            symbol=item.symbol, timeframe=item.timeframe, period=item.period,
            status=STATUS_FAILED, attempts=attempts, retries=retries,
            detail="; ".join(reasons) or "no candidate sources",
        )

    # -- one candidate -----------------------------------------------------

    def _acquire_candidate(
        self, item: PlanItem, candidate: Candidate
    ) -> tuple[str, int, int]:
        """Fetch, verify and commit one candidate. Returns (status, attempts, retries).

        Raises `_SourceAbsent` when the source has no such period (404), and
        `_CandidateFailed` when every attempt was exhausted.
        """
        attempts = _Attempts()

        expected_sha: Optional[str] = None
        if candidate.checksum_url is not None:
            url = candidate.checksum_url

            def fetch_checksum() -> str:
                """Fetch *and parse* under one retry.

                The parse used to sit outside `_retry`. `parse_binance_checksum`
                raises `ChecksumMismatch`, which is an `IntegrityError` and which
                `_retry` therefore knows how to classify — but only if it is
                inside. Outside, a CHECKSUM URL answering HTTP 200 with a body
                that is not a 64-hex digest (a CDN error page, a truncated
                response, an S3 XML error served with 200) escaped `acquire()`
                and `run()` and terminated the entire acquisition with an
                unhandled exception.
                """
                payload = self._fetch(url)
                return parse_binance_checksum(
                    payload.decode("utf-8", errors="replace"))

            try:
                expected_sha = self._retry(
                    fetch_checksum, attempts, candidate.kind)
            except TransportNotFound:
                # No published checksum: the archive itself may still exist, but
                # unverifiable bulk bytes are not committed.
                raise _SourceAbsent(f"{candidate.kind}: no published checksum")

        def fetch_and_commit() -> str:
            payload = self._fetch(candidate.url)
            return self._commit(item, candidate, payload, expected_sha)

        try:
            status = self._retry(fetch_and_commit, attempts, candidate.kind)
        except TransportNotFound as exc:
            raise _SourceAbsent(f"{candidate.kind}: not published ({exc})")
        return status, attempts.count, attempts.retries

    def _commit(
        self,
        item: PlanItem,
        candidate: Candidate,
        payload: bytes,
        expected_sha: Optional[str],
    ) -> str:
        """Verify a payload for its kind, then commit it. Returns the status."""
        status = STATUS_DOWNLOADED
        if candidate.kind == "rest_json":
            rows = self._parse_rest(payload)
            if not rows:
                status = STATUS_EMPTY
        else:
            verify_archive(payload, expected_member=candidate.expected_member or "")

        record = ArtifactRecord(
            schema_version=RAW_SCHEMA_VERSION,
            downloader_version=self._version,
            downloaded_at=self._now_iso(),
            source_url=candidate.url,
            kind=candidate.kind,
            symbol=item.symbol,
            timeframe=item.timeframe,
            coverage_start_ms=item.coverage_start_ms,
            coverage_end_ms=item.coverage_end_ms,
            sha256=sha256_hex(payload),
            size_bytes=len(payload),
            checksum_source="binance" if expected_sha else "computed",
            artifact_path=candidate.relative_path,
        )
        self._store.commit(
            candidate.relative_path, payload,
            record=record, expected_sha256=expected_sha,
        )
        return status

    @staticmethod
    def _parse_rest(payload: bytes) -> list:
        """A REST kline response must be a JSON array. Its *contents* are not
        interpreted here — parsing klines is the canonical builder's job."""
        try:
            rows = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise _PayloadRejected(f"REST payload is not JSON: {exc}") from exc
        if isinstance(rows, dict) and "code" in rows:
            raise _PayloadRejected(f"REST error response: {rows}")
        if not isinstance(rows, list):
            raise _PayloadRejected(
                f"REST payload is {type(rows).__name__}, expected a JSON array"
            )
        return rows

    # -- discovery evidence ------------------------------------------------

    def commit_discovery(self, result: DiscoveryResult) -> tuple[str, ...]:
        """Store the payloads a discovery was derived from, as evidence.

        A discovery that cannot be re-derived from stored bytes is not
        reproducible, so the raw responses are committed like any other artifact.
        Their path carries the commit instant, because two discoveries days apart
        legitimately differ and neither supersedes the other.
        """
        stamp = self._now_iso().replace(":", "").replace("-", "")
        stored: list[str] = []
        payloads = [("exchange_info", result.exchange_info_bytes)]
        if result.archive_listing_bytes is not None:
            payloads.append(("archive_listing", result.archive_listing_bytes))
        for name, data in payloads:
            relative = f"discovery/{stamp}-{name}.json"
            record = ArtifactRecord(
                schema_version=RAW_SCHEMA_VERSION,
                downloader_version=self._version,
                downloaded_at=self._now_iso(),
                source_url=name,
                kind="discovery_json",
                symbol="",
                timeframe="",
                coverage_start_ms=0,
                coverage_end_ms=0,
                sha256=sha256_hex(data),
                size_bytes=len(data),
                checksum_source="computed",
                artifact_path=relative,
            )
            self._store.commit(relative, data, record=record)
            stored.append(relative)
        return tuple(stored)

    # -- internals ---------------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        """Pace, then fetch. The one place a request leaves this module.

        Pacing is host-scoped and lives in `ratelimit`: `fapi.binance.com` allows
        4 req/s and is bounded to 3, the bulk archive is unpaced. It changes *when*
        a request is issued, never what is fetched or how it is classified, so no
        artifact, checksum, manifest or identity can depend on it.

        The bucket is shared across workers, which is what makes the guarantee hold
        at any worker count — bounding concurrency cannot bound a rate that depends
        on latency.
        """
        self._rate_limiter.acquire(url)
        return self._transport.get(url)

    def _retry(self, call, attempts: "_Attempts", label: str):
        """The one retry implementation: exponential backoff, bounded attempts.

        Retries what can plausibly be transient — a transport error, a corrupted
        transfer, a payload that failed verification. Never retries
        `TransportNotFound` (absence is not transient) or `DuplicateArtifact`
        (immutable storage refusing an overwrite is a hard stop); both propagate
        to the caller, which decides what they mean.

        ## Rate limiting is the server's decision, not ours

        Two statuses are handled ahead of the generic branch because guessing a
        wait when the server has stated one is how a throttle becomes a ban:

        - **429** keeps its place in the attempt budget but waits `Retry-After`
          instead of `backoff_base * 2**attempt`. When the server states nothing
          parseable, the exponential schedule is used unchanged.
        - **418** is a ban, not a fault, and is never treated as one. A *stated*
          ban of at most `BAN_PAUSE_LIMIT_SECONDS` is paused through **once**; an
          unstated ban, a longer one, or a second ban propagates and stops the
          run. Retrying a 418 on a fixed backoff is exactly what Binance
          documents as lengthening the ban from 2 minutes toward 3 days, and it
          is what this code did before.

        A ban therefore never becomes an item `failed`: it is a condition of the
        whole run, and reporting it per item would bury it among 79,000 rows.

        ## Storage faults join the generic branch

        `StorageTransient` is retried on the same 1/2/4 schedule as a corrupted
        transfer: a scanner holding a file open clears in milliseconds, and before
        it was classified the resulting `PermissionError` escaped every handler and
        killed the run. It shares the branch rather than getting its own because
        the response is identical — wait, try again, and fail the item if it never
        clears. Note the retry re-fetches the payload, exactly as an integrity
        failure already does; correctness is worth one repeated request.
        """
        last: Optional[Exception] = None
        paused_for_ban = False
        for attempt in range(self._max_attempts):
            attempts.count += 1
            if attempt:
                attempts.retries += 1
            try:
                return call()
            except (TransportNotFound, DuplicateArtifact):
                raise
            except TransportBanned as exc:
                if (paused_for_ban
                        or exc.retry_after is None
                        or exc.retry_after > BAN_PAUSE_LIMIT_SECONDS
                        or attempt + 1 >= self._max_attempts):
                    raise
                paused_for_ban = True
                last = exc
                self._sleeper(exc.retry_after)
            except TransportRateLimited as exc:
                last = exc
                if attempt + 1 < self._max_attempts:
                    self._sleeper(
                        exc.retry_after if exc.retry_after is not None
                        else self._backoff_base * (2 ** attempt))
            except (TransportError, IntegrityError, _PayloadRejected,
                    StorageTransient) as exc:
                last = exc
                if attempt + 1 < self._max_attempts:
                    self._sleeper(self._backoff_base * (2 ** attempt))
        raise _CandidateFailed(f"{label}: {last}", attempts.count, attempts.retries)

    def _now_iso(self) -> str:
        return self._clock.now().astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )


# ---------------------------------------------------------------------------
# Internal control flow — never escapes this module
# ---------------------------------------------------------------------------

class _SourceAbsent(Exception):
    """This source has not published the period; try the next candidate."""


class _PayloadRejected(Exception):
    """The payload is not what this candidate kind must return."""


class _CandidateFailed(Exception):
    """Every attempt against this candidate was exhausted."""

    def __init__(self, message: str, attempts: int, retries: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retries = retries


@dataclass
class _Attempts:
    """A mutable attempt counter, shared across the fetches that make up one
    candidate (its checksum and its payload) so the outcome reports the true
    total rather than the last leg's.

    `count` is **operations**: a bulk candidate performs two even when nothing
    goes wrong. `retries` is only incremented when an operation is re-attempted
    after a transient error, so a flawless run reports `retries == 0` and a
    genuine rate-limit episode is visible instead of buried.
    """

    count: int = 0
    retries: int = 0
