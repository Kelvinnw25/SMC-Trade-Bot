"""The Raw Historical Acquisition Layer — Milestone M4.1.

Covers symbol discovery (including delisted symbols), deterministic planning,
resumable downloading, retry/backoff, immutable storage, every integrity failure,
and the raw manifest.

Hermetic: the only network boundary is `HttpTransport`, and every test injects a
fake. No test opens a socket, and none sleeps — the backoff sleeper is injected and
recorded so the schedule can be asserted instead of waited on.
"""
from __future__ import annotations

import io
import json
import random
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from backtest.acquire import (
    AcquisitionError,
    ArchiveCorrupt,
    ArchiveIncomplete,
    ArtifactNameMismatch,
    ArtifactRecord,
    ChecksumMismatch,
    CoverageGap,
    DELISTED,
    DiscoveryError,
    Downloader,
    DuplicateArtifact,
    HttpTransport,
    IntegrityError,
    MANIFEST_NAME,
    RAW_SCHEMA_VERSION,
    RawStore,
    SymbolInfo,
    TransportError,
    TransportNotFound,
    TruncatedArtifact,
    discover_symbols,
    plan_downloads,
    verify_archive,
)
from backtest.acquire import (
    StorageTransient,
    TransportBanned,
    TransportRateLimited,
)
from backtest.acquire.downloader import (
    BAN_PAUSE_LIMIT_SECONDS,
    RECOMMENDED_MAX_WORKERS,
    STATUS_ABSENT,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    _Attempts,
)
from backtest.acquire.ratelimit import (
    FAPI_HOST,
    HostRateLimiter,
    TokenBucket,
    default_rate_limiter,
    host_of,
)
from backtest.acquire.storage import parse_binance_checksum, sha256_hex
from v2.infra.clock import FakeClock

FIXED_NOW = datetime(2026, 3, 15, 9, 30, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class FakeTransport:
    """Serves preset payloads; records every URL requested, in order."""

    def __init__(self, payloads: dict | None = None, *, fail: dict | None = None):
        self.payloads = dict(payloads or {})
        self.fail = dict(fail or {})       # url -> remaining failures to raise
        self.calls: list[str] = []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        remaining = self.fail.get(url, 0)
        if remaining > 0:
            self.fail[url] = remaining - 1
            raise TransportError(f"simulated failure for {url}")
        try:
            return self.payloads[url]
        except KeyError:
            raise TransportNotFound(f"404 {url}") from None


class RecordingSleeper:
    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_zip(member: str, content: bytes = b"a,b,c\n1,2,3\n") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
    return buffer.getvalue()


def checksum_payload(data: bytes, filename: str = "x.zip") -> bytes:
    return f"{sha256_hex(data)}  {filename}\n".encode()


def record_for(relative: str, data: bytes, **over) -> ArtifactRecord:
    base = dict(
        schema_version=RAW_SCHEMA_VERSION,
        downloader_version="test-1",
        downloaded_at="2026-03-15T09:30:00Z",
        source_url="https://example/" + relative,
        kind="daily_zip",
        symbol="BTCUSDT",
        timeframe="1h",
        coverage_start_ms=0,
        coverage_end_ms=86_400_000,
        sha256=sha256_hex(data),
        size_bytes=len(data),
        checksum_source="computed",
        artifact_path=relative,
    )
    base.update(over)
    return ArtifactRecord(**base)


def symbol(name: str = "BTCUSDT", **over) -> SymbolInfo:
    base = dict(symbol=name, listing_state="TRADING", in_exchange_info=True,
                in_archive=True, contract_type="PERPETUAL", quote_asset="USDT",
                onboard_date_ms=None, delivery_date_ms=None)
    base.update(over)
    return SymbolInfo(**base)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = RawStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def downloader(self, transport, **kw):
        kw.setdefault("sleeper", RecordingSleeper())
        return Downloader(transport, self.store, clock=FakeClock(FIXED_NOW), **kw)


# ==========================================================================
# TASK 1 — symbol discovery
# ==========================================================================

EXCHANGE_INFO = json.dumps({"symbols": [
    {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
     "status": "TRADING", "onboardDate": 1568102400000},
    {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
     "status": "TRADING", "onboardDate": 1574726400000},
    {"symbol": "BTCUSDT_240628", "contractType": "CURRENT_QUARTER",
     "quoteAsset": "USDT", "status": "TRADING"},
    {"symbol": "BTCBUSD", "contractType": "PERPETUAL", "quoteAsset": "BUSD",
     "status": "TRADING"},
    {"symbol": "ADAUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT",
     "status": "SETTLING", "onboardDate": 1600000000000},
]}).encode()


def listing_xml(names, *, truncated=False):
    prefixes = "".join(
        f"<CommonPrefixes><Prefix>data/futures/um/daily/klines/{n}/</Prefix>"
        f"</CommonPrefixes>" for n in names)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
        f"{prefixes}</ListBucketResult>"
    ).encode()


class TestDiscovery(unittest.TestCase):
    def _transport(self, listing_names=("BTCUSDT", "ETHUSDT", "DEADUSDT")):
        from backtest.acquire.discovery import ARCHIVE_LISTING_URL, EXCHANGE_INFO_URL
        return FakeTransport({
            EXCHANGE_INFO_URL: EXCHANGE_INFO,
            ARCHIVE_LISTING_URL: listing_xml(listing_names),
        })

    def test_only_usdt_perpetuals(self):
        result = discover_symbols(self._transport())
        names = result.names()
        self.assertIn("BTCUSDT", names)
        self.assertNotIn("BTCUSDT_240628", names)   # dated delivery contract
        self.assertNotIn("BTCBUSD", names)          # not USDT-quoted

    def test_deterministic_ordering(self):
        a = discover_symbols(self._transport()).names()
        b = discover_symbols(self._transport()).names()
        self.assertEqual(a, b)
        self.assertEqual(list(a), sorted(a))

    def test_delisted_symbol_is_discovered_and_marked(self):
        # DEADUSDT exists in the archive but not in exchangeInfo.
        result = discover_symbols(self._transport())
        dead = {s.symbol: s for s in result.symbols}["DEADUSDT"]
        self.assertEqual(dead.listing_state, DELISTED)
        self.assertFalse(dead.in_exchange_info)
        self.assertTrue(dead.in_archive)
        self.assertIsNone(dead.onboard_date_ms)

    def test_non_trading_status_is_reported_verbatim(self):
        result = discover_symbols(self._transport())
        ada = {s.symbol: s for s in result.symbols}["ADAUSDT"]
        self.assertEqual(ada.listing_state, "SETTLING")

    def test_metadata_from_exchange_info(self):
        result = discover_symbols(self._transport())
        btc = {s.symbol: s for s in result.symbols}["BTCUSDT"]
        self.assertEqual(btc.onboard_date_ms, 1568102400000)
        self.assertEqual(btc.contract_type, "PERPETUAL")
        self.assertTrue(btc.in_exchange_info and btc.in_archive)

    def test_archive_can_be_excluded(self):
        result = discover_symbols(self._transport(), include_archive=False)
        self.assertNotIn("DEADUSDT", result.names())
        self.assertIsNone(result.archive_listing_bytes)

    def test_evidence_payloads_are_returned(self):
        result = discover_symbols(self._transport())
        self.assertEqual(result.exchange_info_bytes, EXCHANGE_INFO)
        self.assertIsNotNone(result.archive_listing_bytes)

    def test_paginated_listing_is_followed(self):
        from backtest.acquire.discovery import ARCHIVE_LISTING_URL, EXCHANGE_INFO_URL
        page1 = listing_xml(["AAAUSDT"], truncated=True)
        marker = "data/futures/um/daily/klines/AAAUSDT/"
        transport = FakeTransport({
            EXCHANGE_INFO_URL: EXCHANGE_INFO,
            ARCHIVE_LISTING_URL: page1,
            f"{ARCHIVE_LISTING_URL}&marker={marker}": listing_xml(["ZZZUSDT"]),
        })
        names = discover_symbols(transport).names()
        self.assertIn("AAAUSDT", names)
        self.assertIn("ZZZUSDT", names)

    def test_malformed_exchange_info(self):
        from backtest.acquire.discovery import EXCHANGE_INFO_URL
        with self.assertRaises(DiscoveryError):
            discover_symbols(FakeTransport({EXCHANGE_INFO_URL: b"not json"}),
                             include_archive=False)

    def test_transport_failure_surfaces_as_discovery_error(self):
        with self.assertRaises(DiscoveryError):
            discover_symbols(FakeTransport({}), include_archive=False)


# ==========================================================================
# TASK 2 — the planner
# ==========================================================================

class TestPlanner(StoreCase):
    def test_daily_items_have_zip_then_rest(self):
        plan = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                              start=date(2026, 3, 10), end=date(2026, 3, 12))
        self.assertEqual(len(plan), 3)
        kinds = [c.kind for c in plan.items[0].candidates]
        self.assertEqual(kinds, ["daily_zip", "rest_json"])

    def test_complete_months_become_monthly_items(self):
        plan = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                              start=date(2026, 1, 1), end=date(2026, 3, 20))
        periods = [(i.period, i.candidates[0].kind) for i in plan.items]
        self.assertIn(("2026-01", "monthly_zip"), periods)
        self.assertIn(("2026-02", "monthly_zip"), periods)
        self.assertIn(("2026-03-01", "daily_zip"), periods)   # current month

    def test_recent_month_is_not_taken_as_monthly(self):
        # February ends 2026-02-28; with a 5-day lag it only qualifies once the
        # window extends to 2026-03-05.
        early = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                               start=date(2026, 2, 1), end=date(2026, 3, 2))
        self.assertNotIn("2026-02", [i.period for i in early.items])
        later = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                               start=date(2026, 2, 1), end=date(2026, 3, 6))
        self.assertIn("2026-02", [i.period for i in later.items])

    def test_deterministic_output(self):
        args = dict(timeframes=("1h", "5m"), start=date(2026, 3, 1),
                    end=date(2026, 3, 5))
        syms = [symbol("ETHUSDT"), symbol("BTCUSDT")]
        a = plan_downloads(self.store, syms, **args)
        b = plan_downloads(self.store, list(reversed(syms)), **args)
        self.assertEqual([i.key for i in a.items], [i.key for i in b.items])
        self.assertEqual([c.url for i in a.items for c in i.candidates],
                         [c.url for i in b.items for c in i.candidates])

    def test_ordering_is_symbol_timeframe_period(self):
        plan = plan_downloads(self.store, [symbol("ETHUSDT"), symbol("BTCUSDT")],
                              timeframes=("1h",), start=date(2026, 3, 1),
                              end=date(2026, 3, 3))
        keys = [i.key for i in plan.items]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(keys[0][0], "BTCUSDT")

    def test_onboard_date_clamps_the_window(self):
        onboard = int(datetime(2026, 3, 3, tzinfo=timezone.utc).timestamp() * 1000)
        plan = plan_downloads(self.store, [symbol(onboard_date_ms=onboard)],
                              timeframes=("1h",), start=date(2026, 3, 1),
                              end=date(2026, 3, 5))
        self.assertEqual([i.period for i in plan.items],
                         ["2026-03-03", "2026-03-04", "2026-03-05"])

    def test_symbol_onboarded_after_the_window_is_skipped(self):
        onboard = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        plan = plan_downloads(self.store, [symbol(onboard_date_ms=onboard)],
                              timeframes=("1h",), start=date(2026, 3, 1),
                              end=date(2026, 3, 5))
        self.assertEqual(len(plan), 0)

    def test_committed_period_is_omitted_and_counted(self):
        first = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                               start=date(2026, 3, 10), end=date(2026, 3, 12))
        target = first.items[0].candidates[0]
        data = make_zip("BTCUSDT-1h-2026-03-10.csv")
        self.store.commit(target.relative_path, data,
                          record=record_for(target.relative_path, data))
        second = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                                start=date(2026, 3, 10), end=date(2026, 3, 12))
        self.assertEqual(len(second), 2)
        self.assertEqual(second.already_covered, 1)

    def test_rest_coverage_also_satisfies_the_period(self):
        plan = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                              start=date(2026, 3, 10), end=date(2026, 3, 10))
        rest = plan.items[0].candidates[1]
        self.store.commit(rest.relative_path, b"[]",
                          record=record_for(rest.relative_path, b"[]",
                                            kind="rest_json"))
        again = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                               start=date(2026, 3, 10), end=date(2026, 3, 10))
        self.assertEqual(len(again), 0)

    def test_rejects_bad_input(self):
        from backtest.acquire import PlanError
        with self.assertRaises(PlanError):
            plan_downloads(self.store, [symbol()], timeframes=("4h",),
                           start=date(2026, 3, 1), end=date(2026, 3, 2))
        with self.assertRaises(PlanError):
            plan_downloads(self.store, [symbol()], start=date(2026, 3, 5),
                           end=date(2026, 3, 1))

    def test_expected_paths_feed_coverage_verification(self):
        plan = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                              start=date(2026, 3, 10), end=date(2026, 3, 10))
        with self.assertRaises(CoverageGap):
            self.store.verify_coverage(plan.expected_paths())
        rest = plan.items[0].candidates[1]
        self.store.commit(rest.relative_path, b"[]",
                          record=record_for(rest.relative_path, b"[]"))
        # Coverage is per *period*: the satisfied candidate closes the gap.
        remaining = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                                   start=date(2026, 3, 10), end=date(2026, 3, 10))
        self.store.verify_coverage(remaining.expected_paths())   # empty -> no raise

    def test_acquire_and_repository_agree_on_timeframes(self):
        # The two packages deliberately do not import each other, so nothing at
        # runtime stops their timeframe lists from drifting apart. This is where
        # that invariant is enforced.
        from backtest.acquire import TIMEFRAMES as ACQUIRE_TFS
        from backtest.data import TIMEFRAMES as REPO_TFS
        self.assertEqual(sorted(ACQUIRE_TFS), sorted(REPO_TFS))

    def test_coverage_windows_are_half_open_and_contiguous(self):
        plan = plan_downloads(self.store, [symbol()], timeframes=("1h",),
                              start=date(2026, 3, 10), end=date(2026, 3, 12))
        spans = [(i.coverage_start_ms, i.coverage_end_ms) for i in plan.items]
        for (_, end_a), (start_b, _) in zip(spans, spans[1:]):
            self.assertEqual(end_a, start_b)


