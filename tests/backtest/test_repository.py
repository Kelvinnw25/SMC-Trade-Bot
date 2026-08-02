"""The historical kline repository — Milestone M3.

Covers dataset location, identity validation, the C-6.2/C-6.3 lookup and window
rules, the C-4.3/C-9.5 forward range, immutability, determinism, and every
declared failure mode.

The fixture writer below lives HERE, not in `backtest/`: populating a repository
is Milestone M4's responsibility, and shipping a writer inside the read-only
package would blur that boundary. It writes through the repository's public
format constants, which is exactly what M4 will do.
"""
from __future__ import annotations

import gc
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from backtest.data import (
    COLUMNS,
    CorruptDataset,
    DatasetCoverage,
    DatasetNotFound,
    FORMAT_VERSION,
    INTERVAL_MS,
    KLINE_DTYPE,
    KlineRepository,
    MANIFEST_NAME,
    ManifestError,
    RepositoryNotFound,
    SnapshotMismatch,
    UnknownTimeframe,
    compute_snapshot_id,
    dataset_path,
)

HOUR = INTERVAL_MS["1h"]
DAY = INTERVAL_MS["1d"]
M5 = INTERVAL_MS["5m"]

#: 2025-03-11 12:00:00 UTC — the contract's worked example (C-5.8).
T_EXAMPLE = 1_741_694_400_000


# --------------------------------------------------------------------------
# Fixture writer (test-side; M4 will write the same format)
# --------------------------------------------------------------------------

def make_rows(open_times: list[int]) -> np.ndarray:
    """A kline array with positionally distinct, recognisable values."""
    rows = np.zeros(len(open_times), dtype=KLINE_DTYPE)
    for idx, open_ms in enumerate(open_times):
        rows[idx] = (
            open_ms,                    # timestamp
            100.0 + idx,                # open
            101.0 + idx,                # high
            99.0 + idx,                 # low
            100.5 + idx,                # close
            1000.0 + idx,               # v
            open_ms + 1,                # ct (deliberately not the selection key)
            5_000.0 + idx,              # qv
            10 + idx,                   # nt
            1.0, 2.0, 0.0,              # tb, tq, i
        )
    return rows


