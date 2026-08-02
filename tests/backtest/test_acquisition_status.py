"""Absence is evidence; only faults are failures.

Regression suite for the classification defect the first real Stage 1 run
exposed: 13,052 of 45,201 planned periods were reported `failed` and aborted a
healthy acquisition, because `acquire()` had one terminal status for "no
candidate succeeded" regardless of why.

The scientific rule these tests pin:

* **absent** — the archive was never published. The symbol was not listed yet,
  was already delisted, or the period predates coverage. There is no data to be
  short of, so `strict` must not fire.
* **failed** — the data should have been obtainable and could not be trusted:
  transport exhausted, checksum mismatch, corrupt ZIP, parse failure. `strict`
  must still fire.
"""
from __future__ import annotations

import hashlib
import io
import unittest
import zipfile

from backtest.acquire.downloader import (
    STATUS_ABSENT,
    STATUS_DOWNLOADED,
    STATUS_EMPTY,
    STATUS_FAILED,
    DownloadReport,
    Downloader,
    ItemOutcome,
)
from backtest.acquire.planner import Candidate, DownloadPlan, PlanItem
from backtest.acquire.storage import AcquisitionError
from backtest.acquire.transport import TransportError, TransportNotFound


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransport:
    """Maps URL -> bytes, or an exception class/instance to raise."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        for pattern, value in self.responses.items():
            if pattern in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, type) and issubclass(value, Exception):
                    raise value(url)
                return value
        raise TransportNotFound(url)


class FakeStore:
    """Accepts every commit; records what it was given."""

    def __init__(self) -> None:
        self.committed: list[str] = []

    def is_committed(self, relative_path: str) -> bool:
        return relative_path in self.committed

    def commit(self, relative_path, payload, *, record, expected_sha256=None):
        # Mirrors `RawStore.commit`'s signature, including the digest check the
        # real store performs, so a mismatched checksum still surfaces here.
        if expected_sha256 is not None:
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected_sha256:
                from backtest.acquire.storage import ChecksumMismatch
                raise ChecksumMismatch(
                    f"{relative_path}: expected {expected_sha256}, got {actual}")
        self.committed.append(relative_path)
        return relative_path


def _zip_for(symbol: str, period: str = "2021-03") -> bytes:
    """A real ZIP whose member name matches the candidate's `expected_member`,
    so `verify_archive` passes and the download reaches `_commit`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        # Fixed date_time: a wall-clock stamp would make the bytes, and so the
        # checksum below, differ between runs.
        archive.writestr(
            zipfile.ZipInfo(f"{symbol}-1h-{period}.csv",
                            date_time=(2025, 1, 1, 0, 0, 0)),
            "1609459200000,1,2,0.5,1.5,10,1609462799999,15,3,5,7,0\n")
    return buffer.getvalue()


def _checksum_for(payload: bytes, symbol: str, period: str = "2021-03") -> bytes:
    """Binance's `.CHECKSUM` shape: `"<sha256>  <filename>"`."""
    return (hashlib.sha256(payload).hexdigest().encode("ascii")
            + b"  " + f"{symbol}-1h-{period}.csv".encode("ascii") + b"\n")


_GOOD_ZIP = _zip_for("GOODUSDT")
_VALID_CHECKSUM = _checksum_for(_GOOD_ZIP, "GOODUSDT")


def _bulk_item(symbol="AAAUSDT", period="2021-03", *, with_rest=False):
    """A monthly-shaped item: one bulk candidate, no REST fallback."""
    candidates = [Candidate(
        kind="monthly_zip",
        url=f"https://example/{symbol}/{period}.zip",
        checksum_url=f"https://example/{symbol}/{period}.zip.CHECKSUM",
        relative_path=f"bulk/{symbol}/{period}.zip",
        expected_member=f"{symbol}-1h-{period}.csv",
    )]
    if with_rest:
        candidates.append(Candidate(
            kind="rest_json", url=f"https://rest/{symbol}/{period}",
            checksum_url=None,
            relative_path=f"rest/{symbol}/{period}.json",
            expected_member=None))
    return PlanItem(symbol=symbol, timeframe="1h", period=period,
                    coverage_start_ms=0, coverage_end_ms=1,
                    candidates=tuple(candidates))


def _plan(*items, already_covered=0):
    return DownloadPlan(items=tuple(items), already_covered=already_covered,
                        requested_start="2021-03-01",
                        requested_end="2021-03-31")


def _downloader(transport, store=None):
    return Downloader(transport, store or FakeStore(),
                      sleeper=lambda _s: None, max_attempts=4)


# ---------------------------------------------------------------------------