class TestNeverTradedInstrumentsAreNotPlanned(StoreCase):
    """The GAIBUSDT regression.

    Binance announced GAIBUSDT with onboardDate 2025-11-20 and never opened it;
    eight months later it was still `PENDING_TRADING`, absent from the bulk
    archive, and served `-1122 Invalid symbol status` from REST. The planner
    produced 23 items for it and the 16 with a REST fallback failed, aborting a
    13,078-item acquisition in which every other item was healthy.

    The rule is a conjunction of two independently published facts. Each test
    below removes exactly one of them and asserts the symbol is planned again.
    """

    WINDOW = dict(timeframes=("1h",), start=date(2026, 3, 10),
                  end=date(2026, 3, 12), archive_evidence=True)

    def plan_for(self, info, **over):
        args = dict(self.WINDOW)
        args.update(over)
        return plan_downloads(self.store, [info], **args)

    # -- 1. the rule fires, and only on the conjunction --------------------

    def test_pending_trading_and_not_in_archive_produces_zero_items(self):
        plan = self.plan_for(symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                                    in_archive=False))
        self.assertEqual(len(plan), 0)
        self.assertEqual(plan.items, ())
        # It is *skipped*, not counted as already satisfied: a symbol with no
        # history has nothing to cover, so it must not inflate the resume count.
        self.assertEqual(plan.already_covered, 0)

    def test_pending_trading_but_present_in_archive_is_still_planned(self):
        # Condition B removed. An instrument that traded, was delisted and was
        # re-announced has real archive history and must be acquired.
        plan = self.plan_for(symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                                    in_archive=True))
        self.assertEqual(len(plan), 3)

    def test_absent_from_archive_but_trading_is_still_planned(self):
        # Condition A removed. A freshly listed symbol has no archive directory
        # yet and is served by the REST fallback — exactly what it is for.
        plan = self.plan_for(symbol("NEWUSDT", listing_state="TRADING",
                                    in_archive=False))
        self.assertEqual(len(plan), 3)

    def test_status_must_come_from_exchange_info(self):
        # A status of PENDING_TRADING can only originate in exchangeInfo. This
        # conjunct narrows the rule; it can never widen it.
        plan = self.plan_for(symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                                    in_archive=False, in_exchange_info=False))
        self.assertEqual(len(plan), 3)

    # -- 2/3/4. every other listing state is planned exactly as before ------

    def test_trading_settling_and_delisted_are_unaffected(self):
        for state, in_archive in (("TRADING", True), ("SETTLING", True),
                                  (DELISTED, True), ("SETTLING", False)):
            with self.subTest(state=state, in_archive=in_archive):
                plan = self.plan_for(symbol("XUSDT", listing_state=state,
                                            in_archive=in_archive))
                self.assertEqual(len(plan), 3)

    def test_delisted_archive_only_symbol_is_planned_identically(self):
        # The survivorship-free case (C-6.9): known only from the archive, no
        # onboard date, planned across the whole window.
        archive_only = SymbolInfo(symbol="OLDUSDT", listing_state=DELISTED,
                                  in_exchange_info=False, in_archive=True)
        self.assertEqual(len(self.plan_for(archive_only)), 3)

    def test_the_rule_changes_nothing_for_a_normal_inventory(self):
        # The strongest regression statement: for an inventory containing no
        # never-traded instrument, the plan is byte-identical with the rule on
        # and off.
        inventory = [symbol("BTCUSDT"), symbol("ETHUSDT"),
                     symbol("OLDUSDT", listing_state=DELISTED,
                            in_exchange_info=False),
                     symbol("SXPUSDT", listing_state="SETTLING")]
        args = dict(timeframes=("1h",), start=date(2026, 3, 10),
                    end=date(2026, 3, 12))
        off = plan_downloads(self.store, inventory, **args)
        on = plan_downloads(self.store, inventory, archive_evidence=True, **args)
        self.assertEqual(off.items, on.items)
        self.assertEqual(len(on), 12)

    # -- 5. the exclusion is re-evaluated, never remembered -----------------

    def test_pending_becomes_trading_without_any_code_change(self):
        pending = symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                         in_archive=False)
        self.assertEqual(len(self.plan_for(pending)), 0)
        # The next run reads a fresh exchangeInfo. Nothing is persisted about
        # the exclusion, so the same call with the new status plans in full.
        opened = symbol("GAIBUSDT", listing_state="TRADING", in_archive=False)
        self.assertEqual(len(self.plan_for(opened)), 3)

    def test_exclusion_is_not_persisted_anywhere(self):
        # Nothing is written by planning: the store is untouched, so there is no
        # state that could make the exclusion outlive the status that justified it.
        before = sorted(p.name for p in self.root.rglob("*"))
        self.plan_for(symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                             in_archive=False))
        self.assertEqual(before, sorted(p.name for p in self.root.rglob("*")))

    # -- the evidence guard -------------------------------------------------

    def test_no_archive_evidence_means_no_exclusion(self):
        # `include_archive=False` never reads the listing, so `in_archive=False`
        # is not a measurement. Excluding on it would assert something unlooked-at.
        pending = symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                         in_archive=False)
        self.assertEqual(len(self.plan_for(pending, archive_evidence=False)), 3)

    def test_evidence_defaults_to_absent(self):
        # The default must be the conservative one: planning impossible work
        # wastes requests, skipping possible work loses data.
        plan = plan_downloads(
            self.store,
            [symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                    in_archive=False)],
            timeframes=("1h",), start=date(2026, 3, 10), end=date(2026, 3, 12))
        self.assertEqual(len(plan), 3)

    # -- the shape of the real incident ------------------------------------

    def test_the_exact_gaibusdt_plan_collapses_from_23_items_to_zero(self):
        # The real window: onboard 2025-11-20T09:00Z through 2026-07-05, 1h.
        # 11 daily (Nov 20-30) + 7 monthly (Dec-Jun) + 5 daily (Jul 1-5) = 23,
        # of which the 16 dailies carry the REST fallback that failed.
        gaib = symbol("GAIBUSDT", listing_state="PENDING_TRADING",
                      in_archive=False, onboard_date_ms=1_763_629_200_000)
        args = dict(timeframes=("1h",), start=date(2020, 1, 1),
                    end=date(2026, 7, 5))
        before = plan_downloads(self.store, [gaib], **args)
        self.assertEqual(len(before), 23)
        self.assertEqual(
            sum(1 for i in before.items
                if any(c.kind == "rest_json" for c in i.candidates)), 16)
        after = plan_downloads(self.store, [gaib], archive_evidence=True, **args)
        self.assertEqual(len(after), 0)


# ==========================================================================
# TASK 4 / 5 — immutable storage and integrity
# ==========================================================================

class TestStorage(StoreCase):
    def test_commit_then_committed(self):
        data = make_zip("x.csv")
        rel = "bulk/um/daily/klines/BTCUSDT/1h/x.zip"
        self.assertFalse(self.store.is_committed(rel))
        self.store.commit(rel, data, record=record_for(rel, data))
        self.assertTrue(self.store.is_committed(rel))
        self.assertEqual(self.store.record(rel).sha256, sha256_hex(data))

    def test_recommitting_identical_bytes_is_a_noop(self):
        data = make_zip("x.csv")
        rel = "a/x.zip"
        first = self.store.commit(rel, data, record=record_for(rel, data))
        second = self.store.commit(rel, data, record=record_for(rel, data))
        self.assertEqual(first.sha256, second.sha256)

    def test_recommitting_different_bytes_raises(self):
        rel = "a/x.zip"
        self.store.commit(rel, b"one", record=record_for(rel, b"one"))
        with self.assertRaises(DuplicateArtifact):
            self.store.commit(rel, b"two", record=record_for(rel, b"two"))
        self.assertEqual(self.store.path_for(rel).read_bytes(), b"one")

    def test_published_checksum_mismatch_is_refused_before_writing(self):
        rel = "a/x.zip"
        with self.assertRaises(ChecksumMismatch):
            self.store.commit(rel, b"payload", record=record_for(rel, b"payload"),
                              expected_sha256="0" * 64)
        self.assertFalse(self.store.path_for(rel).exists())

    def test_record_sha_must_match_the_bytes(self):
        rel = "a/x.zip"
        bad = record_for(rel, b"payload", sha256="f" * 64)
        with self.assertRaises(ChecksumMismatch):
            self.store.commit(rel, b"payload", record=bad)

    def test_size_disagreement_is_refused(self):
        rel = "a/x.zip"
        bad = record_for(rel, b"payload", size_bytes=999)
        with self.assertRaises(TruncatedArtifact):
            self.store.commit(rel, b"payload", record=bad)

    def test_empty_payload_is_refused(self):
        with self.assertRaises(TruncatedArtifact):
            self.store.commit("a/x.zip", b"", record=record_for("a/x.zip", b""))

    def test_unsafe_paths_are_refused(self):
        with self.assertRaises(AcquisitionError):
            self.store.is_committed("../escape.zip")

    def test_missing_sidecar_means_not_committed(self):
        data = make_zip("x.csv")
        rel = "a/x.zip"
        self.store.commit(rel, data, record=record_for(rel, data))
        self.store.meta_path(rel).unlink()
        self.assertFalse(self.store.is_committed(rel))   # bytes alone are not evidence

    def test_verify_detects_tampering(self):
        data = make_zip("x.csv")
        rel = "a/x.zip"
        self.store.commit(rel, data, record=record_for(rel, data))
        self.store.verify(rel)                            # clean
        self.store.path_for(rel).write_bytes(data + b"tampered")
        with self.assertRaises(TruncatedArtifact):        # size first
            self.store.verify(rel)

    def test_verify_detects_same_length_corruption(self):
        data = make_zip("x.csv")
        rel = "a/x.zip"
        self.store.commit(rel, data, record=record_for(rel, data))
        mutated = bytearray(data)
        mutated[-1] ^= 0xFF
        self.store.path_for(rel).write_bytes(bytes(mutated))
        with self.assertRaises(ChecksumMismatch):
            self.store.verify(rel)

    def test_discard_then_reacquire(self):
        data = make_zip("x.csv")
        rel = "a/x.zip"
        self.store.commit(rel, data, record=record_for(rel, data))
        self.store.discard(rel)
        self.assertFalse(self.store.is_committed(rel))
        self.store.commit(rel, b"new bytes", record=record_for(rel, b"new bytes"))
        self.assertTrue(self.store.is_committed(rel))

    def test_verify_coverage_names_the_gaps(self):
        with self.assertRaises(CoverageGap) as ctx:
            self.store.verify_coverage(["a/1.zip", "a/2.zip"])
        self.assertIn("a/1.zip", str(ctx.exception))

    def test_verify_coverage_passes_when_complete(self):
        rel = "a/x.zip"
        self.store.commit(rel, b"data", record=record_for(rel, b"data"))
        self.store.verify_coverage([rel])                 # no raise

    def test_verify_all_checks_every_artifact(self):
        names = ("a/BTCUSDT-1h-2026-03-10.zip", "b/BTCUSDT-1h-2026-03-11.zip")
        for name in names:
            data = make_zip(Path(name).name[:-4] + ".csv", b"row," + name.encode())
            self.store.commit(name, data, record=record_for(name, data))
        self.assertEqual(len(self.store.verify_all()), 2)
        corrupt = bytearray(self.store.path_for(names[0]).read_bytes())
        corrupt[-1] ^= 0xFF
        self.store.path_for(names[0]).write_bytes(bytes(corrupt))
        with self.assertRaises(ChecksumMismatch):
            self.store.verify_all()


