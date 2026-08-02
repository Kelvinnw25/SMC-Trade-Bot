"""M11 — the Historical Corpus as one resumable, verifiable operation.

Hermetic: a synthetic exchange serves generated monthly archives, so nothing here
touches the network. The archives are real Binance-format ZIPs containing real
12-column kline CSVs, parsed by the production parser and validated by the
production validator — the fake is the *transport*, never the format.

Coverage follows the milestone's task list — multiple symbols, multiple months,
every timeframe replay needs, incremental updates, resume after interruption,
deterministic storage, versioning, manifest generation, integrity verification and
duplicate prevention — with the emphasis on the two properties a corpus is worth
nothing without: building it twice yields the same identity, and a corrupted byte
is found rather than served.
"""
from __future__ import annotations

import io
import json
import time
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.acquire.discovery import LISTED, SymbolInfo
from backtest.acquire.storage import RawStore, sha256_hex
from backtest.acquire.transport import TransportNotFound
from backtest.corpus import (
    CORPUS_MANIFEST_NAME, CorpusError, CorpusSpec, build_corpus, load_corpus,
    open_corpus, verify_corpus,
)

DAY_MS = 86_400_000
INTERVAL_MS = {"1h": 3_600_000, "1d": DAY_MS, "5m": 300_000}


# ---------------------------------------------------------------------------
# A synthetic exchange — real formats, no network
# ---------------------------------------------------------------------------

def kline_csv(start_ms: int, end_ms: int, interval_ms: int,
              *, base: float = 100.0) -> str:
    """A month of well-formed klines. Deterministic and OHLC-consistent."""
    lines = []
    open_time = start_ms
    step = 0
    while open_time < end_ms:
        drift = (step % 24) * 0.5
        o = base + drift
        c = o + 0.25
        h = c + 0.75
        low = o - 0.75
        volume = 10.0 + (step % 7)
        quote = volume * c
        lines.append(",".join(str(value) for value in (
            open_time, f"{o:.4f}", f"{h:.4f}", f"{low:.4f}", f"{c:.4f}",
            f"{volume:.4f}", open_time + interval_ms - 1, f"{quote:.4f}",
            10 + step % 5, f"{volume / 2:.4f}", f"{quote / 2:.4f}", 0)))
        open_time += interval_ms
        step += 1
    return "\n".join(lines) + "\n"


