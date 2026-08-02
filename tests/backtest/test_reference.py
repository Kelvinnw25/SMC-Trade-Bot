"""M12 — the reference tier: instrument metadata and funding history.

Hermetic. The transport is faked; the *formats* are real — genuine Binance
funding CSVs inside genuine ZIPs, and genuine `exchangeInfo` shapes with nested
filter arrays.

The property this milestone lives or dies by is **isolation**: the reference tier
must not touch the kline repository's identity, because two runs with different
`data_snapshot_id` are never comparable (C-6.10) and changing it would retroactively
invalidate every replay run ever produced. That is tested first and directly.
"""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.acquire.storage import RawStore, sha256_hex
from backtest.acquire.transport import TransportError, TransportNotFound
from backtest.reference import (
    FUNDING_DTYPE, INSTRUMENT_FIELDS, OBSERVATIONS_NAME, REFERENCE_MANIFEST_NAME,
    ReferenceError, acquire_funding, build_reference, capture_instruments,
    instrument_history, load_reference, parse_funding_csv, read_funding,
    reference_root, verify_reference,
)

BULK = "https://data.binance.vision/data/futures/um"
EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
HOUR_MS = 3_600_000


# ---------------------------------------------------------------------------
# Fixtures — real formats, fake network
# ---------------------------------------------------------------------------

def funding_csv(start_ms: int, count: int, *, rate: float = 0.0001) -> str:
    lines = ["calc_time,funding_interval_hours,last_funding_rate"]
    for index in range(count):
        lines.append(f"{start_ms + index * 8 * HOUR_MS},8,{rate:.8f}")
    return "\n".join(lines) + "\n"


def zip_bytes(member: str, text: str) -> bytes:
    info = zipfile.ZipInfo(member, date_time=(2025, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, text)
    return buffer.getvalue()


def exchange_info(symbols=("BTCUSDT", "ETHUSDT"), *, tick="0.10",
                  status="TRADING") -> bytes:
    payload = {"timezone": "UTC", "symbols": [
        {
            "symbol": name, "status": status, "contractType": "PERPETUAL",
            "baseAsset": name[:-4], "quoteAsset": "USDT",
            "onboardDate": 1568102400000, "deliveryDate": 4133404800000,
            "pricePrecision": 2, "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": tick},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }
        for name in symbols
    ]}
    return json.dumps(payload, sort_keys=True).encode()


class FakeTransport:
    def __init__(self, payloads=None):
        self.payloads = dict(payloads or {})
        self.calls: list[str] = []

    def add_funding(self, symbol, year, month, *, count=93, rate=0.0001):
        period = f"{year:04d}-{month:02d}"
        name = f"{symbol}-fundingRate-{period}"
        url = f"{BULK}/monthly/fundingRate/{symbol}/{name}.zip"
        start = int(date(year, month, 1).toordinal() - date(1970, 1, 1).toordinal())
        data = zip_bytes(name + ".csv",
                         funding_csv(start * 86_400_000, count, rate=rate))
        self.payloads[url] = data
        self.payloads[url + ".CHECKSUM"] = f"{sha256_hex(data)}  {name}.zip\n".encode()
        return data

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        try:
            return self.payloads[url]
        except KeyError:
            raise TransportNotFound(f"404 {url}") from None


class ReferenceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def transport_with_funding(self, symbols=("BTCUSDT",),
                               months=((2025, 1), (2025, 2))):
        transport = FakeTransport()
        for symbol in symbols:
            for year, month in months:
                transport.add_funding(symbol, year, month)
        return transport


# ==========================================================================
# RISK R-A — the reference tier must not touch the kline repository
# ==========================================================================