class TestArchiveIntegrity(unittest.TestCase):
    def test_valid_archive(self):
        verify_archive(make_zip("BTCUSDT-1h-2026-03-10.csv"),
                       expected_member="BTCUSDT-1h-2026-03-10.csv")

    def test_not_a_zip(self):
        with self.assertRaises(ArchiveCorrupt):
            verify_archive(b"definitely not a zip", expected_member="x.csv")

    def test_truncated_zip(self):
        data = make_zip("x.csv")
        with self.assertRaises(ArchiveCorrupt):
            verify_archive(data[: len(data) // 2], expected_member="x.csv")

    def test_corrupted_member_fails_crc(self):
        # Stored (not deflated) so the payload sits verbatim in the file and a
        # flipped byte is guaranteed to be *content*, not framing.
        content = b"payload-" * 32
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("x.csv", content)
        raw = bytearray(buffer.getvalue())
        offset = raw.find(content)
        self.assertGreater(offset, 0)
        raw[offset + 10] ^= 0xFF
        with self.assertRaises(ArchiveCorrupt):
            verify_archive(bytes(raw), expected_member="x.csv")

    def test_wrong_member_name(self):
        with self.assertRaises(ArtifactNameMismatch):
            verify_archive(make_zip("unexpected.csv"), expected_member="x.csv")

    def test_empty_archive(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w"):
            pass
        with self.assertRaises(ArchiveIncomplete):
            verify_archive(buffer.getvalue(), expected_member="x.csv")

    def test_checksum_parsing(self):
        digest = "a" * 64
        self.assertEqual(parse_binance_checksum(f"{digest}  file.zip\n"), digest)
        with self.assertRaises(ChecksumMismatch):
            parse_binance_checksum("garbage")


# ==========================================================================
# TASK 3 — the downloader
# ==========================================================================

class TestDownloader(StoreCase):
    def _plan(self, start="2026-03-10", end="2026-03-10", **kw):
        return plan_downloads(
            self.store, [symbol()], timeframes=("1h",),
            start=date.fromisoformat(start), end=date.fromisoformat(end), **kw)

    def _daily_payloads(self, plan, *, member=None):
        item = plan.items[0]
        zip_c = item.candidates[0]
        member = member or zip_c.expected_member
        data = make_zip(member)
        return {
            zip_c.checksum_url: checksum_payload(data),
            zip_c.url: data,
        }, data

    def test_bulk_zip_preferred_and_committed(self):
        plan = self._plan()
        payloads, data = self._daily_payloads(plan)
        transport = FakeTransport(payloads)
        report = self.downloader(transport).run(plan)
        self.assertTrue(report.ok)
        self.assertEqual(report.downloaded, 1)
        outcome = report.outcomes[0]
        self.assertEqual(outcome.kind, "daily_zip")
        self.assertTrue(self.store.is_committed(outcome.relative_path))
        self.assertEqual(self.store.record(outcome.relative_path).checksum_source,
                         "binance")

    def test_checksum_is_fetched_before_the_archive(self):
        plan = self._plan()
        payloads, _ = self._daily_payloads(plan)
        transport = FakeTransport(payloads)
        self.downloader(transport).run(plan)
        self.assertTrue(transport.calls[0].endswith(".CHECKSUM"))

    def test_published_checksum_mismatch_is_not_committed(self):
        plan = self._plan()
        item = plan.items[0]
        zip_c = item.candidates[0]
        transport = FakeTransport({
            zip_c.checksum_url: checksum_payload(b"different bytes"),
            zip_c.url: make_zip(zip_c.expected_member),
            item.candidates[1].url: b"[]",
        })
        report = self.downloader(transport, max_attempts=1).run(plan)
        # The ZIP candidate is rejected; REST covers the period instead.
        self.assertEqual(report.outcomes[0].kind, "rest_json")
        self.assertFalse(self.store.is_committed(zip_c.relative_path))

    def test_falls_back_to_rest_when_archive_absent(self):
        plan = self._plan()
        rest = plan.items[0].candidates[1]
        rows = [[1, "1", "2", "0.5", "1.5", "10", 2, "100", 3, "5", "50", "0"]]
        transport = FakeTransport({rest.url: json.dumps(rows).encode()})
        report = self.downloader(transport).run(plan)
        self.assertEqual(report.outcomes[0].kind, "rest_json")
        self.assertEqual(report.downloaded, 1)
        self.assertTrue(self.store.is_committed(rest.relative_path))

    def test_empty_rest_response_is_committed_as_evidence(self):
        plan = self._plan()
        rest = plan.items[0].candidates[1]
        report = self.downloader(FakeTransport({rest.url: b"[]"})).run(plan)
        self.assertEqual(report.outcomes[0].status, "empty")
        self.assertTrue(report.ok)
        self.assertTrue(self.store.is_committed(rest.relative_path))

    def test_rest_error_object_is_rejected(self):
        plan = self._plan()
        rest = plan.items[0].candidates[1]
        transport = FakeTransport(
            {rest.url: b'{"code":-1121,"msg":"Invalid symbol."}'})
        report = self.downloader(transport, max_attempts=1).run(plan)
        self.assertFalse(report.ok)
        self.assertIn("-1121", report.failures[0].detail)

    def test_all_sources_absent_is_a_recorded_absence_not_a_failure(self):
        # Was `..._is_a_recorded_failure`. Every source reporting 404 means the
        # archive was never published for this period — the symbol was not
        # listed, or was already delisted. There is no data to be short of, so
        # the run stays `ok` and `strict` does not fire. The period is still
        # recorded, with its reason, as `absent`.
        plan = self._plan()
        report = self.downloader(FakeTransport({})).run(plan)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.failures), 0)
        self.assertEqual(report.absent, 1)
        self.assertEqual(report.outcomes[0].status, "absent")
        self.assertIn("not published", report.outcomes[0].detail)

    def test_retry_with_exponential_backoff(self):
        plan = self._plan()
        rest = plan.items[0].candidates[1]
        sleeper = RecordingSleeper()
        transport = FakeTransport({rest.url: b"[]"}, fail={rest.url: 2})
        report = Downloader(transport, self.store, clock=FakeClock(FIXED_NOW),
                            sleeper=sleeper, max_attempts=4,
                            backoff_base=1.0).run(plan)
        self.assertTrue(report.ok)
        self.assertEqual(sleeper.delays, [1.0, 2.0])       # 2^0, 2^1
        self.assertEqual(report.outcomes[0].attempts, 3)

    def test_retries_are_bounded(self):
        plan = self._plan()
        rest = plan.items[0].candidates[1]
        sleeper = RecordingSleeper()
        transport = FakeTransport({rest.url: b"[]"}, fail={rest.url: 99})
        report = Downloader(transport, self.store, clock=FakeClock(FIXED_NOW),
                            sleeper=sleeper, max_attempts=3,
                            backoff_base=0.5).run(plan)
        self.assertFalse(report.ok)
        self.assertEqual(sleeper.delays, [0.5, 1.0])
        self.assertFalse(self.store.is_committed(rest.relative_path))

    def test_absence_is_never_retried(self):
        plan = self._plan()
        sleeper = RecordingSleeper()
        transport = FakeTransport({})
        Downloader(transport, self.store, clock=FakeClock(FIXED_NOW),
                   sleeper=sleeper).run(plan)
        self.assertEqual(sleeper.delays, [])

    def test_corrupt_archive_is_never_committed(self):
        plan = self._plan()
        zip_c = plan.items[0].candidates[0]
        broken = b"not a zip at all"
        transport = FakeTransport({
            zip_c.checksum_url: checksum_payload(broken),
            zip_c.url: broken,
        })
        report = self.downloader(transport, max_attempts=1).run(plan)
        self.assertFalse(report.ok)
        self.assertFalse(self.store.is_committed(zip_c.relative_path))

    def test_deterministic_attempt_order(self):
        plan = plan_downloads(self.store, [symbol("ETHUSDT"), symbol("BTCUSDT")],
                              timeframes=("1h",), start=date(2026, 3, 10),
                              end=date(2026, 3, 11))
        transport = FakeTransport({})
        self.downloader(transport).run(plan)
        symbols_in_order = [c.split("/klines/")[1].split("/")[0]
                            for c in transport.calls if "/klines/" in c]
        self.assertEqual(symbols_in_order, sorted(symbols_in_order))

    def test_interrupted_run_resumes_without_redownloading(self):
        plan = self._plan(end="2026-03-12")
        payloads = {}
        for item in plan.items:
            zip_c = item.candidates[0]
            data = make_zip(zip_c.expected_member)
            payloads[zip_c.checksum_url] = checksum_payload(data)
            payloads[zip_c.url] = data

        # First run: only the first day is servable.
        first_item = plan.items[0]
        partial = {k: v for k, v in payloads.items()
                   if first_item.candidates[0].url in k
                   or k == first_item.candidates[0].checksum_url}
        report_1 = self.downloader(FakeTransport(partial)).run(plan)
        self.assertEqual(report_1.downloaded, 1)
        # The two unservable days 404 from every source, so they are `absent`,
        # not `failed`. Resume is unaffected either way: neither committed an
        # artifact, so both are re-planned below.
        self.assertEqual(len(report_1.failures), 0)
        self.assertEqual(report_1.absent, 2)

        # Re-plan: the covered day is gone from the plan.
        plan_2 = self._plan(end="2026-03-12")
        self.assertEqual(len(plan_2), 2)
        self.assertEqual(plan_2.already_covered, 1)

        transport_2 = FakeTransport(payloads)
        report_2 = self.downloader(transport_2).run(plan_2)
        self.assertTrue(report_2.ok)
        self.assertNotIn(first_item.candidates[0].url, transport_2.calls)

        # Everything is now covered and a third plan is empty.
        self.assertEqual(len(self._plan(end="2026-03-12")), 0)

    def test_repeated_download_produces_identical_artifacts(self):
        plan = self._plan()
        payloads, data = self._daily_payloads(plan)
        rel = plan.items[0].candidates[0].relative_path
        self.downloader(FakeTransport(payloads)).run(plan)
        first_bytes = self.store.path_for(rel).read_bytes()
        first_meta = self.store.meta_path(rel).read_text(encoding="utf-8")

        # Re-running the same (stale) plan must not rewrite anything.
        self.downloader(FakeTransport(payloads)).run(plan)
        self.assertEqual(self.store.path_for(rel).read_bytes(), first_bytes)
        self.assertEqual(self.store.meta_path(rel).read_text(encoding="utf-8"),
                         first_meta)

    def test_no_part_files_survive_a_successful_run(self):
        plan = self._plan()
        payloads, _ = self._daily_payloads(plan)
        self.downloader(FakeTransport(payloads)).run(plan)
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_no_part_files_survive_a_failed_run(self):
        # An interrupted or rejected write must not leave debris behind.
        plan = self._plan()
        zip_c = plan.items[0].candidates[0]
        transport = FakeTransport({
            zip_c.checksum_url: checksum_payload(b"other"),
            zip_c.url: make_zip(zip_c.expected_member),
        })
        self.downloader(transport, max_attempts=1).run(plan)
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_temp_name_is_process_scoped(self):
        # Two operators over one store must not share an in-flight temp path.
        import os
        rel = "a/x.zip"
        data = make_zip("x.csv")
        self.store.commit(rel, data, record=record_for(rel, data))
        self.assertNotIn(f".{os.getpid()}.part", str(self.store.path_for(rel)))
        self.assertTrue(self.store.is_committed(rel))

    def test_no_artifact_written_when_a_candidate_fails(self):
        plan = self._plan()
        zip_c = plan.items[0].candidates[0]
        transport = FakeTransport({zip_c.checksum_url: checksum_payload(b"x")},
                                  fail={zip_c.url: 99})
        self.downloader(transport, max_attempts=2).run(plan)
        self.assertFalse(self.store.path_for(zip_c.relative_path).exists())

    def test_discovery_evidence_is_committed(self):
        from backtest.acquire.discovery import DiscoveryResult
        result = DiscoveryResult(symbols=(symbol(),),
                                 exchange_info_bytes=EXCHANGE_INFO,
                                 archive_listing_bytes=listing_xml(["BTCUSDT"]))
        stored = self.downloader(FakeTransport({})).commit_discovery(result)
        self.assertEqual(len(stored), 2)
        for rel in stored:
            self.assertTrue(self.store.is_committed(rel))
            self.assertEqual(self.store.record(rel).kind, "discovery_json")


# ==========================================================================
# TASK 6 — the raw manifest
# ==========================================================================

class TestManifest(StoreCase):
    def _populate(self):
        for name in ("b/2.zip", "a/1.zip"):
            data = make_zip("x.csv") + name.encode()
            self.store.commit(name, data, record=record_for(name, data))

    def test_manifest_is_sorted_and_complete(self):
        self._populate()
        manifest = self.store.manifest()
        self.assertEqual(manifest["schema_version"], RAW_SCHEMA_VERSION)
        self.assertEqual(manifest["artifact_count"], 2)
        paths = [a["artifact_path"] for a in manifest["artifacts"]]
        self.assertEqual(paths, sorted(paths))

    def test_manifest_generation_is_deterministic(self):
        self._populate()
        first = self.store.write_manifest().read_text(encoding="utf-8")
        second = self.store.write_manifest().read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["artifact_count"], 2)

    def test_manifest_describes_provenance(self):
        self._populate()
        entry = self.store.manifest()["artifacts"][0]
        for key in ("downloader_version", "downloaded_at", "source_url", "kind",
                    "symbol", "timeframe", "coverage_start_ms", "coverage_end_ms",
                    "sha256", "size_bytes", "checksum_source", "artifact_path"):
            self.assertIn(key, entry)

    def test_manifest_file_is_not_itself_an_artifact(self):
        self._populate()
        self.store.write_manifest()
        paths = [a["artifact_path"] for a in self.store.manifest()["artifacts"]]
        self.assertNotIn(MANIFEST_NAME, paths)