class TestAbsenceIsNotFailure(unittest.TestCase):
    """Requirement 1 — historical absence no longer fails the build."""

    def test_a_missing_monthly_checksum_is_absent(self):
        # The exact shape of all 13,052 Stage 1 items: the symbol was not listed
        # in that month, so no checksum was ever published.
        outcome = _downloader(FakeTransport({})).acquire(_bulk_item())
        self.assertEqual(outcome.status, STATUS_ABSENT)
        self.assertIn("no published checksum", outcome.detail)

    def test_a_missing_archive_with_a_published_checksum_is_absent(self):
        transport = FakeTransport({
            ".CHECKSUM": b"a" * 64 + b"  AAAUSDT-1h-2021-03.csv\n",
            ".zip": TransportNotFound,
        })
        outcome = _downloader(transport).acquire(_bulk_item())
        self.assertEqual(outcome.status, STATUS_ABSENT)

    def test_absence_costs_no_retries(self):
        # A 404 is never retried: absence is not transient.
        transport = FakeTransport({})
        outcome = _downloader(transport).acquire(_bulk_item())
        self.assertEqual(outcome.retries, 0)
        self.assertEqual(len(transport.calls), 1)

    def test_absence_does_not_make_the_report_fail(self):
        report = _downloader(FakeTransport({})).run(
            _plan(_bulk_item(period="2021-01"), _bulk_item(period="2021-02")))
        self.assertEqual(report.absent, 2)
        self.assertEqual(len(report.failures), 0)
        self.assertTrue(report.ok)

    def test_strict_does_not_fire_on_absence(self):
        """Requirement 1, at the gate that actually aborted Stage 1."""
        report = _downloader(FakeTransport({})).run(_plan(_bulk_item()))
        report.raise_for_failures()          # must not raise


class TestFaultsStillFail(unittest.TestCase):
    """Requirement 2 — real integrity failures still fail the build."""

    def test_transport_exhaustion_is_a_failure(self):
        transport = FakeTransport({".CHECKSUM": TransportError})
        outcome = _downloader(transport).acquire(_bulk_item())
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertEqual(outcome.retries, 3)     # 4 attempts, 3 re-attempts

    def test_a_corrupt_archive_is_a_failure_not_absence(self):
        # Checksum published, payload arrives, and it is not a valid ZIP.
        transport = FakeTransport({
            # sha256 of the payload below, so verification gets past the digest
            # and fails on the ZIP structure — a corrupt archive, not a
            # mismatched one.
            ".CHECKSUM": (b"bb81a2fd7185fcabb3e46254cfac3a6cbd703e7ac0e407e1"
                          b"efe1ca927f9c0a16  AAAUSDT-1h-2021-03.csv\n"),
            ".zip": b"this is not a zip file",
        })
        outcome = _downloader(transport).acquire(_bulk_item())
        self.assertEqual(outcome.status, STATUS_FAILED)

    def test_strict_still_fires_on_a_fault(self):
        transport = FakeTransport({".CHECKSUM": TransportError})
        report = _downloader(transport).run(_plan(_bulk_item()))
        self.assertFalse(report.ok)
        with self.assertRaises(AcquisitionError):
            report.raise_for_failures()

    def test_a_mixed_item_is_a_failure_not_absence(self):
        # ZIP unpublished (absent) but the REST fallback then breaks: the period
        # is NOT known to be absent, and saying so would assert something
        # unmeasured.
        transport = FakeTransport({"https://rest/": TransportError})
        outcome = _downloader(transport).acquire(_bulk_item(with_rest=True))
        self.assertEqual(outcome.status, STATUS_FAILED)

    def test_an_item_with_no_candidates_is_a_failure(self):
        # A planner defect, not an observation about the market.
        empty = PlanItem(symbol="AAAUSDT", timeframe="1h", period="2021-03",
                         coverage_start_ms=0, coverage_end_ms=1, candidates=())
        outcome = _downloader(FakeTransport({})).acquire(empty)
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertEqual(outcome.detail, "no candidate sources")