class TestIsolation(ReferenceCase):

    def test_the_tier_lives_beside_the_canonical_klines_not_inside(self):
        transport = self.transport_with_funding()
        acquire_funding(self.root, transport=transport, symbols=["BTCUSDT"],
                        start=date(2025, 1, 1), end=date(2025, 3, 10))
        build_reference(self.root)
        self.assertTrue((self.root / "reference").is_dir())
        self.assertFalse((self.root / "canonical").exists())
        self.assertFalse((self.root / "raw").exists())

    def test_the_reference_identity_is_not_the_data_snapshot_id(self):
        """C-6.10: changing data_snapshot_id invalidates every existing run."""
        transport = self.transport_with_funding()
        acquire_funding(self.root, transport=transport, symbols=["BTCUSDT"],
                        start=date(2025, 1, 1), end=date(2025, 3, 10))
        report = build_reference(self.root)
        manifest = load_reference(self.root)
        self.assertEqual(manifest["reference_snapshot_id"],
                         report.reference_snapshot_id)
        self.assertNotIn("data_snapshot_id", manifest)

    def test_the_manifest_declares_that_nothing_consumes_it(self):
        build_reference(self.root)
        manifest = load_reference(self.root)
        self.assertEqual(manifest["consumed_by"], [])
        self.assertIn("identical decisions whether this tier exists or not",
                      manifest["note"])

    def test_nothing_in_the_runtime_or_engines_imports_reference(self):
        """Read-only means unreachable, not merely unused."""
        from tests.backtest.test_historical_adapter import imported_modules
        allowed = {"cli.py", "__main__.py", "reference.py"}
        for tree in ("v2", "backtest"):
            for path in Path(tree).rglob("*.py"):
                if path.name in allowed:
                    continue
                for module in imported_modules(path):
                    self.assertNotIn("backtest.reference", module,
                                     f"{path} imports {module}")

    def test_the_outcome_engine_still_models_no_funding(self):
        """M12 captures funding; it does not price it. That is Priority 1."""
        from backtest.outcome import OutcomeConfig
        self.assertEqual(OutcomeConfig().funding_rate_model, "none")


# ==========================================================================
# Instrument metadata — the irreplaceable half
# ==========================================================================