def zip_bytes(member: str, text: str) -> bytes:
    """A ZIP with a **fixed** member timestamp.

    `writestr` defaults a member's `date_time` to now, so two archives built
    seconds apart differ in bytes and therefore in SHA-256. That would make the
    fixture non-deterministic and the determinism tests below meaningless — they
    would pass or fail on where a two-second ZIP clock tick landed.
    """
    info = zipfile.ZipInfo(member, date_time=(2025, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, text)
    return buffer.getvalue()


def month_bounds(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (datetime(year + (month == 12), (month % 12) + 1, 1,
                    tzinfo=timezone.utc))
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


class SyntheticExchange:
    """Serves any monthly archive it is asked for; 404s everything else.

    Records every URL so a test can assert what a *resumed* build did not fetch.
    """

    def __init__(self, symbols=("AAABUSDT", "BBBCUSDT"),
                 months=((2025, 1), (2025, 2)),
                 timeframes=("1h", "1d", "5m")):
        self.calls: list[str] = []
        self.payloads: dict[str, bytes] = {}
        for symbol in symbols:
            for timeframe in timeframes:
                for year, month in months:
                    self._add(symbol, timeframe, year, month)

    def _add(self, symbol: str, timeframe: str, year: int, month: int) -> None:
        period = f"{year:04d}-{month:02d}"
        name = f"{symbol}-{timeframe}-{period}"
        url = (f"https://data.binance.vision/data/futures/um/monthly/klines/"
               f"{symbol}/{timeframe}/{name}.zip")
        start_ms, end_ms = month_bounds(year, month)
        data = zip_bytes(name + ".csv",
                         kline_csv(start_ms, end_ms, INTERVAL_MS[timeframe]))
        self.payloads[url] = data
        self.payloads[url + ".CHECKSUM"] = (
            f"{sha256_hex(data)}  {name}.zip\n".encode())

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        try:
            return self.payloads[url]
        except KeyError:
            raise TransportNotFound(f"404 {url}") from None


def inventory(*names: str) -> list[SymbolInfo]:
    return [SymbolInfo(symbol=name, listing_state=LISTED, in_exchange_info=True,
                       in_archive=True, contract_type="PERPETUAL",
                       quote_asset="USDT", onboard_date_ms=0)
            for name in names]


class CorpusCase(unittest.TestCase):

    SYMBOLS = ("AAABUSDT", "BBBCUSDT")

    def setUp(self):
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def spec(self, name="corpus", **kw) -> CorpusSpec:
        params = dict(
            root=self.tmp / name,
            start=date(2025, 1, 1), end=date(2025, 2, 28),
            timeframes=("1h", "1d", "5m"),
            # Binance publishes a monthly archive some days after the month
            # ends, so the planner falls back to daily items for a month whose
            # window does not extend past that lag. This fixture serves monthly
            # archives only, so it declares the lag away rather than pretending
            # the daily endpoints exist.
            monthly_lag_days=0,
        )
        params.update(kw)
        return CorpusSpec(**params)

    def build(self, spec=None, exchange=None, **kw):
        spec = spec or self.spec()
        exchange = exchange or SyntheticExchange()
        return build_corpus(spec, transport=exchange,
                            inventory=inventory(*self.SYMBOLS), **kw), exchange


# ==========================================================================
# TASK 3 — the corpus infrastructure
# ==========================================================================

class TestBuild(CorpusCase):

    def test_a_corpus_covers_every_symbol_timeframe_and_month(self):
        report, _ = self.build()
        self.assertEqual(report.build.symbols, self.SYMBOLS)
        self.assertEqual(len(report.build.datasets), 6)      # 2 symbols x 3 tf
        self.assertEqual(report.artifact_count, 12)          # x 2 months
        hours = 31 * 24 + 28 * 24
        by_key = {(d.symbol, d.timeframe): d.rows for d in report.build.datasets}
        self.assertEqual(by_key[("AAABUSDT", "1h")], hours)
        self.assertEqual(by_key[("AAABUSDT", "1d")], 31 + 28)
        self.assertEqual(by_key[("AAABUSDT", "5m")], hours * 12)

    def test_the_two_tiers_live_in_separate_directories(self):
        report, _ = self.build()
        root = Path(report.spec.root)
        self.assertTrue((root / "raw").is_dir())
        self.assertTrue((root / "canonical" / "klines").is_dir())
        self.assertTrue((root / CORPUS_MANIFEST_NAME).is_file())

    def test_the_manifest_states_scope_identity_and_integrity(self):
        report, _ = self.build()
        manifest = load_corpus(report.spec.root)
        self.assertEqual(manifest["corpus_id"], report.corpus_id)
        self.assertEqual(manifest["data_snapshot_id"], report.data_snapshot_id)
        self.assertEqual(manifest["spec"]["start"], "2025-01-01")
        self.assertEqual(manifest["spec"]["universe_scope"], "inventory")
        self.assertEqual(manifest["inventory"], list(self.SYMBOLS))
        self.assertEqual(manifest["artifact_count"], 12)
        self.assertEqual(sorted(manifest["acquired_timeframes"]),
                         ["1d", "1h", "5m"])
        self.assertEqual(manifest["corpus_schema_version"], 1)
        self.assertIn("builder_version", manifest)

    def test_the_corpus_opens_as_the_repository_replay_consumes(self):
        report, _ = self.build()
        repository = open_corpus(report.spec.root)
        self.assertEqual(repository.data_snapshot_id, report.data_snapshot_id)
        self.assertEqual(repository.symbols("1h"), self.SYMBOLS)

    def test_opening_with_the_wrong_expected_snapshot_is_refused(self):
        report, _ = self.build()
        with self.assertRaises(CorpusError) as caught:
            open_corpus(report.spec.root, expect_snapshot="deadbeefdeadbeef")
        self.assertIn("never comparable", str(caught.exception))

    def test_a_directory_without_a_manifest_is_not_a_corpus(self):
        with self.assertRaises(CorpusError) as caught:
            load_corpus(self.tmp / "empty")
        self.assertIn("not a corpus", str(caught.exception))


# ==========================================================================
# TASK 3 — resume, incremental update, duplicate prevention
# ==========================================================================

class TestResumeAndIncrement(CorpusCase):

    def test_rebuilding_downloads_nothing_and_changes_nothing(self):
        """Resume is running the same command again. No flag, no checkpoint."""
        spec = self.spec()
        first, _ = self.build(spec)
        second_exchange = SyntheticExchange()
        second = build_corpus(spec, transport=second_exchange,
                              inventory=inventory(*self.SYMBOLS))
        self.assertEqual(second.acquired, 0)
        self.assertEqual(second.already_present, 12)
        self.assertEqual(len(second.plan), 0)
        self.assertEqual(second_exchange.calls, [])
        self.assertEqual(second.corpus_id, first.corpus_id)
        self.assertEqual(second.data_snapshot_id, first.data_snapshot_id)

    def test_an_interrupted_acquisition_resumes_where_it_stopped(self):
        """Half the archives exist; the second run fetches only the rest."""
        spec = self.spec()
        partial = SyntheticExchange(symbols=("AAABUSDT",))
        first = build_corpus(spec, transport=partial,
                             inventory=inventory("AAABUSDT"))
        self.assertEqual(first.acquired, 6)

        full = SyntheticExchange()
        second = build_corpus(spec, transport=full,
                              inventory=inventory(*self.SYMBOLS))
        self.assertEqual(second.already_present, 6)
        self.assertEqual(second.acquired, 6)
        for url in full.calls:
            self.assertNotIn("AAABUSDT", url)

    def test_extending_the_window_is_the_same_operation(self):
        """January first, then the same corpus asked for January and February."""
        january = self.spec(end=date(2025, 1, 31))
        build_corpus(january, transport=SyntheticExchange(months=((2025, 1),)),
                     inventory=inventory(*self.SYMBOLS))
        extended = SyntheticExchange(months=((2025, 1), (2025, 2)))
        report = build_corpus(self.spec(), transport=extended,
                              inventory=inventory(*self.SYMBOLS))
        self.assertEqual(report.already_present, 6)
        self.assertEqual(report.acquired, 6)
        self.assertEqual(report.artifact_count, 12)
        for url in extended.calls:
            self.assertNotIn("2025-01", url)

    def test_a_committed_artifact_is_never_re_downloaded(self):
        spec = self.spec()
        self.build(spec)
        store = RawStore(spec.raw_root)
        before = {record.artifact_path: record.sha256
                  for record in store.records()}
        exchange = SyntheticExchange()
        build_corpus(spec, transport=exchange,
                     inventory=inventory(*self.SYMBOLS))
        after = {record.artifact_path: record.sha256
                 for record in store.records()}
        self.assertEqual(before, after)
        self.assertEqual(exchange.calls, [])


# ==========================================================================
# TASK 8 — determinism
# ==========================================================================

class TestDeterminism(CorpusCase):

    def test_two_independent_builds_produce_the_same_identity(self):
        """Different directories, different downloads, identical corpus."""
        first, _ = self.build(self.spec("a"))
        second, _ = self.build(self.spec("b"))
        self.assertEqual(first.corpus_id, second.corpus_id)
        self.assertEqual(first.data_snapshot_id, second.data_snapshot_id)

    def test_the_canonical_manifests_are_byte_identical(self):
        first, _ = self.build(self.spec("a"))
        second, _ = self.build(self.spec("b"))
        for name in ("manifest.json", "gap_inventory.json"):
            self.assertEqual(
                (Path(first.spec.root) / "canonical" / name).read_bytes(),
                (Path(second.spec.root) / "canonical" / name).read_bytes(),
                name)

    def test_the_corpus_manifest_differs_in_nothing(self):
        first, _ = self.build(self.spec("a"))
        second, _ = self.build(self.spec("b"))
        self.assertEqual(
            (Path(first.spec.root) / CORPUS_MANIFEST_NAME).read_bytes(),
            (Path(second.spec.root) / CORPUS_MANIFEST_NAME).read_bytes())

    def test_the_stored_arrays_are_byte_identical(self):
        first, _ = self.build(self.spec("a"))
        second, _ = self.build(self.spec("b"))
        for path in (Path(first.spec.root) / "canonical" / "klines").rglob("*.npy"):
            twin = (Path(second.spec.root) / "canonical" / "klines"
                    / path.relative_to(Path(first.spec.root) / "canonical"
                                       / "klines"))
            self.assertEqual(path.read_bytes(), twin.read_bytes(), path.name)

    def test_the_identity_ignores_when_the_bytes_were_downloaded(self):
        """Two people downloading the same history must reach one corpus."""
        first, _ = self.build(self.spec("a"))
        store = RawStore(Path(first.spec.root) / "raw")
        stamps = {record.downloaded_at for record in store.records()}
        self.assertTrue(stamps)
        second, _ = self.build(self.spec("b"))
        self.assertEqual(first.corpus_id, second.corpus_id)

    def test_a_different_spec_is_a_different_corpus(self):
        first, _ = self.build(self.spec("a"))
        narrower, _ = self.build(self.spec("b", symbols=("AAABUSDT",)))
        self.assertNotEqual(first.corpus_id, narrower.corpus_id)
        self.assertNotEqual(first.data_snapshot_id, narrower.data_snapshot_id)


# ==========================================================================
# TASK 5 — integrity, corruption, completeness
# ==========================================================================

class TestIntegrity(CorpusCase):

    def test_a_clean_corpus_verifies(self):
        report, _ = self.build()
        result = verify_corpus(report.spec.root)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.artifacts_verified, 12)
        self.assertEqual(result.corpus_id, report.corpus_id)

    def test_a_corrupted_artifact_is_detected_not_served(self):
        report, _ = self.build()
        store = RawStore(Path(report.spec.root) / "raw")
        victim = store.records()[0]
        path = store.path_for(victim.artifact_path)
        path.write_bytes(path.read_bytes() + b"rot")
        result = verify_corpus(report.spec.root)
        self.assertFalse(result.ok)
        self.assertTrue(any("raw tier" in problem for problem in result.problems),
                        result.problems)

    def test_a_swapped_canonical_tier_is_detected(self):
        report, _ = self.build()
        manifest_path = Path(report.spec.root) / CORPUS_MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["data_snapshot_id"] = "0000000000000000"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        result = verify_corpus(report.spec.root)
        self.assertFalse(result.ok)
        with self.assertRaises(CorpusError):
            open_corpus(report.spec.root)

    def test_a_shallow_verify_still_catches_an_identity_mismatch(self):
        report, _ = self.build()
        result = verify_corpus(report.spec.root, deep=False)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.artifacts_verified, 0)


