"""The download planner — decide what to acquire, before acquiring anything.

The planner is a **pure function of (requested window, discovered symbols, local
storage)**. It performs no I/O beyond reading the store's committed state, holds
no state of its own, and given identical storage produces an identical plan —
same items, same order, same candidate URLs.

## What a plan is

A plan is an ordered list of `PlanItem`s. Each item is *one period that must be
covered* for one symbol and timeframe, together with an ordered list of
`Candidate` sources to try. The item is satisfied by whichever candidate commits
first; the period, not the source, is what the plan is about.

## Source policy (Task 3: "prefer Binance Bulk ZIP, REST only for uncovered ranges")

- A calendar month that ended at least `monthly_lag_days` before the window's end
  is acquired as **one monthly archive** — roughly a 30x reduction in requests,
  which is what makes ~400 symbols x 5 years x 3 timeframes feasible at all.
- Every remaining day is a **daily archive**, falling back to **REST** for the
  recent days whose archive Binance has not published yet.

The lag rule is expressed against the *requested window*, never against a clock
read: a planner that consulted `now()` would produce different plans for the same
request and could not be reasoned about.

## Resume

An item whose period is already covered by *any* committed candidate is omitted.
That is the whole of the resume mechanism: re-planning after an interruption
yields exactly the work that remains, and re-planning after a complete run yields
an empty plan.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from backtest.acquire.discovery import SymbolInfo
from backtest.acquire.storage import (
    AcquisitionError, RawStore, expected_member_for,
)

__all__ = [
    "BULK_BASE_URL",
    "Candidate",
    "DownloadPlan",
    "PENDING_TRADING",
    "PlanError",
    "PlanItem",
    "REST_KLINES_URL",
    "TIMEFRAMES",
    "plan_downloads",
]

BULK_BASE_URL = "https://data.binance.vision/data/futures/um"
REST_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

#: The timeframes the acquisition layer knows how to fetch. Declared here rather
#: than imported from `backtest.data`: the downloader must not depend on the
#: repository (they are separate tiers, and the dependency would be a cycle in
#: responsibility even where the import would work).
TIMEFRAMES: tuple[str, ...] = ("1d", "1h", "5m")

#: `exchangeInfo` status for an instrument Binance has announced but has not
#: begun trading. Passed through verbatim by `discovery` (which must not decide
#: what a non-trading status means); the *rule* about it belongs here, because
#: only the planner decides what work exists.
PENDING_TRADING = "PENDING_TRADING"

_DAY_MS = 86_400_000

#: REST returns at most 1500 klines per call. One UTC day is 288 five-minute
#: candles at most, so a day is always a single request and the planner never
#: needs to page.
_REST_LIMIT = 1500


class PlanError(AcquisitionError):
    """The requested plan is not well formed."""


@dataclass(frozen=True)
class Candidate:
    """One way to acquire a period: a URL, where its bytes would be stored, and
    the URL of the published checksum when the source has one."""

    kind: str                        # monthly_zip | daily_zip | rest_json
    url: str
    relative_path: str               # store-relative POSIX path
    checksum_url: Optional[str] = None
    expected_member: Optional[str] = None   # the CSV inside a ZIP


@dataclass(frozen=True)
class PlanItem:
    """One period of one (symbol, timeframe) that must be covered."""

    symbol: str
    timeframe: str
    period: str                      # "2025-03" or "2025-03-11"
    coverage_start_ms: int
    coverage_end_ms: int             # half-open
    candidates: tuple[Candidate, ...]

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.symbol, self.timeframe, self.coverage_start_ms)


@dataclass(frozen=True)
class DownloadPlan:
    """An ordered, deterministic list of work.

    `expected_paths` is every relative path that *would* satisfy the plan's
    items — the input to `RawStore.verify_coverage` once a run finishes.
    """

    items: tuple[PlanItem, ...]
    already_covered: int
    requested_start: str
    requested_end: str

    def __len__(self) -> int:
        return len(self.items)

    def expected_paths(self) -> tuple[str, ...]:
        return tuple(c.relative_path for item in self.items for c in item.candidates)


# ---------------------------------------------------------------------------

def plan_downloads(
    store: RawStore,
    symbols: Sequence[SymbolInfo],
    *,
    timeframes: Iterable[str] = TIMEFRAMES,
    start: date,
    end: date,
    monthly_lag_days: int = 5,
    archive_evidence: bool = False,
) -> DownloadPlan:
    """The work needed to cover `[start, end]` (inclusive UTC dates).

    Days before a symbol's `onboard_date_ms` are not planned: the exchange has no
    data there, and planning them would turn a five-year run into millions of
    guaranteed 404s. A symbol discovered only from the archive has no onboard date
    and is planned across the whole window.

    An instrument that has **never begun trading** is not planned at all (see
    `_never_traded`). `archive_evidence` declares that `symbols` were discovered
    *with* the bulk-archive listing, which is what makes `in_archive=False` a
    measurement rather than the absence of one. It defaults to False so that a
    caller who did not look at the archive cannot accidentally exclude a symbol
    on evidence it never gathered: planning impossible work wastes requests,
    while skipping possible work loses data, and only one of those is recoverable.
    """
    timeframes = tuple(timeframes)
    unknown = [tf for tf in timeframes if tf not in TIMEFRAMES]
    if unknown:
        raise PlanError(f"unknown timeframe(s) {unknown}; expected {TIMEFRAMES}")
    if end < start:
        raise PlanError(f"end date {end} precedes start date {start}")

    items: list[PlanItem] = []
    covered = 0
    for info in sorted(symbols, key=lambda s: s.symbol):
        if _never_traded(info, archive_evidence):
            continue
        symbol_start = _clamp_start(start, info)
        if symbol_start > end:
            continue
        for timeframe in timeframes:
            for item in _periods(info.symbol, timeframe, symbol_start, end,
                                 monthly_lag_days):
                if _is_covered(store, item):
                    covered += 1
                    continue
                items.append(item)

    items.sort(key=lambda i: i.key)
    return DownloadPlan(
        items=tuple(items),
        already_covered=covered,
        requested_start=start.isoformat(),
        requested_end=end.isoformat(),
    )


def _never_traded(info: SymbolInfo, archive_evidence: bool) -> bool:
    """True when two independent sources agree the instrument has no history.

    `_clamp_start` already refuses to plan days on which no data can exist, using
    `onboardDate` as the proxy for "trading started". For 850 of 851 symbols that
    proxy holds. It failed for GAIBUSDT, whose onboard date passed on
    2025-11-20 and which was still `PENDING_TRADING` eight months later: Binance
    announced the listing and never opened it. The planner produced 23 items for
    an instrument with no klines, and the 16 with a REST fallback aborted a
    healthy 13,078-item acquisition on `-1122` and `418`.

    The conjunction is the point. Either half alone is an inference:

    - `PENDING_TRADING` alone would exclude an instrument that traded, was
      delisted, and was re-announced — its archive history is real and wanted.
    - `not in_archive` alone would exclude every symbol whose archive directory
      has not appeared yet, including freshly listed ones that REST can serve.

    Together they are a measurement: the archive has published nothing for it,
    *and* the exchange states trading has not begun. `in_exchange_info` is
    required so the status is known to come from `exchangeInfo` rather than from
    a hand-built `SymbolInfo`; it narrows the rule and can never widen it.

    Deliberately *not* inferred from `-1122 Invalid symbol status`: that is a
    statement about an instrument's status now, not about whether history exists,
    and a symbol suspended mid-run would answer it for periods it has full data
    for. Absence is claimed here from published evidence or not at all.
    """
    return (
        archive_evidence
        and info.in_exchange_info
        and info.listing_state == PENDING_TRADING
        and not info.in_archive
    )


def _clamp_start(start: date, info: SymbolInfo) -> date:
    """The first date worth planning for a symbol."""
    if info.onboard_date_ms is None:
        return start
    onboard = datetime.fromtimestamp(
        info.onboard_date_ms / 1000.0, tz=timezone.utc
    ).date()
    return max(start, onboard)


def _is_covered(store: RawStore, item: PlanItem) -> bool:
    """The period is covered when *any* of its candidates is committed.

    Source-agnostic on purpose: a day fetched by REST before its archive existed
    stays covered once the archive appears, so re-planning never re-downloads a
    period that is already evidenced.
    """
    return any(store.is_committed(c.relative_path) for c in item.candidates)


def _periods(
    symbol: str, timeframe: str, start: date, end: date, monthly_lag_days: int
) -> list[PlanItem]:
    """Split `[start, end]` into monthly items followed by daily items."""
    items: list[PlanItem] = []
    cursor = start
    while cursor <= end:
        month_last = _last_day_of_month(cursor)
        whole_month = (
            cursor.day == 1
            and month_last <= end
            and month_last + timedelta(days=monthly_lag_days) <= end
        )
        if whole_month:
            items.append(_monthly_item(symbol, timeframe, cursor))
            cursor = month_last + timedelta(days=1)
        else:
            items.append(_daily_item(symbol, timeframe, cursor))
            cursor += timedelta(days=1)
    return items


def _monthly_item(symbol: str, timeframe: str, first_day: date) -> PlanItem:
    period = f"{first_day.year:04d}-{first_day.month:02d}"
    name = f"{symbol}-{timeframe}-{period}"
    relative = (
        f"bulk/um/monthly/klines/{symbol}/{timeframe}/{name}.zip"
    )
    url = f"{BULK_BASE_URL}/monthly/klines/{symbol}/{timeframe}/{name}.zip"
    last_day = _last_day_of_month(first_day)
    return PlanItem(
        symbol=symbol,
        timeframe=timeframe,
        period=period,
        coverage_start_ms=_epoch_ms(first_day),
        coverage_end_ms=_epoch_ms(last_day) + _DAY_MS,
        candidates=(
            Candidate(
                kind="monthly_zip",
                url=url,
                relative_path=relative,
                checksum_url=url + ".CHECKSUM",
                expected_member=expected_member_for(name + ".zip"),
            ),
        ),
    )


def _daily_item(symbol: str, timeframe: str, day: date) -> PlanItem:
    period = day.isoformat()
    name = f"{symbol}-{timeframe}-{period}"
    zip_relative = f"bulk/um/daily/klines/{symbol}/{timeframe}/{name}.zip"
    zip_url = f"{BULK_BASE_URL}/daily/klines/{symbol}/{timeframe}/{name}.zip"
    start_ms = _epoch_ms(day)
    end_ms = start_ms + _DAY_MS
    rest_relative = f"rest/{symbol}/{timeframe}/{start_ms}-{end_ms}.json"
    rest_url = (
        f"{REST_KLINES_URL}?symbol={symbol}&interval={timeframe}"
        f"&startTime={start_ms}&endTime={end_ms - 1}&limit={_REST_LIMIT}"
    )
    return PlanItem(
        symbol=symbol,
        timeframe=timeframe,
        period=period,
        coverage_start_ms=start_ms,
        coverage_end_ms=end_ms,
        candidates=(
            Candidate(
                kind="daily_zip",
                url=zip_url,
                relative_path=zip_relative,
                checksum_url=zip_url + ".CHECKSUM",
                expected_member=expected_member_for(name + ".zip"),
            ),
            Candidate(
                kind="rest_json",
                url=rest_url,
                relative_path=rest_relative,
            ),
        ),
    )


def _last_day_of_month(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _epoch_ms(day: date) -> int:
    return int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000
    )