def write_repo(root: Path, datasets: dict[tuple[str, str], np.ndarray],
               *, builder_version: str = "test-writer-1",
               snapshot_id: str | None = None,
               format_version: int = FORMAT_VERSION) -> str:
    """Write a complete repository; return its data_snapshot_id."""
    meta: dict[str, dict] = {}
    for (symbol, timeframe), rows in datasets.items():
        path = dataset_path(root, symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, rows, allow_pickle=False)
        meta[f"{symbol}|{timeframe}"] = {
            "rows": int(len(rows)),
            "first_open_ms": int(rows["timestamp"][0]),
            "last_open_ms": int(rows["timestamp"][-1]),
            "raw_sha256": [f"sha-{symbol}-{timeframe}"],
        }
    computed = compute_snapshot_id(builder_version=builder_version, datasets=meta)
    manifest = {
        "format_version": format_version,
        "builder_version": builder_version,
        "data_snapshot_id": snapshot_id if snapshot_id is not None else computed,
        "datasets": meta,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return computed


def hourly(n: int, *, end_ms: int = T_EXAMPLE, symbol: str = "BTCUSDT") -> np.ndarray:
    """`n` contiguous H1 candles whose last one closes exactly at `end_ms`."""
    last_open = end_ms - HOUR
    return make_rows([last_open - HOUR * i for i in range(n - 1, -1, -1)])


class RepoCase(unittest.TestCase):
    """Base for tests needing a temp repository.

    `tearDown` drops the repository and collects before removing the directory:
    a memory-mapped file cannot be deleted on Windows while the mapping is open.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo: KlineRepository | None = None

    def tearDown(self):
        self.repo = None
        gc.collect()
        self._tmp.cleanup()

    def build(self, datasets, **kw) -> KlineRepository:
        write_repo(self.root, datasets, **kw)
        self.repo = KlineRepository(self.root)
        return self.repo


# --------------------------------------------------------------------------
# Loading and identity
# --------------------------------------------------------------------------

class TestLoading(RepoCase):
    def test_loads_and_exposes_identity(self):
        expected = write_repo(self.root, {("BTCUSDT", "1h"): hourly(10)})
        self.repo = KlineRepository(self.root)
        self.assertEqual(self.repo.data_snapshot_id, expected)
        self.assertEqual(self.repo.builder_version, "test-writer-1")
        self.assertEqual(self.repo.root, self.root)

    def test_identity_is_deterministic_across_constructions(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(10)})
        first = KlineRepository(self.root).data_snapshot_id
        second = KlineRepository(self.root).data_snapshot_id
        self.repo = None
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_identity_changes_when_content_changes(self):
        a = write_repo(self.root, {("BTCUSDT", "1h"): hourly(10)})
        with tempfile.TemporaryDirectory() as other:
            b = write_repo(Path(other), {("BTCUSDT", "1h"): hourly(11)})
        self.assertNotEqual(a, b)

    def test_expected_snapshot_id_is_enforced(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(10)})
        with self.assertRaises(SnapshotMismatch):
            KlineRepository(self.root, expect_snapshot_id="0000000000000000")

    def test_expected_snapshot_id_accepts_the_right_one(self):
        expected = write_repo(self.root, {("BTCUSDT", "1h"): hourly(10)})
        self.repo = KlineRepository(self.root, expect_snapshot_id=expected)
        self.assertEqual(self.repo.data_snapshot_id, expected)

    def test_tampered_manifest_is_rejected(self):
        # A hand-edited row count no longer hashes to the recorded id.
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(10)})
        path = self.root / MANIFEST_NAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["datasets"]["BTCUSDT|1h"]["rows"] = 999
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(SnapshotMismatch):
            KlineRepository(self.root)


class TestLocating(RepoCase):
    def setUp(self):
        super().setUp()
        self.build({
            ("BTCUSDT", "1h"): hourly(10),
            ("ETHUSDT", "1h"): hourly(10),
            ("BTCUSDT", "1d"): make_rows([DAY * i for i in range(1, 6)]),
        })

    def test_symbols_are_sorted_and_timeframe_scoped(self):
        self.assertEqual(self.repo.symbols("1h"), ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(self.repo.symbols("1d"), ("BTCUSDT",))
        self.assertEqual(self.repo.symbols("5m"), ())

    def test_coverage_reports_the_manifest(self):
        cov = self.repo.coverage("BTCUSDT", "1h")
        self.assertIsInstance(cov, DatasetCoverage)
        self.assertEqual(cov.rows, 10)
        self.assertEqual(cov.interval_ms, HOUR)
        self.assertEqual(cov.last_open_ms, T_EXAMPLE - HOUR)

    def test_missing_dataset_raises(self):
        with self.assertRaises(DatasetNotFound):
            self.repo.coverage("DOGEUSDT", "1h")

    def test_unknown_timeframe_raises(self):
        with self.assertRaises(UnknownTimeframe):
            self.repo.symbols("4h")
        with self.assertRaises(UnknownTimeframe):
            self.repo.window("BTCUSDT", "15m", end_ms=T_EXAMPLE, count=1)


# --------------------------------------------------------------------------
# Lookup — C-6.2
# --------------------------------------------------------------------------

class TestLookup(RepoCase):
    def setUp(self):
        super().setUp()
        # Opens at T-5h .. T-1h; the last one closes exactly at T_EXAMPLE.
        self.build({("BTCUSDT", "1h"): hourly(5)})

    def _idx(self, end_ms):
        # White-box: the C-6.2 selection rule is worth testing in isolation, but
        # it is private because only `window` needs a row position.
        return self.repo._last_index_at("BTCUSDT", "1h", end_ms=end_ms)  # noqa: SLF001

    def test_candle_closing_exactly_at_end_ms_qualifies(self):
        # C-6.2: open + interval <= end_ms, so the boundary candle is included.
        self.assertEqual(self._idx(T_EXAMPLE), 4)

    def test_candle_still_forming_does_not_qualify(self):
        self.assertEqual(self._idx(T_EXAMPLE - 1), 3)

    def test_one_ms_before_a_close_excludes_it(self):
        self.assertEqual(self._idx(T_EXAMPLE - HOUR), 3)
        self.assertEqual(self._idx(T_EXAMPLE - HOUR - 1), 2)

    def test_before_all_history_is_minus_one_not_an_error(self):
        self.assertEqual(self._idx(T_EXAMPLE - HOUR * 10), -1)

    def test_after_all_history_returns_the_last_row(self):
        self.assertEqual(self._idx(T_EXAMPLE + HOUR * 100), 4)

    def test_close_time_column_is_not_used_for_selection(self):
        # `ct` is stored as open+1ms; if selection used it, a query at
        # open+2ms would qualify. It must not (decision R-4).
        rows = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=5)
        self.assertEqual(int(rows["ct"][0]), int(rows["timestamp"][0]) + 1)
        first_open = T_EXAMPLE - HOUR * 5
        self.assertEqual(self._idx(first_open + 2), -1)


# --------------------------------------------------------------------------
# Windows — C-6.3
# --------------------------------------------------------------------------

class TestWindow(RepoCase):
    def test_returns_the_last_count_elapsed_rows(self):
        self.build({("BTCUSDT", "1h"): hourly(600)})
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=499)
        self.assertEqual(len(w), 499)
        self.assertEqual(int(w["timestamp"][-1]), T_EXAMPLE - HOUR)
        self.assertEqual(int(w["timestamp"][0]), T_EXAMPLE - HOUR * 499)

    def test_contract_worked_example_shapes(self):
        # C-5.8: at T = 2025-03-11T12:00Z the frames are 119 D1 / 499 H1 / 199 M5.
        self.build({
            ("BTCUSDT", "1h"): hourly(600),
            ("BTCUSDT", "1d"): make_rows(
                [T_EXAMPLE - DAY * i for i in range(200, 0, -1)]),
            ("BTCUSDT", "5m"): make_rows(
                [T_EXAMPLE - M5 * i for i in range(300, 0, -1)]),
        })
        self.assertEqual(
            len(self.repo.window("BTCUSDT", "1d", end_ms=T_EXAMPLE, count=119)), 119)
        self.assertEqual(
            len(self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=499)), 499)
        m5 = self.repo.window("BTCUSDT", "5m", end_ms=T_EXAMPLE, count=199)
        self.assertEqual(len(m5), 199)
        # The last closed M5 opens at 11:55 — the value C-5.8 records.
        self.assertEqual(int(m5["timestamp"][-1]), T_EXAMPLE - M5)

    def test_short_history_truncates_and_never_pads(self):
        self.build({("BTCUSDT", "1h"): hourly(7)})
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=499)
        self.assertEqual(len(w), 7)

    def test_before_all_history_returns_empty(self):
        self.build({("BTCUSDT", "1h"): hourly(5)})
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE - HOUR * 99, count=10)
        self.assertEqual(len(w), 0)
        self.assertEqual(w.dtype, KLINE_DTYPE)

    def test_gap_is_not_filled_and_the_window_stays_row_based(self):
        # 10 hourly candles with hours 4 and 5 missing (an exchange outage).
        opens = [T_EXAMPLE - HOUR * i for i in range(10, 0, -1)]
        del opens[4:6]
        self.build({("BTCUSDT", "1h"): make_rows(opens)})
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=8)
        self.assertEqual(len(w), 8)                    # 8 rows, not 8 hours
        span = int(w["timestamp"][-1]) - int(w["timestamp"][0])
        self.assertEqual(span, HOUR * 9)               # spans 10 hours of history
        self.assertEqual(list(w["timestamp"]), opens)  # exactly the stored rows

    def test_window_of_one(self):
        self.build({("BTCUSDT", "1h"): hourly(5)})
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)
        self.assertEqual(len(w), 1)
        self.assertEqual(int(w["timestamp"][0]), T_EXAMPLE - HOUR)

    def test_non_positive_count_is_a_caller_bug(self):
        self.build({("BTCUSDT", "1h"): hourly(5)})
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=bad)

    def test_trailing_24h_window_for_universe_reconstruction(self):
        # C-6.9 sums `qv` over the trailing 24 closed H1 rows.
        self.build({("BTCUSDT", "1h"): hourly(100)})
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=24)
        self.assertEqual(len(w), 24)
        self.assertEqual(float(w["qv"].sum()), sum(5_000.0 + i for i in range(76, 100)))


# --------------------------------------------------------------------------
# Ranges — C-4.3 / C-9.5
# --------------------------------------------------------------------------

class TestRange(RepoCase):
    def setUp(self):
        super().setUp()
        self.opens = [T_EXAMPLE + M5 * i for i in range(24)]
        self.build({("BTCUSDT", "5m"): make_rows(self.opens)})

    def test_half_open_interval(self):
        r = self.repo.range("BTCUSDT", "5m",
                            start_ms=T_EXAMPLE, end_ms=T_EXAMPLE + HOUR)
        self.assertEqual(len(r), 12)                       # the 12 M5 of one hour
        self.assertEqual(int(r["timestamp"][0]), T_EXAMPLE)
        self.assertEqual(int(r["timestamp"][-1]), T_EXAMPLE + HOUR - M5)

    def test_consecutive_ranges_tile_without_overlap(self):
        a = self.repo.range("BTCUSDT", "5m",
                            start_ms=T_EXAMPLE, end_ms=T_EXAMPLE + HOUR)
        b = self.repo.range("BTCUSDT", "5m",
                            start_ms=T_EXAMPLE + HOUR, end_ms=T_EXAMPLE + 2 * HOUR)
        self.assertEqual(len(a) + len(b), 24)
        self.assertEqual(set(a["timestamp"]) & set(b["timestamp"]), set())

    def test_empty_interval(self):
        r = self.repo.range("BTCUSDT", "5m", start_ms=T_EXAMPLE, end_ms=T_EXAMPLE)
        self.assertEqual(len(r), 0)

    def test_interval_outside_history(self):
        r = self.repo.range("BTCUSDT", "5m",
                            start_ms=T_EXAMPLE - HOUR, end_ms=T_EXAMPLE)
        self.assertEqual(len(r), 0)

    def test_reversed_bounds_are_a_caller_bug(self):
        with self.assertRaises(ValueError):
            self.repo.range("BTCUSDT", "5m",
                            start_ms=T_EXAMPLE + HOUR, end_ms=T_EXAMPLE)

    def test_range_returns_only_stored_rows_across_a_gap(self):
        self.repo = None
        gc.collect()
        with tempfile.TemporaryDirectory() as other:
            root = Path(other)
            opens = [T_EXAMPLE + M5 * i for i in range(12)]
            del opens[3:7]
            write_repo(root, {("BTCUSDT", "5m"): make_rows(opens)})
            repo = KlineRepository(root)
            r = repo.range("BTCUSDT", "5m",
                           start_ms=T_EXAMPLE, end_ms=T_EXAMPLE + HOUR)
            self.assertEqual(len(r), 8)               # not 12 — nothing is filled
            self.assertEqual(list(r["timestamp"]), opens)
            del repo, r
            gc.collect()


# --------------------------------------------------------------------------
# Immutability, determinism, mmap correctness
# --------------------------------------------------------------------------

class TestGuarantees(RepoCase):
    def setUp(self):
        super().setUp()
        self.build({("BTCUSDT", "1h"): hourly(100)})

    def test_returned_window_is_read_only(self):
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=10)
        self.assertFalse(w.flags.writeable)
        with self.assertRaises(ValueError):
            w["close"][0] = 1.0

    def test_returned_window_shares_no_memory_with_the_mapping(self):
        # This is what lets a caller hold a window indefinitely: it is a copy,
        # so it cannot be invalidated by the mapping's lifetime.
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=10)
        backing = self.repo._dataset("BTCUSDT", "1h")   # noqa: SLF001 - white box
        self.assertFalse(np.shares_memory(w, backing))

    def test_two_windows_are_independent_objects(self):
        a = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=10)
        b = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=10)
        self.assertIsNot(a, b)
        self.assertFalse(np.shares_memory(a, b))

    def test_repeated_queries_are_byte_identical(self):
        results = [
            self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=37).tobytes()
            for _ in range(5)
        ]
        self.assertEqual(len(set(results)), 1)

    def test_identical_across_repository_instances(self):
        first = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=37)
        other = KlineRepository(self.root)
        second = other.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=37)
        self.assertEqual(first.tobytes(), second.tobytes())
        del other, second
        gc.collect()

    def test_query_order_does_not_affect_results(self):
        forward = [self.repo.window("BTCUSDT", "1h",
                                    end_ms=T_EXAMPLE - HOUR * k, count=5).tobytes()
                   for k in range(10)]
        backward = [self.repo.window("BTCUSDT", "1h",
                                     end_ms=T_EXAMPLE - HOUR * k, count=5).tobytes()
                    for k in reversed(range(10))]
        self.assertEqual(forward, list(reversed(backward)))

    def test_backing_array_is_memory_mapped_and_read_only(self):
        backing = self.repo._dataset("BTCUSDT", "1h")   # noqa: SLF001 - white box
        self.assertIsInstance(backing, np.memmap)
        self.assertFalse(backing.flags.writeable)

    def test_mapping_is_opened_once_and_reused(self):
        first = self.repo._dataset("BTCUSDT", "1h")     # noqa: SLF001 - white box
        second = self.repo._dataset("BTCUSDT", "1h")    # noqa: SLF001 - white box
        self.assertIs(first, second)

    def test_columns_match_the_declared_format(self):
        w = self.repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)
        self.assertEqual(w.dtype.names, COLUMNS)
        self.assertEqual(w.dtype, KLINE_DTYPE)
        self.assertEqual(w.dtype["timestamp"], np.dtype("<i8"))


# --------------------------------------------------------------------------
# Failure behaviour — Task 5
# --------------------------------------------------------------------------

class TestFailures(RepoCase):
    def test_missing_root(self):
        with self.assertRaises(RepositoryNotFound):
            KlineRepository(self.root / "nope")

    def test_missing_manifest(self):
        with self.assertRaises(RepositoryNotFound):
            KlineRepository(self.root)

    def test_malformed_manifest_json(self):
        (self.root / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(ManifestError):
            KlineRepository(self.root)

    def test_manifest_is_not_an_object(self):
        (self.root / MANIFEST_NAME).write_text("[]", encoding="utf-8")
        with self.assertRaises(ManifestError):
            KlineRepository(self.root)

    def test_unsupported_format_version(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(5)}, format_version=99)
        with self.assertRaises(ManifestError):
            KlineRepository(self.root)

    def test_bad_dataset_key(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(5)})
        path = self.root / MANIFEST_NAME
        m = json.loads(path.read_text(encoding="utf-8"))
        m["datasets"]["BTCUSDT|4h"] = m["datasets"].pop("BTCUSDT|1h")
        m["data_snapshot_id"] = compute_snapshot_id(
            builder_version=m["builder_version"], datasets=m["datasets"])
        path.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(ManifestError):
            KlineRepository(self.root)

    def test_manifest_entry_missing_fields(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(5)})
        path = self.root / MANIFEST_NAME
        m = json.loads(path.read_text(encoding="utf-8"))
        del m["datasets"]["BTCUSDT|1h"]["first_open_ms"]
        m["data_snapshot_id"] = "irrelevant"
        path.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(ManifestError):
            KlineRepository(self.root)

    def test_empty_dataset_is_refused(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(5)})
        path = self.root / MANIFEST_NAME
        m = json.loads(path.read_text(encoding="utf-8"))
        m["datasets"]["BTCUSDT|1h"]["rows"] = 0
        m["data_snapshot_id"] = compute_snapshot_id(
            builder_version=m["builder_version"], datasets=m["datasets"])
        path.write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(ManifestError):
            KlineRepository(self.root)

    def test_missing_array_file(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(5)})
        dataset_path(self.root, "BTCUSDT", "1h").unlink()
        repo = KlineRepository(self.root)          # manifest alone is consistent
        with self.assertRaises(CorruptDataset):    # the array is not
            repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)

    def test_truncated_array(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(20)})
        path = dataset_path(self.root, "BTCUSDT", "1h")
        raw = path.read_bytes()
        path.write_bytes(raw[:len(raw) // 2])
        repo = KlineRepository(self.root)
        with self.assertRaises(CorruptDataset):
            repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)

    def test_row_count_contradicts_the_manifest(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(20)})
        # Rewrite the array with fewer rows but keep the manifest's bounds valid.
        rows = hourly(20)
        np.save(dataset_path(self.root, "BTCUSDT", "1h"), rows[:19], allow_pickle=False)
        repo = KlineRepository(self.root)
        with self.assertRaises(CorruptDataset):
            repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)

    def test_wrong_dtype(self):
        write_repo(self.root, {("BTCUSDT", "1h"): hourly(5)})
        np.save(dataset_path(self.root, "BTCUSDT", "1h"),
                np.zeros(5, dtype=np.float64), allow_pickle=False)
        repo = KlineRepository(self.root)
        with self.assertRaises(CorruptDataset):
            repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)

    def test_non_increasing_open_times(self):
        rows = hourly(6)
        writable = np.array(rows, dtype=KLINE_DTYPE)
        writable["timestamp"][3] = int(writable["timestamp"][1])  # duplicate
        write_repo(self.root, {("BTCUSDT", "1h"): writable})
        repo = KlineRepository(self.root)
        with self.assertRaises(CorruptDataset):
            repo.window("BTCUSDT", "1h", end_ms=T_EXAMPLE, count=1)

    def test_every_failure_is_a_repository_error(self):
        from backtest.data import RepositoryError
        for exc in (RepositoryNotFound, ManifestError, SnapshotMismatch,
                    UnknownTimeframe, DatasetNotFound, CorruptDataset):
            self.assertTrue(issubclass(exc, RepositoryError), exc.__name__)


# --------------------------------------------------------------------------
# Boundaries — the repository knows nothing above it
# --------------------------------------------------------------------------

class TestBoundaries(unittest.TestCase):
    def test_repository_imports_nothing_from_the_production_runtime(self):
        source = (Path("backtest") / "data" / "repository.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import v2", source)
        self.assertNotIn("from v2", source)

    def test_repository_mentions_no_layer_above_it(self):
        source = (Path("backtest") / "data" / "repository.py").read_text(
            encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        ).lower()
        # Names that would signal leaked knowledge if they appeared as code.
        for forbidden in ("orchestrator", "signal", "scoring", "lifecycle",
                          "outcome", "analyzer", "download", "requests"):
            self.assertNotIn(f"{forbidden}(", code, forbidden)
            self.assertNotIn(f"import {forbidden}", code, forbidden)

    def test_production_runtime_does_not_import_backtest(self):
        for path in Path("v2").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import backtest", source, str(path))
            self.assertNotIn("from backtest", source, str(path))


if __name__ == "__main__":
    unittest.main()