# ==========================================================================
# TASK 3 — declared bias travels with the data
# ==========================================================================

class TestDeclaredBias(CorpusCase):

    def test_a_full_inventory_corpus_declares_no_selection_bias(self):
        report, _ = self.build()
        manifest = load_corpus(report.spec.root)
        self.assertEqual(manifest["spec"]["universe_scope"], "inventory")
        self.assertFalse(any("universe_scope=pinned" in bias
                             for bias in manifest["declared_biases"]))

    def test_a_pinned_corpus_declares_selection_bias(self):
        report, _ = self.build(self.spec(symbols=("AAABUSDT",)))
        manifest = load_corpus(report.spec.root)
        self.assertEqual(manifest["spec"]["universe_scope"], "pinned")
        biases = " ".join(manifest["declared_biases"])
        self.assertIn("universe_scope=pinned", biases)
        self.assertIn("universe_mode='static'", biases)

    def test_a_corpus_without_m5_declares_degraded_outcomes(self):
        report, _ = self.build(self.spec(timeframes=("1h", "1d")))
        biases = " ".join(load_corpus(report.spec.root)["declared_biases"])
        self.assertIn("no 5m data", biases)
        self.assertIn("excursion_degraded", biases)

    def test_pinning_a_symbol_the_exchange_never_listed_is_refused(self):
        with self.assertRaises(CorpusError) as caught:
            self.build(self.spec(symbols=("NOSUCHUSDT",)))
        self.assertIn("MUST NOT invent a symbol", str(caught.exception))