class TestInstrumentCapture(ReferenceCase):

    def transport(self, **kw):
        return FakeTransport({EXCHANGE_INFO: exchange_info(**kw)})

    def test_a_capture_stores_the_payload_and_logs_an_observation(self):
        observation = capture_instruments(
            self.root, transport=self.transport(), now="2026-08-01T00:00:00Z")
        self.assertEqual(observation.symbol_count, 2)
        self.assertTrue(observation.changed)
        store = RawStore(reference_root(self.root))
        self.assertTrue(store.is_committed(observation.artifact_path))
        lines = (reference_root(self.root) / OBSERVATIONS_NAME
                 ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_the_artifact_path_is_the_content_digest(self):
        """So an unchanged exchange costs nothing to re-capture."""
        observation = capture_instruments(self.root, transport=self.transport())
        self.assertIn(observation.sha256[:16], observation.artifact_path)

    def test_recapturing_an_unchanged_exchange_adds_no_bytes(self):
        transport = self.transport()
        first = capture_instruments(self.root, transport=transport,
                                    now="2026-08-01T00:00:00Z")
        second = capture_instruments(self.root, transport=transport,
                                     now="2026-08-02T00:00:00Z")
        self.assertEqual(first.sha256, second.sha256)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        artifacts = [r for r in RawStore(reference_root(self.root)).records()
                     if r.kind == "exchange_info_json"]
        self.assertEqual(len(artifacts), 1)

    def test_both_observations_are_still_recorded(self):
        """Dedupe applies to bytes, never to the fact that we looked."""
        transport = self.transport()
        capture_instruments(self.root, transport=transport, now="2026-08-01T00:00:00Z")
        capture_instruments(self.root, transport=transport, now="2026-08-02T00:00:00Z")
        lines = (reference_root(self.root) / OBSERVATIONS_NAME
                 ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

    def test_a_changed_exchange_produces_a_second_artifact(self):
        capture_instruments(self.root, transport=self.transport(tick="0.10"),
                            now="2026-08-01T00:00:00Z")
        second = capture_instruments(self.root,
                                     transport=self.transport(tick="0.50"),
                                     now="2026-09-01T00:00:00Z")
        self.assertTrue(second.changed)
        artifacts = [r for r in RawStore(reference_root(self.root)).records()
                     if r.kind == "exchange_info_json"]
        self.assertEqual(len(artifacts), 2)

    def test_a_transport_failure_is_an_error_not_a_silent_gap(self):
        class Broken:
            def get(self, url):
                raise TransportError("down")
        with self.assertRaises(ReferenceError):
            capture_instruments(self.root, transport=Broken())

    def test_a_payload_of_the_wrong_shape_is_refused(self):
        with self.assertRaises(ReferenceError):
            capture_instruments(self.root,
                                transport=FakeTransport({EXCHANGE_INFO: b'{"x":1}'}))


class TestInstrumentHistory(ReferenceCase):

    def capture(self, stamp, **kw):
        return capture_instruments(
            self.root, transport=FakeTransport({EXCHANGE_INFO: exchange_info(**kw)}),
            now=stamp)

    def test_the_history_records_one_entry_per_change(self):
        self.capture("2026-08-01T00:00:00Z", tick="0.10")
        self.capture("2026-08-02T00:00:00Z", tick="0.10")
        self.capture("2026-09-01T00:00:00Z", tick="0.50")
        history = instrument_history(self.root)
        self.assertEqual(len(history["BTCUSDT"]), 2)
        self.assertEqual(history["BTCUSDT"][0]["tickSize"], "0.10")
        self.assertEqual(history["BTCUSDT"][1]["tickSize"], "0.50")
        self.assertEqual(history["BTCUSDT"][1]["observed_at"],
                         "2026-09-01T00:00:00Z")

    def test_the_projection_flattens_the_nested_filters(self):
        self.capture("2026-08-01T00:00:00Z")
        entry = instrument_history(self.root)["BTCUSDT"][0]
        for name in INSTRUMENT_FIELDS:
            self.assertIn(name, entry, name)
        self.assertEqual(entry["stepSize"], "0.001")
        self.assertEqual(entry["minNotional"], "5")

    def test_a_delisting_is_recorded_as_an_absence(self):
        """exchangeInfo never announces a delisting; it simply omits the symbol."""
        self.capture("2026-08-01T00:00:00Z", symbols=("BTCUSDT", "ETHUSDT"))
        self.capture("2026-09-01T00:00:00Z", symbols=("BTCUSDT",))
        history = instrument_history(self.root)
        self.assertEqual(history["ETHUSDT"][-1]["status"], "ABSENT")
        self.assertEqual(history["ETHUSDT"][-1]["observed_at"],
                         "2026-09-01T00:00:00Z")
        self.assertNotEqual(history["BTCUSDT"][-1].get("status"), "ABSENT")

    def test_a_status_change_is_recorded(self):
        self.capture("2026-08-01T00:00:00Z", status="TRADING")
        self.capture("2026-09-01T00:00:00Z", status="SETTLING")
        self.assertEqual(instrument_history(self.root)["BTCUSDT"][-1]["status"],
                         "SETTLING")

    def test_an_empty_tier_has_an_empty_history(self):
        self.assertEqual(instrument_history(self.root), {})


# ==========================================================================
# Funding history
# ==========================================================================

class TestFundingParse(unittest.TestCase):

    def test_a_real_archive_row_parses(self):
        rows = parse_funding_csv(
            "calc_time,funding_interval_hours,last_funding_rate\n"
            "1714521600000,8,0.00010000\n", source="x")
        self.assertEqual(rows.dtype, FUNDING_DTYPE)
        self.assertEqual(int(rows["calc_time"][0]), 1_714_521_600_000)
        self.assertEqual(int(rows["interval_hours"][0]), 8)
        self.assertAlmostEqual(float(rows["funding_rate"][0]), 0.0001)

    def test_a_negative_rate_is_preserved(self):
        rows = parse_funding_csv("1714521600000,8,-0.00042\n", source="x")
        self.assertAlmostEqual(float(rows["funding_rate"][0]), -0.00042)

    def test_a_malformed_row_names_its_line(self):
        with self.assertRaises(ReferenceError) as caught:
            parse_funding_csv("1714521600000,8\n", source="src")
        self.assertIn("line 1", str(caught.exception))

    def test_an_empty_file_yields_no_rows(self):
        self.assertEqual(len(parse_funding_csv("", source="x")), 0)


class TestFundingAcquisition(ReferenceCase):

    def test_whole_months_are_acquired_and_built(self):
        transport = self.transport_with_funding()
        acquired, present, missing = acquire_funding(
            self.root, transport=transport, symbols=["BTCUSDT"],
            start=date(2025, 1, 1), end=date(2025, 3, 10))
        self.assertEqual((acquired, present), (2, 0))
        self.assertEqual(missing, ())
        report = build_reference(self.root)
        self.assertEqual(report.symbols, ("BTCUSDT",))
        self.assertEqual(report.funding_rows, 186)

    def test_rerunning_acquires_nothing(self):
        transport = self.transport_with_funding()
        args = dict(symbols=["BTCUSDT"], start=date(2025, 1, 1),
                    end=date(2025, 3, 10))
        acquire_funding(self.root, transport=transport, **args)
        transport.calls.clear()
        acquired, present, _ = acquire_funding(self.root, transport=transport,
                                               **args)
        self.assertEqual((acquired, present), (0, 2))
        self.assertEqual(transport.calls, [])

    def test_an_unpublished_month_is_reported_not_raised(self):
        transport = self.transport_with_funding(months=((2025, 1),))
        acquired, _, missing = acquire_funding(
            self.root, transport=transport, symbols=["BTCUSDT"],
            start=date(2025, 1, 1), end=date(2025, 3, 10))
        self.assertEqual(acquired, 1)
        self.assertEqual(missing, ("BTCUSDT/2025-02",))

    def test_the_publication_lag_excludes_the_just_ended_month(self):
        transport = self.transport_with_funding(months=((2025, 1), (2025, 2)))
        acquired, _, _ = acquire_funding(
            self.root, transport=transport, symbols=["BTCUSDT"],
            start=date(2025, 1, 1), end=date(2025, 2, 28))
        self.assertEqual(acquired, 1)          # February is not yet published

    def test_a_published_checksum_is_verified(self):
        transport = self.transport_with_funding(months=((2025, 1),))
        url = f"{BULK}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-01.zip"
        transport.payloads[url + ".CHECKSUM"] = b"0" * 64 + b"  x.zip\n"
        with self.assertRaises(Exception):
            acquire_funding(self.root, transport=transport, symbols=["BTCUSDT"],
                            start=date(2025, 1, 1), end=date(2025, 3, 10))

    def test_the_series_is_strictly_increasing_and_readable(self):
        transport = self.transport_with_funding()
        acquire_funding(self.root, transport=transport, symbols=["BTCUSDT"],
                        start=date(2025, 1, 1), end=date(2025, 3, 10))
        build_reference(self.root)
        rows = read_funding(self.root, "BTCUSDT")
        self.assertEqual(len(rows), 186)
        self.assertTrue((rows["calc_time"][1:] > rows["calc_time"][:-1]).all())
        self.assertFalse(rows.flags.writeable)

    def test_reading_an_unacquired_symbol_fails_loudly(self):
        build_reference(self.root)
        with self.assertRaises(ReferenceError):
            read_funding(self.root, "NOSUCHUSDT")

    def test_multiple_symbols_are_kept_separate(self):
        transport = self.transport_with_funding(symbols=("BTCUSDT", "ETHUSDT"))
        acquire_funding(self.root, transport=transport,
                        symbols=["BTCUSDT", "ETHUSDT"],
                        start=date(2025, 1, 1), end=date(2025, 3, 10))
        report = build_reference(self.root)
        self.assertEqual(report.symbols, ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(len(read_funding(self.root, "ETHUSDT")), 186)


# ==========================================================================
# Determinism and integrity
# ==========================================================================

class TestDeterminism(ReferenceCase):

    def populate(self, root):
        transport = self.transport_with_funding()
        acquire_funding(root, transport=transport, symbols=["BTCUSDT"],
                        start=date(2025, 1, 1), end=date(2025, 3, 10))
        capture_instruments(root, transport=FakeTransport(
            {EXCHANGE_INFO: exchange_info()}), now="2026-08-01T00:00:00Z")
        return build_reference(root)

    def test_two_builds_produce_the_same_identity(self):
        first = self.populate(self.root / "a")
        second = self.populate(self.root / "b")
        self.assertEqual(first.reference_snapshot_id,
                         second.reference_snapshot_id)

    def test_the_stored_arrays_are_byte_identical(self):
        self.populate(self.root / "a")
        self.populate(self.root / "b")
        left = (self.root / "a" / "reference" / "funding" / "BTCUSDT.npy")
        right = (self.root / "b" / "reference" / "funding" / "BTCUSDT.npy")
        self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_the_manifest_carries_no_build_timestamp(self):
        self.populate(self.root)
        text = (reference_root(self.root) / REFERENCE_MANIFEST_NAME
                ).read_text(encoding="utf-8")
        for forbidden in ("built_at", "generated_at"):
            self.assertNotIn(forbidden, text)

    def test_rebuilding_is_idempotent(self):
        first = self.populate(self.root)
        second = build_reference(self.root)
        self.assertEqual(first.reference_snapshot_id,
                         second.reference_snapshot_id)

    def test_capture_time_does_not_enter_the_identity(self):
        """Only distinct instrument *states* count; looking again is free."""
        report = self.populate(self.root)
        capture_instruments(self.root, transport=FakeTransport(
            {EXCHANGE_INFO: exchange_info()}), now="2026-12-25T00:00:00Z")
        self.assertEqual(build_reference(self.root).reference_snapshot_id,
                         report.reference_snapshot_id)

    def test_a_new_instrument_state_changes_the_identity(self):
        report = self.populate(self.root)
        capture_instruments(self.root, transport=FakeTransport(
            {EXCHANGE_INFO: exchange_info(tick="0.50")}), now="2026-12-25T00:00:00Z")
        self.assertNotEqual(build_reference(self.root).reference_snapshot_id,
                            report.reference_snapshot_id)


class TestIntegrity(ReferenceCase):

    def populate(self):
        transport = self.transport_with_funding()
        acquire_funding(self.root, transport=transport, symbols=["BTCUSDT"],
                        start=date(2025, 1, 1), end=date(2025, 3, 10))
        capture_instruments(self.root, transport=FakeTransport(
            {EXCHANGE_INFO: exchange_info()}), now="2026-08-01T00:00:00Z")
        return build_reference(self.root)

    def test_a_clean_tier_verifies(self):
        self.populate()
        ok, problems = verify_reference(self.root)
        self.assertTrue(ok, problems)

    def test_a_corrupted_artifact_is_detected(self):
        self.populate()
        store = RawStore(reference_root(self.root))
        victim = [r for r in store.records()
                  if r.kind == "funding_monthly_zip"][0]
        path = store.path_for(victim.artifact_path)
        path.write_bytes(path.read_bytes() + b"rot")
        ok, problems = verify_reference(self.root)
        self.assertFalse(ok)
        self.assertTrue(any("raw tier" in p for p in problems), problems)

    def test_a_tampered_manifest_is_detected(self):
        self.populate()
        path = reference_root(self.root) / REFERENCE_MANIFEST_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reference_snapshot_id"] = "0" * 16
        path.write_text(json.dumps(payload), encoding="utf-8")
        ok, problems = verify_reference(self.root)
        self.assertFalse(ok)

    def test_verifying_an_absent_tier_fails_cleanly(self):
        ok, problems = verify_reference(self.root / "nowhere")
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
