"""Tests for the Canonical Historical Dataset Builder (Milestone M4.2).

Hermetic: no network, no real Binance, no clock reads that matter. Every raw
artifact is synthesised in a temporary directory through the real `RawStore`, so
what is exercised is the actual commit/verify path the downloader writes.

The column conventions asserted here were measured against production archives
during the M4.1 validation gate (1d/1h/5m for 2020-01 and 2026-03, plus a live
REST response): twelve columns in both eras, `close_time == open + interval - 1`,
open times interval-aligned, header present from ~2023-06.
"""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from backtest.acquire.storage import (
    ArtifactRecord, ChecksumMismatch, RAW_SCHEMA_VERSION, RawStore, sha256_hex,
)
from backtest.canonical import (
    BUILDER_VERSION, BuildError, CSV_HEADER_FIELDS, CanonicalBuilder,
    ConflictingCandles, CoverageMismatch, DuplicateCandle, GAP_INVENTORY_NAME,
    InconsistentCloseTime, InvalidValue, MalformedRow, MisalignedCandle,
    OhlcInconsistent, SourceUnusable, UnknownFormat, UnorderedCandles,
    drop_unfinished, has_header, observation_ms, parse_kline_csv,
    parse_kline_json, validate_dataset,
)
from backtest.data import (
    INTERVAL_MS, KLINE_DTYPE, KlineRepository, MANIFEST_NAME,
    RepositoryNotFound, compute_snapshot_id, dataset_path,
)

HOUR = INTERVAL_MS["1h"]
DAY = INTERVAL_MS["1d"]

#: 2026-03-01T00:00:00Z — a real UTC day boundary, so alignment is meaningful.
BASE = 1_772_323_200_000


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def kline(open_ms: int, interval: int = HOUR, **over) -> tuple:
    """One kline row in Binance CSV column order."""
    row = {
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "v": 5.0, "qv": 500.0, "nt": 42, "tb": 2.0, "tq": 200.0, "i": 0,
        "ct": open_ms + interval - 1,
    }
    row.update(over)
    return (open_ms, row["open"], row["high"], row["low"], row["close"],
            row["v"], row["ct"], row["qv"], row["nt"], row["tb"], row["tq"],
            row["i"])


def csv_text(rows, *, header: bool) -> str:
    lines = [",".join(CSV_HEADER_FIELDS)] if header else []
    lines.extend(",".join(str(value) for value in row) for row in rows)
    return "\n".join(lines) + "\n"