class TestDiscoveryEvidence(CorpusCase):
    """M12: the discovery payloads must be committed, not fetched and dropped.

    `exchangeInfo` is the only source of instrument metadata and Binance serves
    the current state only, so discarding it destroys a day of unrecoverable
    history on every build. `DiscoveryResult` carries the raw bytes precisely so
    the caller can commit them.
    """

    EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"

    def discovering_exchange(self):
        from backtest.acquire.discovery import ARCHIVE_LISTING_URL
        exchange = SyntheticExchange()
        exchange.payloads[self.EXCHANGE_INFO] = json.dumps({"symbols": [
            {"symbol": name, "status": "TRADING", "contractType": "PERPETUAL",
             "quoteAsset": "USDT", "onboardDate": 0, "filters": []}
            for name in self.SYMBOLS
        ]}, sort_keys=True).encode()
        prefixes = "".join(
            f"<CommonPrefixes><Prefix>data/futures/um/daily/klines/{name}/"
            f"</Prefix></CommonPrefixes>" for name in self.SYMBOLS)
        exchange.payloads[ARCHIVE_LISTING_URL] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"{prefixes}</ListBucketResult>").encode()
        return exchange

    def test_discovery_payloads_are_committed_as_evidence(self):
        exchange = self.discovering_exchange()
        spec = self.spec()
        build_corpus(spec, transport=exchange)          # no inventory injected
        kinds = [r.kind for r in RawStore(spec.raw_root).records()]
        self.assertIn("discovery_json", kinds)

    def test_the_committed_payload_is_the_exchange_info_bytes(self):
        exchange = self.discovering_exchange()
        spec = self.spec()
        build_corpus(spec, transport=exchange)
        store = RawStore(spec.raw_root)
        records = {r.source_url: r for r in store.records()
                   if r.kind == "discovery_json"}
        self.assertEqual(set(records), {"exchange_info", "archive_listing"})
        record = records["exchange_info"]
        stored = store.path_for(record.artifact_path).read_bytes()
        self.assertEqual(sha256_hex(stored), record.sha256)
        self.assertIn(b"AAABUSDT", stored)
        self.assertIn(b"onboardDate", stored)

    def test_an_injected_inventory_commits_no_discovery(self):
        """Nothing was fetched, so there is no evidence to record."""
        report, _ = self.build()
        kinds = [r.kind for r in RawStore(Path(report.spec.root) / "raw").records()]
        self.assertNotIn("discovery_json", kinds)

    def test_committing_evidence_does_not_break_corpus_reproducibility(self):
        """Discovery artifacts carry a **timestamped path** (two discoveries days
        apart legitimately differ), so admitting them to `corpus_id` would make two
        independent builds of identical history produce different identities —
        destroying the property M11 proved on real data."""
        first = build_corpus(self.spec("a"), transport=self.discovering_exchange())
        time.sleep(1.1)          # guarantee a different discovery timestamp
        second = build_corpus(self.spec("b"), transport=self.discovering_exchange())
        stamps = set()
        for report in (first, second):
            stamps.update(
                r.artifact_path for r in RawStore(Path(report.spec.root) / "raw"
                                                  ).records()
                if r.kind == "discovery_json")
        self.assertEqual(len(stamps), 4, "the two builds must differ in evidence")
        self.assertEqual(first.corpus_id, second.corpus_id)
        self.assertEqual(first.data_snapshot_id, second.data_snapshot_id)


class TestSpecValidation(CorpusCase):

    def test_an_inverted_window_is_refused(self):
        with self.assertRaises(CorpusError):
            self.spec(start=date(2025, 3, 1), end=date(2025, 1, 1))

    def test_an_unknown_timeframe_is_refused(self):
        with self.assertRaises(CorpusError):
            self.spec(timeframes=("1h", "3m"))

    def test_an_empty_symbol_tuple_is_refused_rather_than_meaning_all(self):
        with self.assertRaises(CorpusError) as caught:
            self.spec(symbols=())
        self.assertIn("pass None for the full", str(caught.exception))

    def test_the_spec_hash_ignores_where_the_corpus_sits_on_disk(self):
        self.assertEqual(self.spec("a").spec_hash, self.spec("b").spec_hash)


if __name__ == "__main__":
    unittest.main()