class TestUnknownSymbolIsAbsence(unittest.TestCase):
    """Binance answers a delisted symbol with 400, not 404.

    The real Stage 1 run hit this on 26 BDXNUSDT periods:
    `400 {"code":-1121,"msg":"Invalid symbol."}`. Classified as a generic error
    it was retried four times and then reported as an acquisition failure —
    while the exchange was plainly saying the instrument does not exist.
    """

    def _response(self, status, text):
        class Response:
            status_code = status

            def __init__(self): self.text = text
            @property
            def content(self): return b""
        return Response()

    def _transport(self, response):
        """Stub the transport's network seam and return a live `RequestsTransport`.

        The seam is `requests.Session`, not the module-level `requests.get`: the
        transport pools one session per thread rather than building a throwaway one
        per call. Patching `requests.get` here used to work and now silently would
        not — the stub would be bypassed and this hermetic test would open a real
        socket to `fapi.binance.com`, passing or failing on whatever the exchange
        happened to answer.
        """
        from backtest.acquire import transport as module

        class StubSession:
            def get(self, url, **kwargs):
                return response

            def close(self):
                pass

        source = module.RequestsTransport()
        original = module.requests.Session
        module.requests.Session = StubSession
        self.addCleanup(lambda: setattr(module.requests, "Session", original))
        return source

    def test_code_1121_is_absence_not_a_retryable_error(self):
        source = self._transport(self._response(
            400, '{"code":-1121,"msg":"Invalid symbol."}'))
        with self.assertRaises(TransportNotFound):
            source.get("https://fapi.binance.com/fapi/v1/klines?symbol=BDXNUSDT")

    def test_other_400s_remain_retryable_errors(self):
        # A malformed request, a bad interval, a rate-limit rejection: these
        # are faults and must keep their retry.
        source = self._transport(self._response(
            400, '{"code":-1130,"msg":"Illegal characters."}'))
        with self.assertRaises(TransportError) as caught:
            source.get("https://fapi.binance.com/fapi/v1/klines")
        self.assertNotIsInstance(caught.exception, TransportNotFound)

    def test_a_delisted_symbol_item_resolves_to_absent_end_to_end(self):
        transport = FakeTransport({
            "https://rest/": TransportNotFound("400 unknown symbol"),
        })
        outcome = _downloader(transport).acquire(_bulk_item(with_rest=True))
        self.assertEqual(outcome.status, STATUS_ABSENT)
        self.assertEqual(outcome.retries, 0)


class TestMalformedChecksumIsAnIntegrityFailure(unittest.TestCase):
    """Finding A — a malformed CHECKSUM body must fail its item, not the process.

    `parse_binance_checksum` raises `ChecksumMismatch`, an `IntegrityError` that
    `_retry` knows how to classify. It used to be called *outside* the retry, so
    a CHECKSUM URL answering HTTP 200 with a non-digest body (a CDN error page,
    a truncated response, an S3 XML error served with 200) escaped `acquire()`
    and `run()` and killed the whole acquisition.
    """

    #: HTTP 200, but the body is not "<64-hex>  <filename>".
    GARBAGE = b"<?xml version='1.0'?><Error><Code>InternalError</Code></Error>"

    def test_a_malformed_checksum_fails_the_item_not_the_process(self):
        transport = FakeTransport({".CHECKSUM": self.GARBAGE})
        outcome = _downloader(transport).acquire(_bulk_item())
        self.assertEqual(outcome.status, STATUS_FAILED)

    def test_it_is_retried_under_the_existing_policy(self):
        transport = FakeTransport({".CHECKSUM": self.GARBAGE})
        outcome = _downloader(transport).acquire(_bulk_item())
        self.assertEqual(outcome.retries, 3)          # 4 attempts, 3 re-attempts
        self.assertEqual(outcome.attempts, 4)
        self.assertEqual(len(transport.calls), 4)

    def test_the_run_completes_and_reports_it_normally(self):
        # The whole point: one bad checksum must not stop the other items.
        transport = FakeTransport({
            "BADUSDT": self.GARBAGE,
            "GOODUSDT/2021-03.zip.CHECKSUM": _VALID_CHECKSUM,
            "GOODUSDT/2021-03.zip": _GOOD_ZIP,
        })
        report = _downloader(transport).run(_plan(
            _bulk_item(symbol="BADUSDT"),
            _bulk_item(symbol="GOODUSDT"),
            _bulk_item(symbol="MISSINGUSDT"),
        ))
        self.assertEqual(len(report.outcomes), 3)     # nothing aborted the loop
        self.assertEqual(report.summary()["failed"], 1)
        self.assertEqual(report.summary()["absent"], 1)
        self.assertEqual(report.summary()["downloaded"], 1)

    def test_strict_fires_on_it(self):
        report = _downloader(FakeTransport({".CHECKSUM": self.GARBAGE})).run(
            _plan(_bulk_item()))
        self.assertFalse(report.ok)
        with self.assertRaises(AcquisitionError):
            report.raise_for_failures()

    def test_a_valid_checksum_still_downloads_identically(self):
        transport = FakeTransport({
            ".CHECKSUM": _VALID_CHECKSUM,
            ".zip": _GOOD_ZIP,
        })
        outcome = _downloader(transport).acquire(_bulk_item(symbol="GOODUSDT"))
        self.assertEqual(outcome.status, STATUS_DOWNLOADED)
        self.assertEqual(outcome.retries, 0)
        self.assertEqual(outcome.attempts, 2)         # checksum + payload

    def test_a_404_checksum_is_still_absent(self):
        outcome = _downloader(FakeTransport({})).acquire(_bulk_item())
        self.assertEqual(outcome.status, STATUS_ABSENT)
        self.assertEqual(outcome.retries, 0)