def zip_bytes(member: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


def commit(store: RawStore, relative: str, payload: bytes, *, kind: str,
           symbol: str, timeframe: str, start_ms: int, end_ms: int,
           downloaded_at: str = "2026-07-29T00:00:00Z") -> ArtifactRecord:
    record = ArtifactRecord(
        schema_version=RAW_SCHEMA_VERSION, downloader_version="test",
        downloaded_at=downloaded_at, source_url=f"test://{relative}", kind=kind,
        symbol=symbol, timeframe=timeframe, coverage_start_ms=start_ms,
        coverage_end_ms=end_ms, sha256=sha256_hex(payload),
        size_bytes=len(payload), checksum_source="computed",
        artifact_path=relative,
    )
    store.commit(relative, payload, record=record)
    return record


def commit_daily_zip(store, symbol, timeframe, day_ms, rows, *, header=True,
                     downloaded_at="2026-07-29T00:00:00Z"):
    """A daily bulk archive, named exactly as the downloader names one."""
    stamp = _day_name(day_ms)
    name = f"{symbol}-{timeframe}-{stamp}"
    relative = f"bulk/um/daily/klines/{symbol}/{timeframe}/{name}.zip"
    payload = zip_bytes(name + ".csv", csv_text(rows, header=header))
    return commit(store, relative, payload, kind="daily_zip", symbol=symbol,
                  timeframe=timeframe, start_ms=day_ms, end_ms=day_ms + DAY,
                  downloaded_at=downloaded_at)


def commit_rest(store, symbol, timeframe, day_ms, rows, *,
                downloaded_at="2026-07-29T00:00:00Z"):
    relative = f"rest/{symbol}/{timeframe}/{day_ms}-{day_ms + DAY}.json"
    payload = json.dumps([list(row) for row in rows]).encode("utf-8")
    return commit(store, relative, payload, kind="rest_json", symbol=symbol,
                  timeframe=timeframe, start_ms=day_ms, end_ms=day_ms + DAY,
                  downloaded_at=downloaded_at)


def _day_name(day_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(day_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d")


def day_rows(day_ms: int, count: int = 24, **over) -> list[tuple]:
    return [kline(day_ms + index * HOUR, **over) for index in range(count)]


class BuilderCase(unittest.TestCase):
    """A temporary raw store plus a temporary canonical root."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RawStore(root / "raw")
        self.out = root / "canonical"
        self.addCleanup(self._tmp.cleanup)

    def build(self, **kwargs):
        builder = CanonicalBuilder(self.store)
        return builder.build(self.out, **kwargs)


# ==========================================================================
# TASK 1 — the CSV parser
# ==========================================================================

class TestCsvParsing(unittest.TestCase):

    def test_headerless_csv_parses_every_row(self):
        rows = day_rows(BASE, 24)
        parsed = parse_kline_csv(csv_text(rows, header=False), source="old")
        self.assertEqual(len(parsed), 24)
        self.assertEqual(int(parsed["timestamp"][0]), BASE)
        self.assertEqual(int(parsed["timestamp"][-1]), BASE + 23 * HOUR)

    def test_headed_csv_parses_every_row_and_keeps_none_of_the_header(self):
        rows = day_rows(BASE, 24)
        parsed = parse_kline_csv(csv_text(rows, header=True), source="new")
        self.assertEqual(len(parsed), 24)
        self.assertEqual(int(parsed["timestamp"][0]), BASE)

    def test_both_eras_of_the_same_data_parse_identically(self):
        """The header is presentation. The rows must not differ because of it."""
        rows = day_rows(BASE, 24)
        old = parse_kline_csv(csv_text(rows, header=False), source="old")
        new = parse_kline_csv(csv_text(rows, header=True), source="new")
        self.assertTrue(np.array_equal(old, new))

    def test_every_column_is_carried_through(self):
        row = kline(BASE, open=1.5, high=9.5, low=0.5, close=2.5, v=3.5,
                    qv=4.5, nt=7, tb=1.25, tq=2.75, i=1)
        parsed = parse_kline_csv(csv_text([row], header=True), source="s")[0]
        self.assertEqual(int(parsed["timestamp"]), BASE)
        self.assertEqual(float(parsed["open"]), 1.5)
        self.assertEqual(float(parsed["high"]), 9.5)
        self.assertEqual(float(parsed["low"]), 0.5)
        self.assertEqual(float(parsed["close"]), 2.5)
        self.assertEqual(float(parsed["v"]), 3.5)
        self.assertEqual(int(parsed["ct"]), BASE + HOUR - 1)
        self.assertEqual(float(parsed["qv"]), 4.5)
        self.assertEqual(int(parsed["nt"]), 7)
        self.assertEqual(float(parsed["tb"]), 1.25)
        self.assertEqual(float(parsed["tq"]), 2.75)
        self.assertEqual(float(parsed["i"]), 1.0)

    def test_dtype_is_the_repository_dtype(self):
        parsed = parse_kline_csv(csv_text(day_rows(BASE, 2), header=True), source="s")
        self.assertEqual(parsed.dtype, KLINE_DTYPE)

    def test_empty_text_is_zero_rows_not_an_error(self):
        self.assertEqual(len(parse_kline_csv("", source="s")), 0)

    def test_header_detection_reports_data(self):
        self.assertFalse(has_header(",".join(str(v) for v in kline(BASE)),
                                    source="s"))

    def test_header_detection_reports_header(self):
        self.assertTrue(has_header(",".join(CSV_HEADER_FIELDS), source="s"))

    def test_an_unknown_first_line_is_refused_never_guessed(self):
        line = ",".join(["surprise"] + list(CSV_HEADER_FIELDS[1:]))
        with self.assertRaises(UnknownFormat) as caught:
            has_header(line, source="artifact.zip")
        self.assertIn("refusing to guess", str(caught.exception))

    def test_a_different_column_count_is_refused(self):
        with self.assertRaises(UnknownFormat):
            has_header("1,2,3", source="s")

    def test_a_short_row_fails_with_its_line_number(self):
        text = csv_text(day_rows(BASE, 3), header=True)
        broken = text.splitlines()
        broken[2] = "1,2,3"
        with self.assertRaises(MalformedRow) as caught:
            parse_kline_csv("\n".join(broken) + "\n", source="artifact.zip")
        self.assertIn("line 3", str(caught.exception))

    def test_a_non_numeric_field_names_its_column(self):
        text = csv_text([kline(BASE)], header=True).replace("100.0", "abc")
        with self.assertRaises(MalformedRow) as caught:
            parse_kline_csv(text, source="s")
        self.assertIn("open", str(caught.exception))

    def test_a_fractional_open_time_is_refused_not_truncated(self):
        row = list(kline(BASE))
        row[0] = "1772323200000.5"
        with self.assertRaises(MalformedRow):
            parse_kline_csv(csv_text([tuple(row)], header=True), source="s")

    def test_a_blank_line_is_never_silently_skipped(self):
        text = csv_text(day_rows(BASE, 2), header=True).replace("\n", "\n\n", 1)
        with self.assertRaises(MalformedRow) as caught:
            parse_kline_csv(text, source="s")
        self.assertIn("blank", str(caught.exception))

    def test_rows_are_never_reordered_by_the_parser(self):
        """Out-of-order input is preserved so validation can report it, rather
        than being silently sorted into looking correct."""
        rows = [kline(BASE + HOUR), kline(BASE)]
        parsed = parse_kline_csv(csv_text(rows, header=True), source="s")
        self.assertEqual(int(parsed["timestamp"][0]), BASE + HOUR)
        self.assertEqual(int(parsed["timestamp"][1]), BASE)


class TestJsonParsing(unittest.TestCase):

    def test_rest_rows_parse_with_binance_mixed_types(self):
        payload = json.dumps([[BASE, "100.0", "101.0", "99.0", "100.5", "5.0",
                               BASE + HOUR - 1, "500.0", 42, "2.0", "200.0",
                               "0"]]).encode()
        parsed = parse_kline_json(payload, source="rest")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(int(parsed["timestamp"][0]), BASE)
        self.assertEqual(float(parsed["qv"][0]), 500.0)
        self.assertEqual(int(parsed["nt"][0]), 42)

    def test_an_empty_array_is_zero_rows_not_an_error(self):
        parsed = parse_kline_json(b"[]", source="rest")
        self.assertEqual(len(parsed), 0)
        self.assertEqual(parsed.dtype, KLINE_DTYPE)

    def test_a_json_object_is_refused(self):
        with self.assertRaises(UnknownFormat):
            parse_kline_json(b'{"code": -1121}', source="rest")

    def test_malformed_json_is_refused(self):
        with self.assertRaises(BuildError):
            parse_kline_json(b"not json", source="rest")

    def test_a_short_row_is_refused(self):
        with self.assertRaises(MalformedRow):
            parse_kline_json(b"[[1, 2, 3]]", source="rest")

    def test_a_scalar_element_is_refused(self):
        with self.assertRaises(MalformedRow):
            parse_kline_json(b"[5]", source="rest")


# ==========================================================================
# TASK 2 — candle validation
# ==========================================================================

class TestValidation(unittest.TestCase):

    def rows(self, tuples):
        return np.array(tuples, dtype=KLINE_DTYPE)

    def validate(self, tuples, timeframe="1h"):
        return validate_dataset(self.rows(tuples), symbol="BTCUSDT",
                                timeframe=timeframe)

    def test_a_clean_series_validates(self):
        report = self.validate(day_rows(BASE, 24))
        self.assertEqual(report.rows, 24)
        self.assertEqual(report.first_open_ms, BASE)
        self.assertEqual(report.last_open_ms, BASE + 23 * HOUR)
        self.assertEqual(report.gaps, ())

    def test_decreasing_open_time_is_an_error(self):
        with self.assertRaises(UnorderedCandles):
            self.validate([kline(BASE + HOUR), kline(BASE)])

    def test_a_duplicate_open_time_is_an_error(self):
        with self.assertRaises(DuplicateCandle):
            self.validate([kline(BASE), kline(BASE)])

    def test_a_misaligned_open_time_is_an_error(self):
        with self.assertRaises(MisalignedCandle):
            self.validate([kline(BASE + 60_000)])

    def test_a_wrong_close_time_is_an_error(self):
        with self.assertRaises(InconsistentCloseTime):
            self.validate([kline(BASE, ct=BASE + HOUR)])

    def test_an_hourly_row_filed_as_daily_is_caught(self):
        """The close-time check is what notices a timeframe mix-up."""
        with self.assertRaises((InconsistentCloseTime, MisalignedCandle)):
            self.validate([kline(BASE, HOUR)], timeframe="1d")

    def test_nan_is_an_error(self):
        with self.assertRaises(InvalidValue):
            self.validate([kline(BASE, close=float("nan"))])

    def test_infinity_is_an_error(self):
        with self.assertRaises(InvalidValue):
            self.validate([kline(BASE, high=float("inf"))])

    def test_a_zero_price_is_an_error(self):
        with self.assertRaises(InvalidValue):
            self.validate([kline(BASE, low=0.0, open=0.5, close=0.5)])

    def test_a_negative_volume_is_an_error(self):
        with self.assertRaises(InvalidValue):
            self.validate([kline(BASE, v=-1.0)])

    def test_a_zero_volume_candle_is_legitimate(self):
        """Illiquid symbols really do print candles with no trades. Rejecting
        them would delete history."""
        report = self.validate([kline(BASE, v=0.0, qv=0.0, nt=0, tb=0.0, tq=0.0,
                                      open=100.0, high=100.0, low=100.0,
                                      close=100.0)])
        self.assertEqual(report.rows, 1)

    def test_high_below_low_is_an_error(self):
        with self.assertRaises(OhlcInconsistent):
            self.validate([kline(BASE, high=98.0, low=99.0, open=98.5,
                                 close=98.5)])

    def test_high_below_close_is_an_error(self):
        with self.assertRaises(OhlcInconsistent):
            self.validate([kline(BASE, high=100.0, close=101.0)])

    def test_low_above_open_is_an_error(self):
        with self.assertRaises(OhlcInconsistent):
            self.validate([kline(BASE, low=100.5, open=100.0)])

    def test_a_gap_is_inventoried_not_repaired(self):
        rows = [kline(BASE), kline(BASE + HOUR), kline(BASE + 5 * HOUR)]
        report = self.validate(rows)
        self.assertEqual(report.rows, 3)                 # nothing was filled
        self.assertEqual(len(report.gaps), 1)
        gap = report.gaps[0]
        self.assertEqual(gap.start_ms, BASE + 2 * HOUR)
        self.assertEqual(gap.end_ms, BASE + 4 * HOUR)
        self.assertEqual(gap.missing, 3)
        self.assertEqual(report.missing_candles, 3)

    def test_several_gaps_are_all_recorded_in_order(self):
        rows = [kline(BASE), kline(BASE + 3 * HOUR), kline(BASE + 10 * HOUR)]
        report = self.validate(rows)
        self.assertEqual([g.missing for g in report.gaps], [2, 6])

    def test_an_empty_series_validates_to_nothing(self):
        report = validate_dataset(np.empty(0, dtype=KLINE_DTYPE),
                                  symbol="X", timeframe="1h")
        self.assertEqual(report.rows, 0)
        self.assertEqual(report.gaps, ())


# ==========================================================================
# TASK 3 — the forming-candle policy
# ==========================================================================

class TestFormingCandle(unittest.TestCase):

    def rows(self, count):
        return np.array(day_rows(BASE, count), dtype=KLINE_DTYPE)

    def test_a_candle_still_open_at_observation_is_dropped(self):
        """The live case measured in the M4.1 gate: a REST call at 12:23 UTC
        returns the 12:00 candle, which closes at 12:59:59.999."""
        observed = BASE + 12 * HOUR + 23 * 60_000
        kept, dropped = drop_unfinished(self.rows(13), timeframe="1h",
                                        observed_ms=observed)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 12)
        self.assertEqual(int(kept["timestamp"][-1]), BASE + 11 * HOUR)

    def test_a_candle_that_closed_exactly_at_observation_is_kept(self):
        observed = BASE + 12 * HOUR          # the 11:00 candle closes here
        kept, dropped = drop_unfinished(self.rows(12), timeframe="1h",
                                        observed_ms=observed)
        self.assertEqual(dropped, 0)
        self.assertEqual(int(kept["timestamp"][-1]), BASE + 11 * HOUR)

    def test_one_millisecond_early_still_drops_the_candle(self):
        observed = BASE + 12 * HOUR - 1
        kept, dropped = drop_unfinished(self.rows(12), timeframe="1h",
                                        observed_ms=observed)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 11)

    def test_a_completed_archive_loses_nothing(self):
        kept, dropped = drop_unfinished(self.rows(24), timeframe="1h",
                                        observed_ms=BASE + 10 * DAY)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 24)

    def test_the_rule_uses_the_interval_not_the_stored_close_time(self):
        """`ct` is `open + interval - 1`. Selecting on it would keep a candle
        one millisecond before it closed (decision R-4)."""
        rows = np.array([kline(BASE)], dtype=KLINE_DTYPE)
        kept, dropped = drop_unfinished(rows, timeframe="1h",
                                        observed_ms=int(rows["ct"][0]))
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 0)

    def test_observation_time_comes_from_the_sidecar(self):
        record = ArtifactRecord(
            schema_version=RAW_SCHEMA_VERSION, downloader_version="t",
            downloaded_at="2026-03-01T00:00:00Z", source_url="", kind="rest_json",
            symbol="X", timeframe="1h", coverage_start_ms=0, coverage_end_ms=0,
            sha256="", size_bytes=0, checksum_source="computed", artifact_path="a",
        )
        self.assertEqual(observation_ms(record), BASE)

    def test_an_unreadable_observation_time_is_an_error(self):
        record = ArtifactRecord(
            schema_version=RAW_SCHEMA_VERSION, downloader_version="t",
            downloaded_at="whenever", source_url="", kind="rest_json", symbol="X",
            timeframe="1h", coverage_start_ms=0, coverage_end_ms=0, sha256="",
            size_bytes=0, checksum_source="computed", artifact_path="a",
        )
        with self.assertRaises(BuildError):
            observation_ms(record)

    def test_empty_rows_are_handled(self):
        kept, dropped = drop_unfinished(np.empty(0, dtype=KLINE_DTYPE),
                                        timeframe="1h", observed_ms=BASE)
        self.assertEqual((len(kept), dropped), (0, 0))


class TestFormingCandleThroughTheBuilder(BuilderCase):

    def test_an_unfinished_rest_candle_never_reaches_the_dataset(self):
        commit_rest(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 13),
                    downloaded_at="2026-03-01T12:23:05Z")
        report = self.build()
        dataset = report.datasets[0]
        self.assertEqual(dataset.rows, 12)
        self.assertEqual(dataset.dropped_unfinished, 1)
        self.assertEqual(dataset.last_open_ms, BASE + 11 * HOUR)

    def test_the_dropped_count_is_recorded_in_the_manifest(self):
        commit_rest(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 13),
                    downloaded_at="2026-03-01T12:23:05Z")
        self.build()
        manifest = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["datasets"]["BTCUSDT|1h"]["dropped_unfinished"], 1)


# ==========================================================================
# TASK 4 — canonical storage
# ==========================================================================

class TestBuild(BuilderCase):

    def seed_one_day(self, symbol="BTCUSDT", timeframe="1h", header=True):
        commit_daily_zip(self.store, symbol, timeframe, BASE,
                         day_rows(BASE, 24), header=header)

    def test_a_build_produces_the_repository_layout(self):
        self.seed_one_day()
        report = self.build()
        self.assertTrue((self.out / MANIFEST_NAME).is_file())
        self.assertTrue((self.out / GAP_INVENTORY_NAME).is_file())
        self.assertTrue(dataset_path(self.out, "BTCUSDT", "1h").is_file())
        self.assertEqual(report.total_rows, 24)

    def test_the_repository_reads_what_the_builder_wrote(self):
        self.seed_one_day()
        report = self.build()
        repo = KlineRepository(self.out)
        self.assertEqual(repo.data_snapshot_id, report.data_snapshot_id)
        self.assertEqual(repo.builder_version, BUILDER_VERSION)
        self.assertEqual(repo.symbols("1h"), ("BTCUSDT",))
        coverage = repo.coverage("BTCUSDT", "1h")
        self.assertEqual(coverage.rows, 24)
        self.assertEqual(coverage.first_open_ms, BASE)
        self.assertEqual(coverage.last_open_ms, BASE + 23 * HOUR)

    def test_the_repository_serves_a_contract_shaped_window(self):
        self.seed_one_day()
        self.build()
        repo = KlineRepository(self.out)
        boundary = BASE + 12 * HOUR                     # scan boundary T
        window = repo.window("BTCUSDT", "1h", end_ms=boundary, count=499)
        self.assertEqual(len(window), 12)
        # C-6.2: every served candle had fully elapsed at T.
        self.assertTrue(bool(np.all(window["timestamp"] + HOUR <= boundary)))
        self.assertEqual(int(window["timestamp"][-1]), boundary - HOUR)

    def test_the_stored_dtype_is_the_repository_dtype(self):
        self.seed_one_day()
        self.build()
        stored = np.load(dataset_path(self.out, "BTCUSDT", "1h"))
        self.assertEqual(stored.dtype, KLINE_DTYPE)
        self.assertEqual(stored.ndim, 1)

    def test_all_twelve_columns_survive_the_round_trip(self):
        """C-6.1: every column is retained on disk, `qv` above all — the
        universe reconstruction of C-6.9 is a sum over it."""
        row = kline(BASE, qv=123456.75, nt=99, tb=1.5, tq=2.5)
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, [row])
        self.build()
        stored = np.load(dataset_path(self.out, "BTCUSDT", "1h"))[0]
        self.assertEqual(float(stored["qv"]), 123456.75)
        self.assertEqual(int(stored["nt"]), 99)
        self.assertEqual(float(stored["tb"]), 1.5)
        self.assertEqual(float(stored["tq"]), 2.5)
        self.assertEqual(int(stored["ct"]), BASE + HOUR - 1)

    def test_both_csv_eras_build_byte_identical_datasets(self):
        """The header is presentation: the same candles stored either way
        produce the same canonical bytes.

        Their snapshot ids nonetheless differ, and must: C-6.10 hashes the raw
        lineage, so a dataset derived from different evidence is never silently
        declared comparable to one derived from other evidence — even when the
        rows happen to coincide.
        """
        self.seed_one_day(header=True)
        first = self.build()
        headed = dataset_path(self.out, "BTCUSDT", "1h").read_bytes()

        self.setUp()
        self.seed_one_day(header=False)
        second = self.build()
        headless = dataset_path(self.out, "BTCUSDT", "1h").read_bytes()

        self.assertEqual(headed, headless)
        self.assertNotEqual(first.data_snapshot_id, second.data_snapshot_id)

    def test_a_rebuild_is_byte_identical(self):
        self.seed_one_day()
        first = self.build()
        array_bytes = dataset_path(self.out, "BTCUSDT", "1h").read_bytes()
        manifest_bytes = (self.out / MANIFEST_NAME).read_bytes()
        gaps_bytes = (self.out / GAP_INVENTORY_NAME).read_bytes()

        second = self.build()
        self.assertEqual(dataset_path(self.out, "BTCUSDT", "1h").read_bytes(),
                         array_bytes)
        self.assertEqual((self.out / MANIFEST_NAME).read_bytes(), manifest_bytes)
        self.assertEqual((self.out / GAP_INVENTORY_NAME).read_bytes(), gaps_bytes)
        self.assertEqual(first.data_snapshot_id, second.data_snapshot_id)

    def test_the_manifest_carries_no_build_timestamp(self):
        """A `built_at` field would make every rebuild differ, which is exactly
        what a snapshot id exists to rule out."""
        self.seed_one_day()
        self.build()
        text = (self.out / MANIFEST_NAME).read_text()
        for forbidden in ("built_at", "generated_at", "timestamp"):
            self.assertNotIn(forbidden, text)

    def test_the_snapshot_id_is_the_repositorys_own_formula(self):
        self.seed_one_day()
        report = self.build()
        manifest = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertEqual(
            report.data_snapshot_id,
            compute_snapshot_id(builder_version=BUILDER_VERSION,
                                datasets=manifest["datasets"]),
        )

    def test_a_different_builder_version_is_a_different_snapshot(self):
        self.seed_one_day()
        first = self.build()
        other = CanonicalBuilder(self.store, builder_version="m4.2-test")
        second = other.build(self.out)
        self.assertNotEqual(first.data_snapshot_id, second.data_snapshot_id)

    def test_different_data_is_a_different_snapshot(self):
        self.seed_one_day()
        first = self.build()
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE + DAY,
                         day_rows(BASE + DAY, 24))
        second = self.build()
        self.assertNotEqual(first.data_snapshot_id, second.data_snapshot_id)

    def test_lineage_records_every_contributing_artifact(self):
        self.seed_one_day()
        record = commit_daily_zip(self.store, "BTCUSDT", "1h", BASE + DAY,
                                  day_rows(BASE + DAY, 24))
        report = self.build()
        dataset = report.datasets[0]
        self.assertEqual(len(dataset.raw_sha256), 2)
        self.assertIn(record.sha256, dataset.raw_sha256)
        self.assertIn(record.artifact_path, dataset.source_artifacts)

    def test_several_symbols_and_timeframes_build_together(self):
        self.seed_one_day("BTCUSDT", "1h")
        commit_daily_zip(self.store, "BTCUSDT", "1d", BASE,
                         [kline(BASE, DAY)])
        commit_daily_zip(self.store, "ETHUSDT", "1h", BASE, day_rows(BASE, 24))
        report = self.build()
        self.assertEqual(len(report.datasets), 3)
        repo = KlineRepository(self.out)
        self.assertEqual(repo.symbols("1h"), ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(repo.symbols("1d"), ("BTCUSDT",))

    def test_a_symbol_filter_restricts_the_build(self):
        self.seed_one_day("BTCUSDT")
        commit_daily_zip(self.store, "ETHUSDT", "1h", BASE, day_rows(BASE, 24))
        report = self.build(symbols=["BTCUSDT"])
        self.assertEqual(report.symbols, ("BTCUSDT",))

    def test_a_timeframe_filter_restricts_the_build(self):
        self.seed_one_day("BTCUSDT", "1h")
        commit_daily_zip(self.store, "BTCUSDT", "1d", BASE, [kline(BASE, DAY)])
        report = self.build(timeframes=["1d"])
        self.assertEqual([d.timeframe for d in report.datasets], ["1d"])

    def test_an_unknown_timeframe_is_refused(self):
        with self.assertRaises(BuildError):
            self.build(timeframes=["15m"])

    def test_discovery_artifacts_are_not_kline_sources(self):
        self.seed_one_day()
        commit(self.store, "discovery/x-exchange_info.json", b"{}",
               kind="discovery_json", symbol="", timeframe="",
               start_ms=0, end_ms=0)
        report = self.build()
        self.assertEqual(len(report.datasets), 1)

    def test_an_all_empty_symbol_is_omitted_from_the_manifest(self):
        """A delisted pair whose REST answers were all `[]`. The repository
        forbids recording an empty dataset (I-R3), so it is left out and
        reported instead of silently appearing with zero rows."""
        commit_rest(self.store, "DEADUSDT", "1h", BASE, [])
        report = self.build()
        self.assertEqual(report.datasets, ())
        self.assertEqual(report.empty, ("DEADUSDT|1h",))
        manifest = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["datasets"], {})

    def test_gaps_reach_the_gap_inventory(self):
        rows = [kline(BASE), kline(BASE + HOUR), kline(BASE + 6 * HOUR)]
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows)
        report = self.build()
        self.assertEqual(report.datasets[0].missing_candles, 4)
        inventory = json.loads((self.out / GAP_INVENTORY_NAME).read_text())
        gaps = inventory["gaps"]["BTCUSDT|1h"]
        self.assertEqual(gaps, [{"start_ms": BASE + 2 * HOUR,
                                 "end_ms": BASE + 5 * HOUR, "missing": 4}])

    def test_a_gap_is_never_filled_in_the_stored_rows(self):
        rows = [kline(BASE), kline(BASE + 6 * HOUR)]
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows)
        self.build()
        stored = np.load(dataset_path(self.out, "BTCUSDT", "1h"))
        self.assertEqual(len(stored), 2)

    def test_a_window_over_a_gap_is_short_not_padded(self):
        """C-6.3: the window is the last N *rows*, not the last N intervals."""
        rows = [kline(BASE), kline(BASE + 6 * HOUR)]
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows)
        self.build()
        repo = KlineRepository(self.out)
        window = repo.window("BTCUSDT", "1h", end_ms=BASE + 7 * HOUR, count=499)
        self.assertEqual(len(window), 2)


# ==========================================================================
# Merging independent evidence
# ==========================================================================

class TestMerge(BuilderCase):

    def test_two_days_merge_into_one_ascending_series(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE + DAY,
                         day_rows(BASE + DAY, 24))
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        report = self.build()
        stored = np.load(dataset_path(self.out, "BTCUSDT", "1h"))
        self.assertEqual(len(stored), 48)
        self.assertTrue(bool(np.all(np.diff(stored["timestamp"]) == HOUR)))
        self.assertEqual(report.datasets[0].first_open_ms, BASE)

    def test_identical_overlap_collapses_without_complaint(self):
        """A day fetched by REST before its archive existed, then covered by the
        archive as well. Same evidence twice is not a conflict."""
        rows = day_rows(BASE, 24)
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows)
        commit_rest(self.store, "BTCUSDT", "1h", BASE, rows)
        report = self.build()
        self.assertEqual(report.datasets[0].rows, 24)
        self.assertEqual(report.datasets[0].duplicates_collapsed, 24)

    def test_disagreeing_overlap_is_a_hard_error(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        commit_rest(self.store, "BTCUSDT", "1h", BASE,
                    day_rows(BASE, 24, close=999.0, high=1000.0))
        with self.assertRaises(ConflictingCandles) as caught:
            self.build()
        self.assertIn("discard the wrong artifact", str(caught.exception))

    def test_an_out_of_order_artifact_is_reported_against_its_own_file(self):
        rows = [kline(BASE + HOUR), kline(BASE)]
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows)
        with self.assertRaises(UnorderedCandles) as caught:
            self.build()
        self.assertIn("BTCUSDT-1h-2026-03-01.zip", str(caught.exception))

    def test_a_misfiled_row_is_caught_against_its_declared_coverage(self):
        rows = day_rows(BASE, 24) + [kline(BASE + DAY)]
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows)
        with self.assertRaises(CoverageMismatch):
            self.build()


# ==========================================================================
# Corrupted input fails loudly
# ==========================================================================

class TestCorruptInput(BuilderCase):

    def test_a_tampered_artifact_stops_the_build(self):
        """Raw corruption surfaces as the raw tier's own error, not a build
        error: the evidence is bad, the builder is not, and the remedy is in
        the acquisition tier (`discard` then re-download)."""
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        target = self.store.path_for(
            "bulk/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-03-01.zip")
        target.write_bytes(target.read_bytes()[:-20] + b"\x00" * 20)
        with self.assertRaises(ChecksumMismatch):
            self.build()
        self.assertFalse((self.out / MANIFEST_NAME).exists())

    def test_integrity_verification_can_be_skipped_for_a_verified_store(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        builder = CanonicalBuilder(self.store, verify_integrity=False)
        self.assertEqual(builder.build(self.out).total_rows, 24)

    def test_an_archive_without_its_named_member_is_refused(self):
        name = "BTCUSDT-1h-2026-03-01"
        relative = f"bulk/um/daily/klines/BTCUSDT/1h/{name}.zip"
        payload = zip_bytes("surprise.csv", csv_text(day_rows(BASE, 2),
                                                     header=True))
        commit(self.store, relative, payload, kind="daily_zip", symbol="BTCUSDT",
               timeframe="1h", start_ms=BASE, end_ms=BASE + DAY)
        builder = CanonicalBuilder(self.store, verify_integrity=False)
        with self.assertRaises(SourceUnusable):
            builder.build(self.out)

    def test_an_unparseable_row_names_the_artifact(self):
        text = csv_text(day_rows(BASE, 3), header=True).replace("100.0", "oops")
        name = "BTCUSDT-1h-2026-03-01"
        relative = f"bulk/um/daily/klines/BTCUSDT/1h/{name}.zip"
        commit(self.store, relative, zip_bytes(name + ".csv", text),
               kind="daily_zip", symbol="BTCUSDT", timeframe="1h",
               start_ms=BASE, end_ms=BASE + DAY)
        with self.assertRaises(MalformedRow) as caught:
            self.build()
        self.assertIn(relative, str(caught.exception))

    def test_an_unknown_artifact_kind_is_refused(self):
        commit(self.store, "weird/thing.bin", b"data", kind="something_else",
               symbol="BTCUSDT", timeframe="1h", start_ms=BASE,
               end_ms=BASE + DAY)
        with self.assertRaises(SourceUnusable):
            self.build()

    def test_a_failed_build_writes_no_manifest(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE,
                         [kline(BASE, high=1.0, low=2.0, open=1.5, close=1.5)])
        with self.assertRaises(OhlcInconsistent):
            self.build()
        self.assertFalse(dataset_path(self.out, "BTCUSDT", "1h").exists())
        self.assertFalse((self.out / MANIFEST_NAME).exists())

    def test_a_partial_build_is_no_repository_at_all(self):
        """The manifest is the commit marker. A build that dies on the second
        symbol may leave the first symbol's `.npy` behind, but without a
        manifest the repository refuses to open anything — so orphan bytes are
        never served as if they were a dataset."""
        commit_daily_zip(self.store, "AAAUSDT", "1h", BASE, day_rows(BASE, 24))
        commit_daily_zip(self.store, "ZZZUSDT", "1h", BASE,
                         [kline(BASE, high=1.0, low=2.0, open=1.5, close=1.5)])
        with self.assertRaises(OhlcInconsistent):
            self.build()
        self.assertTrue(dataset_path(self.out, "AAAUSDT", "1h").is_file())
        self.assertFalse((self.out / MANIFEST_NAME).exists())
        with self.assertRaises(RepositoryNotFound):
            KlineRepository(self.out)

    def test_rebuilding_after_a_failure_is_deterministic(self):
        commit_daily_zip(self.store, "AAAUSDT", "1h", BASE, day_rows(BASE, 24))
        bad = "bulk/um/daily/klines/ZZZUSDT/1h/ZZZUSDT-1h-2026-03-01.zip"
        commit_daily_zip(self.store, "ZZZUSDT", "1h", BASE,
                         [kline(BASE, high=1.0, low=2.0, open=1.5, close=1.5)])
        with self.assertRaises(OhlcInconsistent):
            self.build()
        orphan = dataset_path(self.out, "AAAUSDT", "1h").read_bytes()

        self.store.discard(bad)
        report = self.build()
        self.assertEqual(report.symbols, ("AAAUSDT",))
        self.assertEqual(dataset_path(self.out, "AAAUSDT", "1h").read_bytes(),
                         orphan)
        self.assertEqual(KlineRepository(self.out).data_snapshot_id,
                         report.data_snapshot_id)


# ==========================================================================
# TASK 6 — universe compatibility (data sufficiency only; no reconstruction)
# ==========================================================================

class TestUniverseCompatibility(BuilderCase):

    def test_quote_volume_is_available_for_a_trailing_24_hour_sum(self):
        """C-6.9 ranks by the sum of `qv` over the 24 closed H1 rows at T. The
        dataset must make exactly that computable — and here it is, from the
        repository alone, with no reconstruction implemented anywhere."""
        rows = [kline(BASE + i * HOUR, qv=float(i + 1)) for i in range(48)]
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, rows[:24])
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE + DAY, rows[24:])
        self.build()
        repo = KlineRepository(self.out)
        boundary = BASE + 48 * HOUR
        window = repo.window("BTCUSDT", "1h", end_ms=boundary, count=24)
        self.assertEqual(len(window), 24)
        self.assertEqual(float(window["qv"].sum()), sum(range(25, 49)))

    def test_a_delisted_symbol_keeps_its_own_coverage_bounds(self):
        """Symbol availability at T is `first_open_ms <= T <= last_open_ms`, so
        a pair that stopped trading simply stops being rankable."""
        commit_daily_zip(self.store, "DEADUSDT", "1h", BASE, day_rows(BASE, 24))
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE + DAY,
                         day_rows(BASE + DAY, 24))
        self.build()
        repo = KlineRepository(self.out)
        self.assertIn("DEADUSDT", repo.symbols("1h"))
        self.assertEqual(repo.coverage("DEADUSDT", "1h").last_open_ms,
                         BASE + 23 * HOUR)
        after = repo.window("DEADUSDT", "1h", end_ms=BASE + 40 * HOUR, count=24)
        self.assertEqual(len(after), 24)          # its history, never extended

    def test_eligibility_is_decidable_from_row_counts(self):
        """C-6.9 excludes a symbol without 24 closed H1 rows at T. A short
        window is what makes that decidable without a partial sum."""
        commit_daily_zip(self.store, "NEWUSDT", "1h", BASE, day_rows(BASE, 5))
        self.build()
        repo = KlineRepository(self.out)
        window = repo.window("NEWUSDT", "1h", end_ms=BASE + 5 * HOUR, count=24)
        self.assertEqual(len(window), 5)


class TestSurvivorshipDeclaration(BuilderCase):

    def test_an_undeclared_inventory_is_reported_incomplete(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        report = self.build()
        self.assertTrue(report.survivorship_incomplete)
        manifest = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertTrue(manifest["survivorship_incomplete"])
        self.assertIn("unverified", manifest["survivorship_note"])

    def test_a_complete_inventory_clears_the_flag(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        report = self.build(inventory=["BTCUSDT"])
        self.assertFalse(report.survivorship_incomplete)
        self.assertEqual(report.missing_symbols, ())

    def test_a_missing_symbol_names_itself(self):
        commit_daily_zip(self.store, "BTCUSDT", "1h", BASE, day_rows(BASE, 24))
        report = self.build(inventory=["BTCUSDT", "GONEUSDT"])
        self.assertTrue(report.survivorship_incomplete)
        self.assertEqual(report.missing_symbols, ("GONEUSDT",))
        manifest = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertEqual(manifest["missing_symbols"], ["GONEUSDT"])


# ==========================================================================
# Boundaries
# ==========================================================================

def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, parsed rather than grepped — docstrings name
    packages they deliberately do not import."""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestBoundaries(unittest.TestCase):
    CANONICAL = Path("backtest") / "canonical"

    def test_the_builder_is_the_only_bridge_between_the_tiers(self):
        """Neither tier may learn about the other, or about this builder."""
        for package in ("acquire", "data"):
            for path in (Path("backtest") / package).glob("*.py"):
                for module in imported_modules(path):
                    self.assertFalse(
                        module.startswith("backtest.canonical"),
                        f"{path} imports {module}")

    def test_canonical_may_import_both_tiers(self):
        imported = set()
        for path in self.CANONICAL.glob("*.py"):
            imported |= imported_modules(path)
        self.assertTrue(any(m.startswith("backtest.acquire") for m in imported))
        self.assertTrue(any(m.startswith("backtest.data") for m in imported))

    def test_production_runtime_never_imports_the_builder(self):
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"),
                                 f"{path} imports {module}")

    def test_the_builder_never_touches_the_network(self):
        for path in self.CANONICAL.glob("*.py"):
            for module in imported_modules(path):
                self.assertNotIn(module.split(".")[0],
                                 {"requests", "urllib", "http", "socket"},
                                 f"{path.name} imports {module}")

    def test_the_builder_knows_nothing_about_strategy_or_replay(self):
        for path in self.CANONICAL.glob("*.py"):
            code = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#"))
            for forbidden in ("Orchestrator", "MarketSnapshot", "SignalLedger",
                              "pandas_ta", "score"):
                self.assertNotIn(forbidden, code, f"{path.name}: {forbidden}")

    def test_atr_is_never_precomputed(self):
        """C-6.5: ATR is the adapter's, computed after slicing. A stored `atr`
        column would silently change every indicator in replay."""
        self.assertNotIn("atr", KLINE_DTYPE.names)
        for path in self.CANONICAL.glob("*.py"):
            self.assertNotIn("ta.atr", path.read_text(encoding="utf-8"))

    def test_the_format_constants_come_from_the_repository(self):
        """One definition of the on-disk contract, imported by the writer."""
        from backtest.canonical import builder as builder_module
        self.assertIs(builder_module.KLINE_DTYPE, KLINE_DTYPE)
        self.assertIs(builder_module.compute_snapshot_id, compute_snapshot_id)
        self.assertIs(builder_module.dataset_path, dataset_path)


if __name__ == "__main__":
    unittest.main()