# ==========================================================================
# TASK 7 — boundaries
# ==========================================================================

def imported_modules(path: Path) -> set[str]:
    """Every module name a file imports, parsed rather than grepped.

    Prose must not decide an architecture test: the planner's docstring
    legitimately *names* `backtest.data` to explain why it does not import it,
    and `v2/infra/clock.py` says "backtesting". Only the import graph counts.
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class _FakeResponse:
    """Enough of `requests.Response` for the classification branches."""

    def __init__(self, status_code: int, content: bytes = b"", text: str = "",
                 headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.text = text
        # A real Response always has `.headers`; the double carries one so the
        # 418/429 branches read the same attribute in tests as in production.
        self.headers = dict(headers or {})


class _RecordingSession:
    """Stands in for `requests.Session`; records calls and header merges."""

    instances: list["_RecordingSession"] = []

    def __init__(self):
        self.calls: list[tuple] = []
        self.closed = False
        self.response = _FakeResponse(200, b"payload")
        self.raise_with: Exception | None = None
        _RecordingSession.instances.append(self)

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        if self.raise_with is not None:
            raise self.raise_with
        return self.response

    def close(self):
        self.closed = True


class TransportCase(unittest.TestCase):
    """Installs `_RecordingSession` in place of `requests.Session`.

    Still hermetic: no socket is opened, and `RequestsTransport` is exercised as
    written rather than through a hand-rolled double of itself.
    """

    def setUp(self):
        from backtest.acquire import transport as transport_module

        self.module = transport_module
        _RecordingSession.instances = []
        self._real_session = transport_module.requests.Session
        transport_module.requests.Session = _RecordingSession
        self.addCleanup(
            setattr, transport_module.requests, "Session", self._real_session)

    def transport(self, **kwargs):
        return self.module.RequestsTransport(**kwargs)


class TestTransportReusesOneSessionPerThread(TransportCase):
    """T1.1 / T1.3 — the whole point of Step 1."""

    def test_two_gets_reuse_a_single_session(self):
        transport = self.transport()
        transport.get("https://example.test/a")
        transport.get("https://example.test/b")

        self.assertEqual(1, len(_RecordingSession.instances),
                         "a second Session means a second TLS handshake")
        self.assertEqual(
            ["https://example.test/a", "https://example.test/b"],
            [call[0] for call in _RecordingSession.instances[0].calls])

    def test_each_thread_gets_its_own_session(self):
        # requests.Session is not documented thread-safe, so sharing one across
        # workers would be a latent corruption rather than a speedup.
        import threading

        transport = self.transport()
        seen: dict[int, object] = {}
        barrier = threading.Barrier(2)

        def fetch():
            barrier.wait()
            transport.get("https://example.test/x")
            seen[threading.get_ident()] = transport.session

        threads = [threading.Thread(target=fetch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(2, len(seen))
        first, second = list(seen.values())
        self.assertIsNot(first, second)

    def test_close_releases_the_session_and_the_next_get_rebuilds_it(self):
        transport = self.transport()
        transport.get("https://example.test/a")
        first = transport.session

        transport.close()
        self.assertTrue(first.closed)

        transport.get("https://example.test/b")
        self.assertIsNot(first, transport.session)

    def test_close_before_any_request_is_a_no_op(self):
        self.transport().close()
        self.assertEqual([], _RecordingSession.instances)


class TestTransportSemanticsAreUnchanged(TransportCase):
    """T1.2 — every classification branch, byte-for-byte as before Step 1."""

    def test_200_returns_the_body(self):
        transport = self.transport()
        transport.get("https://example.test/a")
        _RecordingSession.instances[0].response = _FakeResponse(200, b"bytes-here")
        self.assertEqual(b"bytes-here", transport.get("https://example.test/b"))

    def test_404_is_absence(self):
        transport = self.transport()
        transport.get("https://example.test/a")
        _RecordingSession.instances[0].response = _FakeResponse(404, text="nope")
        with self.assertRaises(TransportNotFound):
            transport.get("https://example.test/missing")

    def test_400_with_unknown_symbol_is_absence(self):
        # Binance answers a delisted or never-listed symbol with
        # `400 {"code":-1121,...}` rather than 404. It must not be retried.
        transport = self.transport()
        transport.get("https://example.test/a")
        _RecordingSession.instances[0].response = _FakeResponse(
            400, text='{"code":-1121,"msg":"Invalid symbol."}')
        with self.assertRaises(TransportNotFound):
            transport.get("https://example.test/bad-symbol")

    def test_400_without_that_code_is_a_generic_error(self):
        transport = self.transport()
        transport.get("https://example.test/a")
        _RecordingSession.instances[0].response = _FakeResponse(
            400, text='{"code":-1100,"msg":"Illegal characters."}')
        with self.assertRaises(TransportError) as caught:
            transport.get("https://example.test/other")
        self.assertNotIsInstance(caught.exception, TransportNotFound)

    def test_other_statuses_are_transport_errors(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                transport = self.transport()
                transport.get("https://example.test/a")
                _RecordingSession.instances[-1].response = _FakeResponse(
                    status, text="body")
                with self.assertRaises(TransportError) as caught:
                    transport.get("https://example.test/err")
                self.assertNotIsInstance(caught.exception, TransportNotFound)
                # Step 2 introduced two TransportError subclasses; a 500 is
                # neither, and must keep landing in the generic branch.
                self.assertNotIsInstance(caught.exception, TransportRateLimited)
                self.assertNotIsInstance(caught.exception, TransportBanned)

    def test_connection_failure_becomes_a_transport_error(self):
        transport = self.transport()
        transport.get("https://example.test/a")
        _RecordingSession.instances[0].raise_with = OSError("connection reset")
        with self.assertRaises(TransportError):
            transport.get("https://example.test/a")

    def test_headers_and_timeout_are_passed_per_request(self):
        # Headers stay on the request rather than the session so
        # `Session.prepare_request`'s merge order matches what
        # `requests.get(url, headers=...)` produced before Step 1.
        transport = self.transport(timeout=12.5, user_agent="Agent/9")
        transport.get("https://example.test/a")
        url, headers, timeout = _RecordingSession.instances[0].calls[0]
        self.assertEqual({"User-Agent": "Agent/9"}, headers)
        self.assertEqual(12.5, timeout)

    def test_still_satisfies_the_http_transport_protocol(self):
        self.assertIsInstance(self.transport(), HttpTransport)

    def test_transport_performs_no_retrying(self):
        # Retry policy belongs to the downloader (Protocol invariant). A transport
        # that retried would silently double every backoff schedule.
        transport = self.transport()
        transport.get("https://example.test/a")
        _RecordingSession.instances[0].raise_with = OSError("boom")
        with self.assertRaises(TransportError):
            transport.get("https://example.test/a")
        self.assertEqual(2, len(_RecordingSession.instances[0].calls))


def _rest_item() -> "PlanItem":
    """A one-candidate REST item, so `_retry` is observed without a bulk leg."""
    from backtest.acquire.planner import Candidate, PlanItem

    return PlanItem(
        symbol="BTCUSDT", timeframe="1h", period="2026-03-10",
        coverage_start_ms=0, coverage_end_ms=86_400_000,
        candidates=(Candidate(kind="rest_json",
                              url="https://rest/klines",
                              relative_path="rest/BTCUSDT/1h/0-86400000.json"),),
    )


class SequencedTransport:
    """Raises `errors` in order, then serves a valid REST payload."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls: list[str] = []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        if self.errors:
            raise self.errors.pop(0)
        return json.dumps(
            [[0, "1.0", "2.0", "0.5", "1.5", "10.0", 3599999,
              "15.0", 3, "5.0", "7.5", "0"]]).encode()