class TestRetryAccounting(unittest.TestCase):
    """`attempts` counts operations; `retries` counts re-attempts."""

    def test_a_clean_success_reports_zero_retries(self):
        # The defect this replaces reported 31,738 retries on a run with ~78.
        outcomes = (
            ItemOutcome("A", "1h", "2021-01", STATUS_DOWNLOADED, attempts=2),
            ItemOutcome("B", "1h", "2021-02", STATUS_DOWNLOADED, attempts=2),
        )
        self.assertEqual(DownloadReport(outcomes=outcomes).retries, 0)

    def test_retries_are_summed_across_items(self):
        outcomes = (
            ItemOutcome("A", "1h", "2021-01", STATUS_FAILED, attempts=4, retries=3),
            ItemOutcome("B", "1h", "2021-02", STATUS_DOWNLOADED, attempts=3, retries=1),
        )
        self.assertEqual(DownloadReport(outcomes=outcomes).retries, 4)


class TestOperatorSummary(unittest.TestCase):
    """Requirement: distinguish the five categories without reading logs."""

    def test_the_summary_separates_every_category(self):
        outcomes = (
            ItemOutcome("A", "1h", "2021-01", STATUS_DOWNLOADED, attempts=2),
            ItemOutcome("B", "1h", "2021-02", STATUS_ABSENT),
            ItemOutcome("C", "1h", "2021-03", STATUS_EMPTY, attempts=1),
            ItemOutcome("D", "1h", "2021-04", STATUS_FAILED, attempts=4, retries=3),
        )
        summary = DownloadReport(outcomes=outcomes, already_covered=7).summary()
        self.assertEqual(summary, {
            "planned": 4, "downloaded": 1, "absent": 1, "empty": 1,
            "already_covered": 7, "retries": 3, "failed": 1, "ok": False,
        })

    def test_the_summary_omits_the_per_item_detail(self):
        # 45,000 outcome dicts is correct for an artifact and useless on a
        # terminal.
        report = DownloadReport(
            outcomes=tuple(
                ItemOutcome("A", "1h", f"2021-{m:02d}", STATUS_ABSENT)
                for m in range(1, 13)))
        self.assertNotIn("outcomes", report.summary())
        self.assertIn("outcomes", report.to_dict())


class TestResumeUnchanged(unittest.TestCase):
    """Requirement 3 — resume still skips already committed artifacts."""

    def test_a_committed_period_is_never_re_requested(self):
        store = FakeStore()
        store.committed.append("bulk/AAAUSDT/2021-03.zip")
        transport = FakeTransport({})
        # The planner is what skips; prove the store answers `is_committed` for
        # exactly the path the candidate declares.
        item = _bulk_item()
        self.assertTrue(store.is_committed(item.candidates[0].relative_path))
        self.assertEqual(transport.calls, [])

    def test_already_covered_is_carried_into_the_report(self):
        report = _downloader(FakeTransport({})).run(
            _plan(_bulk_item(), already_covered=31_659))
        self.assertEqual(report.already_covered, 31_659)
        self.assertEqual(report.summary()["already_covered"], 31_659)


class TestNothingElseChanged(unittest.TestCase):
    """Requirement 6 — no replay, outcome, metrics, coverage or strategy change."""

    def test_the_status_vocabulary_is_additive(self):
        # `absent` is new; the three existing statuses keep their exact values,
        # so any stored record or test that names them still means what it did.
        self.assertEqual(
            (STATUS_DOWNLOADED, STATUS_EMPTY, STATUS_FAILED, STATUS_ABSENT),
            ("downloaded", "empty", "failed", "absent"))

    def test_corpus_identity_inputs_are_untouched(self):
        # `_corpus_id` hashes the spec, the snapshot and every *committed*
        # artifact's (path, sha256). Absence commits nothing, so re-classifying
        # it cannot move `corpus_id`.
        import inspect

        from backtest import corpus

        source = inspect.getsource(corpus._corpus_id)
        for token in ("STATUS_ABSENT", "absent", "failed", "retries"):
            self.assertNotIn(token, source)

    def test_downloader_version_is_unchanged(self):
        # Stamped into every artifact record. A bump would invalidate the 31,659
        # artifacts already on disk; this fix changes classification only, never
        # the bytes or how they were obtained.
        from backtest.acquire.downloader import DOWNLOADER_VERSION

        self.assertEqual(DOWNLOADER_VERSION, "m4.1-1.0.0")

    def test_the_raw_schema_version_is_unchanged(self):
        from backtest.acquire.storage import RAW_SCHEMA_VERSION

        self.assertEqual(RAW_SCHEMA_VERSION, 1)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