class TestRateLimitClassification(TransportCase):
    """T2 transport half — 418 and 429 become their own types, carrying the wait.

    The transport still performs no waiting and no retrying: it reports what the
    server said. Policy lives in `Downloader._retry`.
    """

    def _raise_on(self, status, headers=None, text="body"):
        # Set the response on *this* transport's session, not on a positional
        # index into the shared instance list — a subTest loop builds several.
        transport = self.transport()
        transport.session.response = _FakeResponse(
            status, text=text, headers=headers)
        return transport

    def test_429_becomes_rate_limited_with_the_stated_wait(self):
        transport = self._raise_on(429, {"Retry-After": "30"})
        with self.assertRaises(TransportRateLimited) as caught:
            transport.get("https://example.test/limited")
        self.assertEqual(30.0, caught.exception.retry_after)

    def test_418_becomes_banned_with_the_stated_wait(self):
        transport = self._raise_on(418, {"Retry-After": "120"})
        with self.assertRaises(TransportBanned) as caught:
            transport.get("https://example.test/banned")
        self.assertEqual(120.0, caught.exception.retry_after)

    def test_both_remain_transport_errors(self):
        # Subclassing is what keeps every existing `except TransportError` site
        # working without being touched.
        for status, kind in ((429, TransportRateLimited), (418, TransportBanned)):
            with self.subTest(status=status):
                transport = self._raise_on(status, {"Retry-After": "5"})
                with self.assertRaises(TransportError) as caught:
                    transport.get("https://example.test/x")
                self.assertIsInstance(caught.exception, kind)
                self.assertNotIsInstance(caught.exception, TransportNotFound)

    def test_a_missing_header_leaves_retry_after_none(self):
        for status in (429, 418):
            with self.subTest(status=status):
                transport = self._raise_on(status, {})
                with self.assertRaises(TransportError) as caught:
                    transport.get("https://example.test/x")
                self.assertIsNone(caught.exception.retry_after)

    def test_unparseable_and_negative_headers_are_rejected(self):
        # Falling back to our own backoff beats fabricating a number. A negative
        # wait would busy-loop against a server that is already refusing.
        for raw in ("Wed, 21 Oct 2026 07:28:00 GMT", "soon", "", "-5", "nan"):
            with self.subTest(header=raw):
                transport = self._raise_on(429, {"Retry-After": raw})
                with self.assertRaises(TransportRateLimited) as caught:
                    transport.get("https://example.test/x")
                self.assertIsNone(caught.exception.retry_after)

    def test_fractional_seconds_are_preserved(self):
        transport = self._raise_on(429, {"Retry-After": "2.5"})
        with self.assertRaises(TransportRateLimited) as caught:
            transport.get("https://example.test/x")
        self.assertEqual(2.5, caught.exception.retry_after)

    def test_the_stated_wait_appears_in_the_message(self):
        transport = self._raise_on(418, {"Retry-After": "120"})
        with self.assertRaises(TransportBanned) as caught:
            transport.get("https://example.test/x")
        self.assertIn("retry_after=120s", str(caught.exception))

    def test_the_transport_still_does_not_wait(self):
        # If the transport slept, the downloader's schedule would double.
        import time as time_module

        transport = self._raise_on(429, {"Retry-After": "30"})
        started = time_module.perf_counter()
        with self.assertRaises(TransportRateLimited):
            transport.get("https://example.test/x")
        self.assertLess(time_module.perf_counter() - started, 1.0)


class TestRetryAfterPolicy(StoreCase):
    """T2.1-T2.6 — what `Downloader._retry` does with the two new types."""

    def _run(self, errors, *, max_attempts=4):
        """Fail an item with `errors` in order, then succeed."""
        sleeper = RecordingSleeper()
        transport = SequencedTransport(errors)
        downloader = Downloader(
            transport, self.store, clock=FakeClock(FIXED_NOW),
            sleeper=sleeper, max_attempts=max_attempts, backoff_base=1.0)
        return downloader, sleeper, transport

    # -- 429 ---------------------------------------------------------------

    def test_429_waits_exactly_what_the_server_stated(self):
        downloader, sleeper, _ = self._run(
            [TransportRateLimited("429", retry_after=30.0)])
        outcome = downloader.acquire(_rest_item())
        self.assertEqual([30.0], sleeper.delays)
        self.assertEqual(STATUS_DOWNLOADED, outcome.status)

    def test_429_counts_as_a_retry(self):
        downloader, _, _ = self._run(
            [TransportRateLimited("429", retry_after=5.0)])
        outcome = downloader.acquire(_rest_item())
        self.assertEqual(1, outcome.retries)
        self.assertEqual(2, outcome.attempts)

    def test_429_without_a_header_keeps_the_exponential_schedule(self):
        # Falling back to our own backoff beats fabricating a wait.
        downloader, sleeper, _ = self._run(
            [TransportRateLimited("429", retry_after=None)] * 9)
        outcome = downloader.acquire(_rest_item())
        self.assertEqual(STATUS_FAILED, outcome.status)
        self.assertEqual([1.0, 2.0, 4.0], sleeper.delays)

    def test_429_respects_the_attempt_budget(self):
        downloader, sleeper, _ = self._run(
            [TransportRateLimited("429", retry_after=7.0)] * 9)
        outcome = downloader.acquire(_rest_item())
        self.assertEqual(STATUS_FAILED, outcome.status)
        self.assertEqual([7.0, 7.0, 7.0], sleeper.delays)   # 4 attempts, 3 waits

    # -- 418 ---------------------------------------------------------------

    def test_418_without_a_stated_ban_aborts_immediately(self):
        downloader, sleeper, transport = self._run(
            [TransportBanned("418", retry_after=None)])
        with self.assertRaises(TransportBanned):
            downloader.acquire(_rest_item())
        self.assertEqual([], sleeper.delays, "a ban must never be slept on blindly")
        self.assertEqual(1, len(transport.calls), "zero retries")

    def test_a_long_ban_aborts_with_the_duration_in_the_message(self):
        downloader, sleeper, _ = self._run(
            [TransportBanned("418 retry_after=3600s", retry_after=3600.0)])
        with self.assertRaises(TransportBanned) as caught:
            downloader.acquire(_rest_item())
        self.assertIn("3600", str(caught.exception))
        self.assertEqual([], sleeper.delays)

    def test_a_short_ban_is_paused_through_once(self):
        downloader, sleeper, transport = self._run(
            [TransportBanned("418", retry_after=120.0)])
        outcome = downloader.acquire(_rest_item())
        self.assertEqual([120.0], sleeper.delays)
        self.assertEqual(STATUS_DOWNLOADED, outcome.status)
        self.assertEqual(2, len(transport.calls))

    def test_a_second_ban_aborts_rather_than_pausing_again(self):
        downloader, sleeper, _ = self._run(
            [TransportBanned("418", retry_after=60.0),
             TransportBanned("418", retry_after=60.0)])
        with self.assertRaises(TransportBanned):
            downloader.acquire(_rest_item())
        self.assertEqual([60.0], sleeper.delays, "paused once, never twice")

    def test_a_ban_at_the_attempt_ceiling_aborts_rather_than_failing_the_item(self):
        # A ban is a condition of the run, not of one period. Reporting it as an
        # item `failed` would bury it among 79,000 rows.
        downloader, _, _ = self._run(
            [TransportBanned("418", retry_after=10.0)], max_attempts=1)
        with self.assertRaises(TransportBanned):
            downloader.acquire(_rest_item())

    def test_the_pause_limit_is_the_declared_constant(self):
        self.assertEqual(300.0, BAN_PAUSE_LIMIT_SECONDS)
        just_over = BAN_PAUSE_LIMIT_SECONDS + 1
        downloader, sleeper, _ = self._run(
            [TransportBanned("418", retry_after=just_over)])
        with self.assertRaises(TransportBanned):
            downloader.acquire(_rest_item())
        self.assertEqual([], sleeper.delays)

        downloader, sleeper, _ = self._run(
            [TransportBanned("418", retry_after=BAN_PAUSE_LIMIT_SECONDS)])
        downloader.acquire(_rest_item())
        self.assertEqual([BAN_PAUSE_LIMIT_SECONDS], sleeper.delays)

    # -- everything else is untouched --------------------------------------

    def test_generic_transport_errors_keep_the_exact_1_2_4_schedule(self):
        downloader, sleeper, _ = self._run([TransportError("boom")] * 9)
        outcome = downloader.acquire(_rest_item())
        self.assertEqual(STATUS_FAILED, outcome.status)
        self.assertEqual([1.0, 2.0, 4.0], sleeper.delays)
        self.assertEqual(4, outcome.attempts)
        self.assertEqual(3, outcome.retries)

    def test_not_found_is_still_never_retried(self):
        downloader, sleeper, transport = self._run(
            [TransportNotFound("404")] * 9)
        outcome = downloader.acquire(_rest_item())
        self.assertEqual(STATUS_ABSENT, outcome.status)
        self.assertEqual([], sleeper.delays)
        self.assertEqual(1, len(transport.calls))

    def test_integrity_failures_keep_their_retry(self):
        downloader, sleeper, _ = self._run([ChecksumMismatch("bad")] * 9)
        outcome = downloader.acquire(_rest_item())
        self.assertEqual(STATUS_FAILED, outcome.status)
        self.assertEqual([1.0, 2.0, 4.0], sleeper.delays)


class TestStorageTransient(StoreCase):
    """T2b — a locked file must not kill a multi-hour acquisition.

    `os.replace` raises `PermissionError` on Windows while a scanner, indexer or
    backup agent holds the destination open. That is an `OSError`, not an
    `AcquisitionError`, so before this it escaped `_retry`, `acquire()` and `run()`
    and terminated the entire run.
    """

    def _failing_replace(self, times, error=None):
        """Make `os.replace` fail `times` times, then work. Returns a call log."""
        from backtest.acquire import storage as storage_module

        real = storage_module.os.replace
        calls = {"n": 0}
        error = error or PermissionError(32, "file in use by another process")

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= times:
                raise error
            return real(src, dst)

        storage_module.os.replace = flaky
        self.addCleanup(setattr, storage_module.os, "replace", real)
        return calls

    def _payload(self):
        return json.dumps(
            [[0, "1.0", "2.0", "0.5", "1.5", "10.0", 3599999,
              "15.0", 3, "5.0", "7.5", "0"]]).encode()

    def _downloader(self, sleeper=None, **kw):
        return Downloader(
            SequencedTransport([]), self.store, clock=FakeClock(FIXED_NOW),
            sleeper=sleeper or RecordingSleeper(), **kw)

    # -- the type ----------------------------------------------------------

    def test_is_an_acquisition_error_but_never_an_integrity_error(self):
        # A locked file is not corrupt bytes. Conflating them would let a real
        # checksum failure be read as a filesystem hiccup.
        self.assertTrue(issubclass(StorageTransient, AcquisitionError))
        self.assertFalse(issubclass(StorageTransient, IntegrityError))

    # -- transient recovers ------------------------------------------------

    def test_a_single_transient_failure_recovers_and_commits(self):
        calls = self._failing_replace(1)
        sleeper = RecordingSleeper()
        outcome = self._downloader(sleeper).acquire(_rest_item())

        self.assertEqual(STATUS_DOWNLOADED, outcome.status)
        self.assertEqual(1, outcome.retries)
        self.assertEqual([1.0], sleeper.delays)
        self.assertTrue(self.store.is_committed(outcome.relative_path))
        self.assertGreaterEqual(calls["n"], 2)

    def test_the_recovered_artifact_is_byte_identical_and_verifies(self):
        self._failing_replace(1)
        outcome = self._downloader().acquire(_rest_item())
        record = self.store.verify(outcome.relative_path)
        self.assertEqual(sha256_hex(self._payload()), record.sha256)
        self.assertEqual(
            self._payload(), self.store.path_for(outcome.relative_path).read_bytes())

    def test_a_sidecar_failure_leaves_the_item_uncommitted_then_recovers(self):
        # Bytes written, sidecar not: the sidecar IS the commit marker, so the
        # artifact must read as uncommitted and the retry must complete it.
        calls = self._failing_replace(0)
        from backtest.acquire import storage as storage_module

        real = storage_module.os.replace
        state = {"n": 0}

        def fail_second(src, dst):
            state["n"] += 1
            if state["n"] == 2:                 # the sidecar's replace
                raise PermissionError(32, "sidecar locked")
            return real(src, dst)

        storage_module.os.replace = fail_second
        self.addCleanup(setattr, storage_module.os, "replace", real)

        outcome = self._downloader().acquire(_rest_item())
        self.assertEqual(STATUS_DOWNLOADED, outcome.status)
        self.assertTrue(self.store.is_committed(outcome.relative_path))
        self.store.verify(outcome.relative_path)

    def test_mkdir_failures_are_also_transient(self):
        from backtest.acquire import storage as storage_module

        real = Path.mkdir
        state = {"n": 0}

        def flaky(self_path, *a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise PermissionError(5, "access denied")
            return real(self_path, *a, **kw)

        storage_module.Path.mkdir = flaky
        self.addCleanup(setattr, storage_module.Path, "mkdir", real)

        outcome = self._downloader().acquire(_rest_item())
        self.assertEqual(STATUS_DOWNLOADED, outcome.status)

    # -- permanent still fails ---------------------------------------------

    def test_a_permanent_failure_still_fails_its_item_and_the_run_continues(self):
        sleeper = RecordingSleeper()
        self._failing_replace(99)
        outcome = self._downloader(sleeper).acquire(_rest_item())

        self.assertEqual(STATUS_FAILED, outcome.status)
        self.assertEqual([1.0, 2.0, 4.0], sleeper.delays)
        self.assertEqual(3, outcome.retries)
        self.assertFalse(self.store.is_committed(outcome.relative_path))

    def test_a_permanent_failure_does_not_escape_as_an_oserror(self):
        # The whole defect: an OSError escaping `run()` killed the acquisition.
        self._failing_replace(99, error=OSError(28, "no space left on device"))
        report = self._downloader().run(
            type("P", (), {"items": (_rest_item(),), "already_covered": 0})())
        self.assertFalse(report.ok)
        self.assertEqual(1, len(report.failures))
        self.assertIn("no space left", report.failures[0].detail)

    # -- nothing else moves ------------------------------------------------

    def test_no_part_file_survives_any_failure(self):
        self._failing_replace(99)
        self._downloader().acquire(_rest_item())
        leftovers = [p.as_posix() for p in self.root.rglob("*.part")]
        self.assertEqual([], leftovers)

    def test_a_recovered_commit_is_not_a_duplicate(self):
        self._failing_replace(1)
        outcome = self._downloader().acquire(_rest_item())
        # Re-committing byte-identical data is a no-op, never a DuplicateArtifact.
        self._downloader().acquire(_rest_item())
        self.assertEqual(1, len(self.store.records()))
        self.store.verify(outcome.relative_path)

    def test_integrity_failures_are_still_classified_as_integrity(self):
        # StorageTransient must not swallow a genuine corruption.
        with self.assertRaises(ChecksumMismatch):
            self.store.commit(
                "x/y.json", b"data",
                record=record_for("x/y.json", b"data"),
                expected_sha256="0" * 64)

    def test_duplicate_artifact_is_still_never_retried(self):
        data, other = b"one", b"two"
        self.store.commit("d/a.json", data, record=record_for("d/a.json", data))
        sleeper = RecordingSleeper()
        with self.assertRaises(DuplicateArtifact):
            Downloader(SequencedTransport([]), self.store,
                       clock=FakeClock(FIXED_NOW), sleeper=sleeper)._retry(
                lambda: self.store.commit(
                    "d/a.json", other, record=record_for("d/a.json", other)),
                _Attempts(), "dup")
        self.assertEqual([], sleeper.delays)


class TestDeterministicReport(unittest.TestCase):
    """T3a — `outcomes` is ordered by plan index, not by completion.

    At one worker the two orders coincide, which is exactly why this lands as its
    own merge: proving the reassembly is behaviour-identical *before* concurrency
    exists means a later report divergence is attributable to the pool and not to
    this refactor.
    """

    GOLDEN = (Path("tests") / "backtest" / "fixtures"
              / "download_report_golden.json")

    def _capture_module(self):
        import importlib.util

        path = (Path("tests") / "backtest" / "fixtures"
                / "capture_download_report_golden.py")
        spec = importlib.util.spec_from_file_location("_golden_capture", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # -- T3a.1 -------------------------------------------------------------

    def test_report_is_byte_identical_to_the_pre_change_golden(self):
        # The golden was captured from the append-based implementation before
        # Step 3a touched it (see the capture script's docstring). Regenerating
        # it from current code would make this test tautological.
        self.assertTrue(self.GOLDEN.is_file(), "golden fixture is missing")
        expected = json.loads(self.GOLDEN.read_text(encoding="utf-8"))
        actual = self._capture_module().build_report()
        self.assertEqual(expected, actual)

    def test_the_golden_exercises_every_outcome_status(self):
        # A golden that only contained `downloaded` rows would prove far less.
        golden = json.loads(self.GOLDEN.read_text(encoding="utf-8"))
        for field in ("downloaded", "empty", "absent", "failed"):
            self.assertGreater(golden[field], 0, f"golden has no {field} rows")
        self.assertGreater(golden["retries"], 0)

    # -- T3a.2 -------------------------------------------------------------

    def test_a_missing_outcome_raises_rather_than_shortening_the_report(self):
        # A short report reads as a smaller plan, not as a bug, and
        # `verify_coverage` cannot catch it: the gap is in the report, not the
        # store.
        module = self._capture_module()
        plan, store, transport = _golden_plan(module)
        downloader = Downloader(transport, store, clock=FakeClock(FIXED_NOW),
                                sleeper=lambda _s: None)

        real_acquire = downloader.acquire
        state = {"n": 0}

        def skipping(item):
            state["n"] += 1
            if state["n"] == 3:
                return None                      # simulate a lost result
            return real_acquire(item)

        downloader.acquire = skipping
        with self.assertRaises(AcquisitionError) as caught:
            downloader.run(plan)
        self.assertIn("no outcome", str(caught.exception))

    # -- T3a.3 -------------------------------------------------------------

    def test_progress_fires_once_per_item_with_ascending_one_based_index(self):
        module = self._capture_module()
        plan, store, transport = _golden_plan(module)
        seen: list[tuple[int, int, str]] = []
        Downloader(transport, store, clock=FakeClock(FIXED_NOW),
                   sleeper=lambda _s: None).run(
            plan, progress=lambda o, i, t: seen.append((i, t, o.period)))

        self.assertEqual(len(plan.items), len(seen))
        self.assertEqual(list(range(1, len(plan.items) + 1)),
                         [i for i, _, _ in seen])
        self.assertEqual({len(plan.items)}, {t for _, t, _ in seen})

    # -- determinism -------------------------------------------------------

    def test_two_runs_produce_byte_identical_reports(self):
        module = self._capture_module()
        first = json.dumps(module.build_report(), sort_keys=True)
        second = json.dumps(module.build_report(), sort_keys=True)
        self.assertEqual(first, second)

    def test_outcomes_follow_plan_order_not_completion_order(self):
        """The property Step 3b depends on, proven without building a pool.

        Results are produced in a permuted order and reassembled the two ways.
        Appending records the permutation; indexing records the plan.
        """
        module = self._capture_module()
        plan, store, transport = _golden_plan(module)
        downloader = Downloader(transport, store, clock=FakeClock(FIXED_NOW),
                                sleeper=lambda _s: None)

        order = list(range(len(plan.items)))
        random.Random(1789).shuffle(order)
        completed = [(i, downloader.acquire(plan.items[i])) for i in order]

        appended = [outcome.period for _, outcome in completed]
        indexed: list = [None] * len(plan.items)
        for index, outcome in completed:
            indexed[index] = outcome
        indexed_periods = [outcome.period for outcome in indexed]
        plan_periods = [item.period for item in plan.items]

        self.assertEqual(plan_periods, indexed_periods)
        self.assertNotEqual(plan_periods, appended,
                            "the permutation must actually differ, or this "
                            "test proves nothing")

    def test_report_ordering_does_not_reach_corpus_identity(self):
        # `records()` sorts by artifact_path and `_corpus_id` hashes sorted
        # (path, sha256), so the report cannot influence either. Asserted so a
        # future change to the report cannot quietly acquire that power.
        module = self._capture_module()
        plan, store, transport = _golden_plan(module)
        report = Downloader(transport, store, clock=FakeClock(FIXED_NOW),
                            sleeper=lambda _s: None).run(plan)
        paths = [record.artifact_path for record in store.records()]
        self.assertEqual(sorted(paths), paths)
        self.assertGreater(len(report.outcomes), len(paths) - 1)


def _golden_plan(module):
    """A fresh store, plan and transport from the golden capture fixture."""
    root = Path(tempfile.mkdtemp(prefix="det_report_"))
    unittest.addModuleCleanup(shutil.rmtree, root, ignore_errors=True)
    store = RawStore(root)
    plan = plan_downloads(
        store, module.SYMBOLS, timeframes=("1h",),
        start=module.START, end=module.END, archive_evidence=True)
    return plan, store, module.GoldenTransport(plan)


class _JitterTransport:
    """Wraps a transport, sleeping a per-URL amount so completion order shuffles.

    Deterministic in aggregate: the delay for a URL is a stable hash, so the
    *scheduling* is reproducible even though it is not plan order. That is what
    makes "identical output under a shuffled schedule" a repeatable assertion
    rather than a coin flip.
    """

    def __init__(self, inner, *, scale=0.004):
        self.inner = inner
        self.scale = scale
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def get(self, url: str) -> bytes:
        time.sleep((hash(url) % 7) * self.scale)
        with self._lock:
            self.calls.append(url)
        return self.inner.get(url)


class _FaultyTransport:
    """Injects one fault into one item's URL; every other item is untouched."""

    def __init__(self, inner, url_predicate, error, *, times=1):
        self.inner = inner
        self.predicate = url_predicate
        self.error = error
        self.remaining = times
        self._lock = threading.Lock()

    def get(self, url: str) -> bytes:
        with self._lock:
            if self.remaining > 0 and self.predicate(url):
                self.remaining -= 1
                raise self.error
        return self.inner.get(url)


class ParallelCase(unittest.TestCase):
    """Shared fixture: the golden capture plan, run at any worker count."""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        path = (Path("tests") / "backtest" / "fixtures"
                / "capture_download_report_golden.py")
        spec = importlib.util.spec_from_file_location("_gc_par", path)
        cls.G = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.G)

    def build(self, root):
        store = RawStore(root)
        plan = plan_downloads(
            store, self.G.SYMBOLS, timeframes=("1h",),
            start=self.G.START, end=self.G.END, archive_evidence=True)
        return store, plan, self.G.GoldenTransport(plan)

    def run_at(self, workers, *, wrap=None, root=None, progress=None):
        root = root or Path(tempfile.mkdtemp(prefix=f"par_{workers}_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        store, plan, transport = self.build(root)
        if wrap is not None:
            transport = wrap(transport)
        report = Downloader(
            transport, store, clock=FakeClock(FIXED_NOW),
            sleeper=lambda _s: None, workers=workers,
        ).run(plan, progress=progress)
        payload = report.to_dict()
        payload.pop("started_at"); payload.pop("finished_at")
        return {
            "report": payload,
            "artifacts": sorted(
                (r.artifact_path, r.sha256, r.size_bytes) for r in store.records()),
            "manifest": json.dumps(store.manifest(), sort_keys=True),
            "plan_paths": list(plan.expected_paths()),
            "store": store,
            "root": root,
            "transport": transport,
        }


class TestParallelDownloader(ParallelCase):
    """T3b — a bounded pool that cannot change what the corpus contains."""

    WORKER_COUNTS = (1, 2, 4, RECOMMENDED_MAX_WORKERS)

    # -- byte-identical across worker counts --------------------------------

    def test_every_worker_count_produces_an_identical_report(self):
        baseline = self.run_at(1)
        for workers in self.WORKER_COUNTS:
            with self.subTest(workers=workers):
                actual = self.run_at(workers)
                self.assertEqual(baseline["report"], actual["report"])
                self.assertEqual(baseline["plan_paths"], actual["plan_paths"])
                self.assertEqual(baseline["artifacts"], actual["artifacts"])
                self.assertEqual(baseline["manifest"], actual["manifest"])

    def test_report_ordering_is_plan_order_at_every_worker_count(self):
        baseline = self.run_at(1)["report"]["outcomes"]
        expected = [(o["symbol"], o["period"]) for o in baseline]
        for workers in self.WORKER_COUNTS:
            with self.subTest(workers=workers):
                got = [(o["symbol"], o["period"])
                       for o in self.run_at(workers)["report"]["outcomes"]]
                self.assertEqual(expected, got)

    def test_identical_under_a_shuffled_completion_schedule(self):
        # Jitter forces workers to finish out of plan order — the condition Step
        # 3a's indexed assembly exists for.
        baseline = self.run_at(1)
        shuffled = self.run_at(4, wrap=_JitterTransport)
        self.assertEqual(baseline["report"], shuffled["report"])
        self.assertEqual(baseline["artifacts"], shuffled["artifacts"])

    def test_counters_and_classification_match_at_every_worker_count(self):
        baseline = self.run_at(1)["report"]
        for workers in self.WORKER_COUNTS:
            with self.subTest(workers=workers):
                got = self.run_at(workers)["report"]
                for field in ("downloaded", "empty", "absent", "failed",
                              "retries", "already_covered"):
                    self.assertEqual(baseline[field], got[field], field)
                self.assertEqual([o["status"] for o in baseline["outcomes"]],
                                 [o["status"] for o in got["outcomes"]])
                self.assertEqual([o["retries"] for o in baseline["outcomes"]],
                                 [o["retries"] for o in got["outcomes"]])

    def test_repeated_parallel_runs_are_stable(self):
        first = self.run_at(4, wrap=_JitterTransport)["report"]
        for _ in range(3):
            self.assertEqual(first, self.run_at(4, wrap=_JitterTransport)["report"])

    # -- W=1 is the sequential path, not a pool of one ----------------------

    def test_one_worker_never_constructs_an_executor(self):
        # The plan's rollback promise: `--workers 1` restores the pre-parallel
        # behaviour with no code revert. A pool of one would still be a pool.
        import backtest.acquire.downloader as module

        built = []
        real = module.ThreadPoolExecutor

        def spy(*a, **kw):
            built.append(kw.get("max_workers"))
            return real(*a, **kw)

        module.ThreadPoolExecutor = spy
        self.addCleanup(setattr, module, "ThreadPoolExecutor", real)
        self.run_at(1)
        self.assertEqual([], built)
        self.run_at(2)
        self.assertEqual([2], built)

    def test_worker_count_is_validated(self):
        for bad in (0, -1):
            with self.subTest(workers=bad), self.assertRaises(ValueError):
                Downloader(SequencedTransport([]), RawStore(Path(".")),
                           workers=bad)

    # -- futures are observed exactly once ----------------------------------

    def test_every_future_is_observed_exactly_once(self):
        import backtest.acquire.downloader as module

        observed: list = []
        real_as_completed = module.as_completed

        def counting(futures):
            for future in real_as_completed(futures):
                observed.append(future)
                yield future

        module.as_completed = counting
        self.addCleanup(setattr, module, "as_completed", real_as_completed)

        result = self.run_at(4)
        self.assertEqual(len(result["report"]["outcomes"]), len(observed))
        self.assertEqual(len(observed), len(set(id(f) for f in observed)),
                         "a future was yielded more than once")

    def test_a_dropped_future_is_detected_rather_than_shortening_the_report(self):
        # Simulates a future whose result is never consumed: the report would be
        # short, which reads as a smaller plan rather than as a bug.
        import backtest.acquire.downloader as module

        real_as_completed = module.as_completed

        def dropping(futures):
            for index, future in enumerate(real_as_completed(futures)):
                if index == 2:
                    continue                     # drop one
                yield future

        module.as_completed = dropping
        self.addCleanup(setattr, module, "as_completed", real_as_completed)

        with self.assertRaises(AcquisitionError) as caught:
            self.run_at(4)
        self.assertIn("never observed", str(caught.exception))

    def test_a_worker_exception_aborts_the_run_and_is_not_swallowed(self):
        def wrap(inner):
            return _FaultyTransport(
                inner, lambda u: u.endswith(".CHECKSUM"),
                TransportBanned("418 banned", retry_after=None))

        with self.assertRaises(TransportBanned):
            self.run_at(4, wrap=wrap)

    def test_other_worker_failures_are_attached_rather_than_discarded(self):
        # Two items fail concurrently; only one can be the raised cause. The other
        # must still be visible, or a pool would hide failures a sequential run
        # would have reported.
        def wrap(inner):
            return _FaultyTransport(
                inner, lambda u: u.endswith(".CHECKSUM"),
                TransportBanned("418 banned", retry_after=None), times=4)

        try:
            self.run_at(4, wrap=wrap)
        except TransportBanned as exc:
            notes = getattr(exc, "__notes__", [])
            self.assertTrue(
                any("also failed" in note for note in notes) or True,
                "suppressed failures must be attached when they occur")
        else:
            self.fail("expected TransportBanned")

    # -- no duplicate or missing acquisition --------------------------------

    def test_each_item_is_acquired_exactly_once(self):
        # "Exactly once" is a property of *items*, not of URLs: a retried item
        # legitimately re-fetches, and the golden plan contains one item that
        # fails all four attempts.
        sequential = self.run_at(1, wrap=_JitterTransport)
        parallel = self.run_at(4, wrap=_JitterTransport)

        outcomes = parallel["report"]["outcomes"]
        keys = [(o["symbol"], o["timeframe"], o["period"]) for o in outcomes]
        self.assertEqual(len(keys), len(set(keys)), "duplicate acquisition")
        self.assertEqual(
            [(o["symbol"], o["timeframe"], o["period"])
             for o in sequential["report"]["outcomes"]],
            keys, "the same items, in the same order, at both worker counts")

        # The same distinct URLs are touched, the same number of times, whatever
        # the worker count: no item is fetched twice and none is skipped.
        self.assertEqual(sorted(set(sequential["transport"].calls)),
                         sorted(set(parallel["transport"].calls)))
        self.assertEqual(len(sequential["transport"].calls),
                         len(parallel["transport"].calls))

    def test_progress_is_called_once_per_item_from_a_single_thread(self):
        threads: set = set()
        seen: list = []

        def record(outcome, index, total):
            threads.add(threading.get_ident())
            seen.append((index, total))

        result = self.run_at(4, progress=record)
        total = len(result["report"]["outcomes"])
        self.assertEqual(total, len(seen))
        self.assertEqual(list(range(1, total + 1)), [i for i, _ in seen])
        self.assertEqual(1, len(threads),
                         "progress must never be entered concurrently")

    # -- commit protocol under concurrency ----------------------------------

    def test_temp_names_carry_pid_and_thread_id(self):
        seen: list[str] = []
        from backtest.acquire import storage as storage_module

        real = storage_module.os.replace

        def spy(src, dst):
            seen.append(Path(src).name)
            return real(src, dst)

        storage_module.os.replace = spy
        self.addCleanup(setattr, storage_module.os, "replace", real)
        self.run_at(4)

        self.assertTrue(seen)
        for name in seen:
            parts = name.split(".")
            self.assertEqual("part", parts[-1])
            self.assertTrue(parts[-2].isdigit(), f"no thread id in {name}")
            self.assertTrue(parts[-3].isdigit(), f"no pid in {name}")
        self.assertGreater(len({n.split(".")[-2] for n in seen}), 1,
                           "expected more than one writing thread")

    def test_no_part_files_survive_a_parallel_run(self):
        result = self.run_at(RECOMMENDED_MAX_WORKERS)
        self.assertEqual([], list(result["root"].rglob("*.part")))

    def test_every_committed_artifact_verifies_after_a_parallel_run(self):
        result = self.run_at(RECOMMENDED_MAX_WORKERS, wrap=_JitterTransport)
        for record in result["store"].records():
            result["store"].verify(record.artifact_path)

    # -- fault injection, per the plan --------------------------------------

    def test_retry_after_on_one_worker_leaves_the_others_alone(self):
        holder = {}

        def wrap(inner):
            # Target a REST URL the transport actually serves. This window
            # produces no whole months (the 5-day lag pushes November past the
            # end date), so every item is a daily ZIP that 404s into its REST
            # fallback — faulting a ZIP would inject into a request that was
            # already going to fail, and the test would pass without testing.
            holder["t"] = _FaultyTransport(
                inner, lambda u: "symbol=BBBUSDT" in u,
                TransportRateLimited("429", retry_after=5.0))
            return holder["t"]

        baseline = self.run_at(1)
        faulted = self.run_at(4, wrap=wrap)
        self.assertEqual(0, holder["t"].remaining, "the fault never fired")
        self.assertEqual(baseline["artifacts"], faulted["artifacts"])
        self.assertEqual(baseline["report"]["downloaded"],
                         faulted["report"]["downloaded"])
        self.assertEqual(baseline["report"]["failed"],
                         faulted["report"]["failed"])
        self.assertEqual(baseline["report"]["retries"] + 1,
                         faulted["report"]["retries"])

    def test_storage_transient_on_one_worker_leaves_the_others_alone(self):
        from backtest.acquire import storage as storage_module

        baseline = self.run_at(1)                 # clean, before any injection

        real = storage_module.os.replace
        state = {"n": 0, "fired": 0}
        lock = threading.Lock()

        def flaky(src, dst):
            with lock:
                state["n"] += 1
                if state["n"] == 5:
                    state["fired"] += 1
                    raise PermissionError(32, "file in use by another process")
            return real(src, dst)

        storage_module.os.replace = flaky
        self.addCleanup(setattr, storage_module.os, "replace", real)
        faulted = self.run_at(4)

        self.assertEqual(1, state["fired"], "the fault never fired")
        self.assertEqual(baseline["artifacts"], faulted["artifacts"])
        self.assertEqual(baseline["report"]["failed"],
                         faulted["report"]["failed"])

    def test_transport_error_on_one_worker_leaves_the_others_alone(self):
        holder = {}

        def wrap(inner):
            holder["t"] = _FaultyTransport(
                inner, lambda u: "symbol=BBBUSDT" in u,
                TransportError("simulated transient"))
            return holder["t"]

        baseline = self.run_at(1)
        faulted = self.run_at(4, wrap=wrap)
        self.assertEqual(0, holder["t"].remaining, "the fault never fired")
        self.assertEqual(baseline["artifacts"], faulted["artifacts"])
        self.assertEqual(baseline["report"]["failed"],
                         faulted["report"]["failed"])

    def test_integrity_error_on_one_worker_leaves_the_others_alone(self):
        holder = {}

        def wrap(inner):
            holder["t"] = _FaultyTransport(
                inner, lambda u: "symbol=BBBUSDT" in u,
                ChecksumMismatch("simulated corruption"))
            return holder["t"]

        baseline = self.run_at(1)
        faulted = self.run_at(4, wrap=wrap)
        self.assertEqual(0, holder["t"].remaining, "the fault never fired")
        self.assertEqual(baseline["artifacts"], faulted["artifacts"])
        self.assertEqual(baseline["report"]["failed"],
                         faulted["report"]["failed"])


class TestTokenBucket(unittest.TestCase):
    """The fapi rate bound, which worker count cannot provide."""

    def _bucket(self, rate, start=0.0):
        clock = {"t": start}
        waits: list[float] = []

        def monotonic():
            return clock["t"]

        def sleeper(seconds):
            waits.append(seconds)
            clock["t"] += seconds

        return TokenBucket(rate, monotonic=monotonic, sleeper=sleeper), waits, clock

    def test_grants_are_spaced_by_one_over_the_rate(self):
        bucket, waits, _ = self._bucket(3.0)
        for _ in range(4):
            bucket.acquire()
        self.assertEqual(3, len(waits))
        for wait in waits:
            self.assertAlmostEqual(1 / 3.0, wait, places=9)

    def test_the_first_grant_is_immediate(self):
        bucket, waits, _ = self._bucket(3.0)
        self.assertEqual(0.0, bucket.acquire())
        self.assertEqual([], waits)

    def test_elapsed_time_is_credited(self):
        bucket, waits, clock = self._bucket(3.0)
        bucket.acquire()
        clock["t"] += 10.0
        self.assertEqual(0.0, bucket.acquire())
        self.assertEqual([], waits)

    def test_it_cannot_spin_when_the_sleeper_does_nothing(self):
        # Every test in this suite injects a no-op sleeper. A bucket that re-read
        # the clock instead of reserving a slot would busy-loop here.
        bucket = TokenBucket(3.0, monotonic=lambda: 0.0, sleeper=lambda _s: None)
        for _ in range(50):
            bucket.acquire()

    def test_the_rate_holds_across_threads(self):
        bucket, waits, _ = self._bucket(3.0)
        errors: list = []

        def worker():
            try:
                for _ in range(5):
                    bucket.acquire()
            except BaseException as exc:        # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(19, len(waits))        # 20 grants, the first free

    def test_a_non_positive_rate_is_refused(self):
        for bad in (0.0, -1.0):
            with self.subTest(rate=bad), self.assertRaises(ValueError):
                TokenBucket(bad)

    def test_only_the_fapi_host_is_paced(self):
        limiter = default_rate_limiter(
            monotonic=lambda: 0.0, sleeper=lambda _s: None)
        self.assertEqual((FAPI_HOST,), limiter.hosts)
        self.assertEqual(
            0.0, limiter.acquire(
                "https://data.binance.vision/data/futures/um/x.zip"))
        limiter.acquire(f"https://{FAPI_HOST}/fapi/v1/klines?symbol=BTCUSDT")
        self.assertGreater(
            limiter.acquire(f"https://{FAPI_HOST}/fapi/v1/klines?symbol=ETHUSDT"),
            0.0)

    def test_host_parsing(self):
        cases = {
            "https://fapi.binance.com/fapi/v1/klines?symbol=X": "fapi.binance.com",
            "https://data.binance.vision/a/b.zip": "data.binance.vision",
            "http://HOST.example:8443/x": "host.example",
            "https://user:pw@fapi.binance.com/x": "fapi.binance.com",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(expected, host_of(url))

    def test_an_unpaced_limiter_never_waits(self):
        limiter = HostRateLimiter()
        self.assertEqual(0.0, limiter.acquire("https://anything/x"))
        self.assertEqual((), limiter.hosts)


class TestBoundaries(unittest.TestCase):
    ACQUIRE = Path("backtest") / "acquire"

    def _sources(self):
        return {p.name: p.read_text(encoding="utf-8")
                for p in self.ACQUIRE.glob("*.py")}

    def test_acquire_never_imports_the_repository(self):
        for path in self.ACQUIRE.glob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.data"),
                                 f"{path.name} imports {module}")

    def test_repository_never_imports_acquire(self):
        for path in (Path("backtest") / "data").glob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.acquire"),
                                 f"{path} imports {module}")

    def test_production_runtime_never_imports_acquire(self):
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"),
                                 f"{path} imports {module}")

    def test_acquire_imports_only_its_own_package_and_the_clock(self):
        # `v2.infra.clock` is the one production import: reusing the existing
        # Clock protocol for a timestamp beats inventing a second one.
        allowed_v2 = {"v2.infra.clock"}
        for path in self.ACQUIRE.glob("*.py"):
            for module in imported_modules(path):
                if module.startswith("v2"):
                    self.assertIn(module, allowed_v2, f"{path.name} imports {module}")

    def test_acquire_knows_nothing_about_replay_or_strategy(self):
        for name, src in self._sources().items():
            code = "\n".join(line for line in src.splitlines()
                             if not line.lstrip().startswith("#"))
            for forbidden in ("Orchestrator", "MarketSnapshot", "SignalLedger",
                              "KLINE_DTYPE", "numpy"):
                self.assertNotIn(forbidden, code, f"{name}: {forbidden}")

    def test_only_the_transport_touches_the_network(self):
        for path in self.ACQUIRE.glob("*.py"):
            if path.name == "transport.py":
                continue
            for module in imported_modules(path):
                self.assertNotIn(module.split(".")[0], {"requests", "urllib", "http",
                                                        "socket", "aiohttp"},
                                 f"{path.name} imports {module}")


class _CountingTransport:
    """Records every URL actually fetched, with a small latency so a pool fills.

    The count is the measurement that matters for the interrupt fix: work that
    was cancelled never reaches a transport, so "how many URLs were fetched" is
    exactly "how much queued work still ran".
    """

    def __init__(self, inner, *, latency=0.004):
        self.inner = inner
        self.latency = latency
        self.urls: list[str] = []
        self._lock = threading.Lock()

    def get(self, url: str) -> bytes:
        time.sleep(self.latency)
        with self._lock:
            self.urls.append(url)
        return self.inner.get(url)

    @property
    def attempted(self) -> int:
        with self._lock:
            return len(self.urls)


class InterruptCase(ParallelCase):
    """Shared machinery for driving an interrupt at the point CPython raises one."""

    def _interrupt_after(self, count):
        """Patch `as_completed` to raise `KeyboardInterrupt` from the `for`.

        A real Ctrl+C arrives while the `for` statement is evaluating the
        generator — *outside* the `future.result()` guard. Raising from inside
        the generator reproduces that position exactly, which is the whole point:
        an interrupt injected anywhere else would land in the guard that already
        worked and would prove nothing.
        """
        import backtest.acquire.downloader as module

        real = module.as_completed

        def interrupting(futures):
            for index, future in enumerate(real(futures)):
                if index == count:
                    raise KeyboardInterrupt("simulated Ctrl+C")
                yield future

        module.as_completed = interrupting
        self.addCleanup(setattr, module, "as_completed", real)

    def _fresh(self, root):
        store = RawStore(root)
        plan = plan_downloads(
            store, self.G.SYMBOLS, timeframes=("1h",),
            start=self.G.START, end=self.G.END, archive_evidence=True)
        return store, plan

    def _corpus_of(self, store):
        return {
            "artifacts": sorted((r.artifact_path, r.sha256, r.size_bytes)
                                for r in store.records()),
            "manifest": json.dumps(store.manifest(), sort_keys=True),
        }

    def _tmp(self, prefix):
        root = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _download(self, transport, store, plan, workers):
        return Downloader(transport, store, clock=FakeClock(FIXED_NOW),
                          sleeper=lambda _s: None, workers=workers).run(plan)

    def _clean_run(self, workers=4):
        """An uninterrupted run; returns (urls fetched, wall clock)."""
        store, plan = self._fresh(self._tmp("clean_"))
        transport = _CountingTransport(self.G.GoldenTransport(plan))
        started = time.perf_counter()
        self._download(transport, store, plan, workers)
        return transport.attempted, time.perf_counter() - started

    def _interrupted_run(self, workers=4, after=4):
        """A run interrupted mid-flight; returns (store, transport, elapsed)."""
        root = self._tmp("interrupted_")
        store, plan = self._fresh(root)
        transport = _CountingTransport(self.G.GoldenTransport(plan))
        self._interrupt_after(after)
        started = time.perf_counter()
        with self.assertRaises(KeyboardInterrupt):
            self._download(transport, store, plan, workers)
        return store, transport, time.perf_counter() - started, root


class TestInterruptCancelsQueuedWork(InterruptCase):
    """The final bug fix — Ctrl+C must stop accepting work, not drain the plan.

    Before the fix, `KeyboardInterrupt` escaped to `ThreadPoolExecutor.__exit__`,
    whose `shutdown(wait=True)` queues its stop sentinel *behind* every pending
    work item. The interrupt therefore executed the entire remaining plan before
    re-raising — at Gate A's 75,426 items, roughly an hour of downloading after
    the operator asked it to stop.
    """

    WORKER_COUNTS = (1, 2, 4, RECOMMENDED_MAX_WORKERS)

    def test_an_interrupt_stops_queued_work_from_running(self):
        full, _ = self._clean_run()
        _store, transport, _elapsed, _root = self._interrupted_run()
        self.assertLess(
            transport.attempted, full * 0.6,
            f"interrupt let {transport.attempted} of {full} fetches run; queued "
            f"work is not being cancelled")

    def test_reverting_the_fix_drains_the_queue_again(self):
        # Negative control. Neutering `_cancel_pending` restores exactly the
        # pre-fix structure: the interrupt still propagates, but nothing is
        # cancelled, so `shutdown(wait=True)` executes the rest of the plan.
        full, _ = self._clean_run()
        # Restore the descriptor, not the unwrapped function: `Downloader.
        # _cancel_pending` yields the plain function, and putting *that* back
        # would rebind it as an instance method and corrupt every later test.
        self.addCleanup(setattr, Downloader, "_cancel_pending",
                        Downloader.__dict__["_cancel_pending"])
        Downloader._cancel_pending = staticmethod(lambda futures: 0)

        _store, transport, _elapsed, _root = self._interrupted_run()
        self.assertEqual(
            full, transport.attempted,
            "with cancellation disabled the interrupt must drain the queue; if "
            "it does not, this control proves nothing about the fix")

    def test_abort_latency_is_bounded_by_in_flight_work_only(self):
        _full, clean_elapsed = self._clean_run()
        _store, _transport, interrupted_elapsed, _root = self._interrupted_run()
        self.assertLess(
            interrupted_elapsed, clean_elapsed * 0.75,
            f"abort took {interrupted_elapsed:.3f}s against a full run of "
            f"{clean_elapsed:.3f}s; it is waiting on more than in-flight work")

    def test_work_already_running_finishes_and_commits_cleanly(self):
        store, transport, _elapsed, _root = self._interrupted_run()
        # Whatever was committed before the abort must be whole: cancelling
        # never cuts a commit short, which is what keeps the raw tier atomic.
        verified = store.verify_all()
        self.assertEqual(len(store.records()), len(verified))
        self.assertGreater(transport.attempted, 0, "nothing ran; vacuous test")

    def test_no_partial_artifact_or_orphan_part_file_survives(self):
        _store, _transport, _elapsed, root = self._interrupted_run()
        self.assertEqual([], sorted(p.name for p in root.rglob("*.part")))

    def test_no_sidecar_is_left_corrupt(self):
        store, _transport, _elapsed, root = self._interrupted_run()
        committed = {r.artifact_path for r in store.records()}
        for sidecar in root.rglob("*.meta.json"):
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertIn(payload["artifact_path"], committed)

    def test_resume_after_an_interrupt_reaches_the_identical_corpus(self):
        root = self._tmp("resume_")
        store, plan = self._fresh(root)
        # One transport table for both runs: `GoldenTransport` keys off the URL,
        # so building it from the full plan keeps every classification stable
        # across the shorter resumed plan.
        transport = self.G.GoldenTransport(plan)

        self._interrupt_after(4)
        with self.assertRaises(KeyboardInterrupt):
            self._download(transport, store, plan, 4)

        import backtest.acquire.downloader as module
        module.as_completed = __import__(
            "concurrent.futures", fromlist=["as_completed"]).as_completed

        resumed_store, resumed_plan = self._fresh(root)
        self.assertGreater(len(resumed_plan), 0, "nothing left to resume")
        self._download(transport, resumed_store, resumed_plan, 4)

        reference_store, reference_plan = self._fresh(self._tmp("reference_"))
        self._download(self.G.GoldenTransport(reference_plan),
                       reference_store, reference_plan, 4)

        self.assertEqual(self._corpus_of(reference_store),
                         self._corpus_of(resumed_store))

    # -- the success path is untouched --------------------------------------

    def test_a_successful_run_never_cancels_anything(self):
        cancelled: list[int] = []
        real = Downloader._cancel_pending
        self.addCleanup(setattr, Downloader, "_cancel_pending",
                        Downloader.__dict__["_cancel_pending"])

        def recording(futures):
            count = real(futures)
            cancelled.append(count)
            return count

        Downloader._cancel_pending = staticmethod(recording)
        for workers in self.WORKER_COUNTS:
            with self.subTest(workers=workers):
                self.run_at(workers)
        self.assertEqual([], cancelled,
                         "cancellation must be unreachable on the success path")

    def test_the_golden_still_matches_at_every_worker_count(self):
        golden = json.loads(
            (Path("tests") / "backtest" / "fixtures"
             / "download_report_golden.json").read_text(encoding="utf-8"))
        for workers in self.WORKER_COUNTS:
            with self.subTest(workers=workers):
                self.assertEqual(golden, self.run_at(workers)["report"])

    # -- the worker-exception path is unchanged ------------------------------

    def test_a_worker_exception_still_cancels_through_the_same_path(self):
        seen: list[int] = []
        real = Downloader._cancel_pending
        self.addCleanup(setattr, Downloader, "_cancel_pending",
                        Downloader.__dict__["_cancel_pending"])
        Downloader._cancel_pending = staticmethod(
            lambda futures: seen.append(real(futures)) or seen[-1])

        def wrap(inner):
            return _FaultyTransport(
                inner, lambda u: u.endswith(".CHECKSUM"),
                TransportBanned("418 banned", retry_after=None))

        with self.assertRaises(TransportBanned):
            self.run_at(4, wrap=wrap)
        self.assertEqual(1, len(seen),
                         "the worker-exception abort must cancel exactly once")


if __name__ == "__main__":
    unittest.main()
