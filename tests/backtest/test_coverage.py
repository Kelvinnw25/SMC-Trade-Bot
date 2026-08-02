"""Stage 1 coverage analysis — `backtest/coverage.py`.

The load-bearing property is `TestUniverseEquivalence`: the vectorised trailing
volume must agree with the real `HistoricalMarketDataAdapter` on real data. If it
does not, the top-K union is wrong, Stage 2 acquires the wrong symbols, and the
error stays invisible until the official run.

Everything else asserts that the report measures rather than decides.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from backtest.coverage import (
    COVERAGE_SCHEMA_VERSION,
    UNIVERSE_TRAILING_ROWS,
    CoverageError,
    CoverageThresholds,
    InstrumentClass,
    SymbolCoverage,
    _rank_matrix,
    _shakedown,
    _symbol_coverage,
    _trailing_volume,
    _window_coverage,
    analyze_coverage,
)
from backtest.coverage import H1_MS
from backtest.data.repository import KLINE_DTYPE

PILOT = Path("data/corpus-pilot")
H1_MS = 3_600_000


def _rows(timestamps, *, qv=None, high=None, low=None, volume=None):
    array = np.zeros(len(timestamps), dtype=KLINE_DTYPE)
    array["timestamp"] = np.asarray(timestamps, dtype=np.int64)
    array["qv"] = 1.0 if qv is None else qv
    array["high"] = 2.0 if high is None else high
    array["low"] = 1.0 if low is None else low
    array["v"] = 10.0 if volume is None else volume
    array["open"] = array["close"] = 1.5
    return array


# ---------------------------------------------------------------------------


class TestTrailingVolume(unittest.TestCase):
    """C-6.9 semantics: the last 24 *stored* rows, not the last 24 hours."""

    def test_fewer_than_24_stored_rows_is_ineligible(self):
        times = np.arange(0, 23 * H1_MS, H1_MS, dtype=np.int64)
        out = _trailing_volume(np.array([23 * H1_MS]), times, np.ones(23))
        self.assertTrue(np.isnan(out[0]))

    def test_exactly_24_rows_becomes_eligible(self):
        times = np.arange(0, 24 * H1_MS, H1_MS, dtype=np.int64)
        out = _trailing_volume(np.array([24 * H1_MS]), times, np.ones(24))
        self.assertEqual(out[0], 24.0)

    def test_the_window_is_rows_not_hours_so_gaps_are_spanned(self):
        # A symbol with a hole still sums 24 stored rows, reaching further back
        # in wall-clock time. That is exactly what `KlineRepository.window` does,
        # and a dense rolling sum over the hour grid would get it wrong by
        # treating the hole as zeros.
        times = np.concatenate([
            np.arange(0, 12 * H1_MS, H1_MS),
            np.arange(500 * H1_MS, 512 * H1_MS, H1_MS),
        ]).astype(np.int64)
        out = _trailing_volume(np.array([512 * H1_MS]), times, np.ones(24))
        self.assertEqual(out[0], 24.0)

    def test_only_rows_strictly_before_the_boundary_are_summed(self):
        times = np.arange(0, 25 * H1_MS, H1_MS, dtype=np.int64)
        quote = np.arange(1.0, 26.0)
        out = _trailing_volume(np.array([24 * H1_MS]), times, quote)
        self.assertEqual(out[0], float(quote[:24].sum()))


class TestRankMatrix(unittest.TestCase):

    def test_ties_break_by_name_ascending(self):
        volumes = np.array([[5.0, 5.0, 5.0]])
        self.assertEqual(_rank_matrix(volumes, 3)[0].tolist(), [0, 1, 2])

    def test_volume_is_primary_and_descending(self):
        volumes = np.array([[1.0, 9.0, 5.0]])
        self.assertEqual(_rank_matrix(volumes, 3)[0].tolist(), [1, 2, 0])

    def test_ineligible_symbols_never_rank(self):
        volumes = np.array([[np.nan, 1.0, np.nan]])
        self.assertEqual(_rank_matrix(volumes, 3)[0].tolist(), [1, -1, -1])

    def test_a_short_universe_is_padded_not_invented(self):
        volumes = np.array([[np.nan, np.nan]])
        self.assertEqual(_rank_matrix(volumes, 2)[0].tolist(), [-1, -1])


@unittest.skipUnless((PILOT / "canonical").is_dir(),
                     "pilot corpus not present")
class TestUniverseEquivalence(unittest.TestCase):
    """The vectorised reconstruction must equal the adapter's, on real data.

    This is the test that protects Stage 2's symbol list.
    """

    def test_the_trailing_window_constant_has_not_drifted(self):
        from backtest.replay import adapter as replay_adapter

        self.assertEqual(UNIVERSE_TRAILING_ROWS,
                         replay_adapter.UNIVERSE_TRAILING_ROWS)

    def test_trailing_volume_matches_the_adapter_ranking(self):
        from backtest.data.repository import KlineRepository
        from backtest.replay.adapter import (
            HistoricalMarketDataAdapter, _to_live_symbol,
        )

        repository = KlineRepository(PILOT / "canonical")
        names = sorted(repository.symbols("1h"))
        adapter = HistoricalMarketDataAdapter(repository)

        # Boundaries spread across the pilot, well past the 24-row warm-up.
        coverage = repository.coverage(names[0], "1h")
        first, last = coverage.first_open_ms, coverage.last_open_ms
        boundaries = np.linspace(first + 48 * H1_MS, last, 12).astype(np.int64)
        boundaries = (boundaries // H1_MS) * H1_MS

        series = {}
        for name in names:
            rows = repository.range(name, "1h", start_ms=0,
                                    end_ms=np.iinfo(np.int64).max)
            series[name] = (rows["timestamp"].astype(np.int64),
                            rows["qv"].astype(np.float64))

        volumes = np.full((boundaries.size, len(names)), np.nan)
        for column, name in enumerate(names):
            times, quote = series[name]
            volumes[:, column] = _trailing_volume(boundaries, times, quote)
        ranks = _rank_matrix(volumes, len(names))

        for row, boundary_ms in enumerate(boundaries.tolist()):
            adapter.set_time(int(boundary_ms // 1000))
            expected = [entry.symbol
                        for entry in adapter.get_top_volume_symbols(len(names))]
            # The adapter emits live-format names (`BTC/USDT`); coverage works
            # in Binance-native names, because that is what the corpus and
            # Stage 2 acquisition speak. Compare the orderings, not the spelling.
            mine = [_to_live_symbol(names[i])
                    for i in ranks[row].tolist() if i >= 0]
            with self.subTest(boundary=boundary_ms):
                self.assertEqual(mine, expected)


class TestSymbolCoverage(unittest.TestCase):

    def test_a_contiguous_series_is_fully_complete(self):
        rows = _rows(np.arange(0, 10 * H1_MS, H1_MS))
        entry = _symbol_coverage("AAAUSDT", rows)
        self.assertEqual(entry.rows, 10)
        self.assertEqual(entry.gap_count, 0)
        self.assertEqual(entry.completeness, 1.0)

    def test_gaps_are_counted_in_hours_and_episodes(self):
        times = np.concatenate([np.arange(0, 3 * H1_MS, H1_MS),
                                np.arange(10 * H1_MS, 12 * H1_MS, H1_MS)])
        entry = _symbol_coverage("AAAUSDT", _rows(times))
        self.assertEqual(entry.gap_count, 1)
        self.assertEqual(entry.gap_hours, 7)
        self.assertEqual(entry.longest_gap_hours, 7)
        self.assertEqual(entry.rows, 5)
        self.assertEqual(entry.expected_rows, 12)

    def test_completeness_is_measured_over_the_symbols_own_span(self):
        # A symbol listed late is not "incomplete" for the period before it
        # existed; availability over the window is the timeline's job.
        rows = _rows(np.arange(1000 * H1_MS, 1010 * H1_MS, H1_MS))
        self.assertEqual(_symbol_coverage("LATEUSDT", rows).completeness, 1.0)

    def test_degenerate_candles_are_counted(self):
        times = np.arange(0, 4 * H1_MS, H1_MS)
        rows = _rows(times, high=[2.0, 1.0, 2.0, 1.0], low=[1.0, 1.0, 1.0, 1.0],
                     volume=[10.0, 0.0, 0.0, 10.0])
        entry = _symbol_coverage("WEIRDUSDT", rows)
        self.assertEqual(entry.flat_candle_hours, 2)
        self.assertEqual(entry.zero_volume_hours, 2)


class TestInstrumentClassification(unittest.TestCase):
    """Topic 4 — the universe rule is exchange-declared, not name-guessed.

    M13 moved the *rule* to `backtest.universe`, which is now its only caller;
    `tests/backtest/test_universe.py` owns its behaviour. What remains here is
    the narrower claim that `InstrumentClass` reports the resolver's verdict
    faithfully and never recomputes it — a class that recomputed would be a
    second rule, free to drift from the one live applies.
    """

    def test_it_reports_the_resolvers_verdict_and_never_recomputes(self):
        # The triple says "crypto perpetual", the resolution says ineligible.
        # The resolution wins, because this class is a projection, not a rule.
        entry = InstrumentClass("BTCUSDT", "PERPETUAL", "COIN", "USDT",
                                source="snapshot", eligible=False)
        self.assertFalse(entry.is_crypto_perpetual)
        self.assertTrue(entry.known)

    def test_an_eligible_instrument_is_reported_eligible(self):
        entry = InstrumentClass("BTCUSDT", "PERPETUAL", "COIN", "USDT",
                                source="snapshot", eligible=True)
        self.assertTrue(entry.is_crypto_perpetual)

    def test_a_tokenised_equity_is_excluded_despite_the_usdt_suffix(self):
        # `AAPLUSDT` passes the legacy `endswith("USDT")` filter; the metadata
        # rule is what rejects it.
        entry = InstrumentClass("AAPLUSDT", "TRADIFI_PERPETUAL", "EQUITY",
                                "USDT", source="snapshot", eligible=False)
        self.assertFalse(entry.is_crypto_perpetual)

    def test_an_index_product_is_excluded(self):
        self.assertFalse(
            InstrumentClass("BTCDOMUSDT", "PERPETUAL", "INDEX", "USDT",
                            source="snapshot",
                            eligible=False).is_crypto_perpetual)

    def test_a_delisted_symbol_is_unknown_not_guessed(self):
        entry = InstrumentClass("ANCUSDT")
        self.assertFalse(entry.known)
        self.assertFalse(entry.is_crypto_perpetual)


class TestShakedownSelection(unittest.TestCase):
    """Union of per-dimension extremes, not a weighted scalar."""

    def _coverage(self, symbol, **kw):
        base = dict(rows=1000, first_open_ms=0, last_open_ms=1000 * H1_MS,
                    expected_rows=1001, gap_count=0, gap_hours=0,
                    longest_gap_hours=0, zero_volume_hours=0,
                    flat_candle_hours=0)
        base.update(kw)
        return SymbolCoverage(symbol=symbol, **base)

    def test_each_pathology_contributes_its_own_extreme(self):
        symbols = [
            self._coverage("GAPPYUSDT", gap_count=99),
            self._coverage("FLATUSDT", flat_candle_hours=99),
            self._coverage("QUIETUSDT", zero_volume_hours=99),
            self._coverage("BTCUSDT", rows=5000),
        ]
        chosen, reasons = _shakedown(symbols, per_dimension=1, controls=1)
        self.assertIn("GAPPYUSDT", chosen)
        self.assertIn("FLATUSDT", chosen)
        self.assertIn("QUIETUSDT", chosen)
        self.assertIn("most_gaps", reasons["GAPPYUSDT"])
        self.assertIn("most_flat_candles", reasons["FLATUSDT"])

    def test_controls_are_always_included(self):
        # Without them a crash cannot be attributed to a pathology rather than
        # to a general regression.
        symbols = [self._coverage(f"S{i}USDT", rows=1000 + i) for i in range(5)]
        chosen, reasons = _shakedown(symbols, per_dimension=1, controls=2)
        controls = [s for s, why in reasons.items() if "control" in why]
        self.assertEqual(len(controls), 2)
        self.assertEqual(sorted(controls), ["S3USDT", "S4USDT"])

    def test_selection_is_deterministic(self):
        symbols = [self._coverage(f"S{i}USDT", gap_count=7) for i in range(6)]
        first, _ = _shakedown(symbols, per_dimension=2, controls=1)
        second, _ = _shakedown(symbols, per_dimension=2, controls=1)
        self.assertEqual(first, second)

    def test_every_selected_symbol_carries_its_reason(self):
        symbols = [self._coverage(f"S{i}USDT", gap_count=i) for i in range(4)]
        chosen, reasons = _shakedown(symbols, per_dimension=1, controls=1)
        for symbol in chosen:
            self.assertTrue(reasons[symbol])


@unittest.skipUnless((PILOT / "canonical").is_dir(),
                     "pilot corpus not present")
class TestReportOnRealCorpus(unittest.TestCase):

    def _report(self, **kw):
        # M13: the resolution is injected, never computed by coverage. The
        # pilot's five symbols are all currently listed, so every one resolves
        # from the snapshot the pilot committed.
        from backtest.data.repository import KlineRepository
        from backtest.universe import resolve
        resolution = resolve(KlineRepository(PILOT / "canonical").symbols("1h"),
                             corpus_root=PILOT)
        return analyze_coverage(PILOT, resolution=resolution,
                                universe_stride=6, **kw)

    def test_it_measures_the_pilot_without_choosing_anything(self):
        report = self._report()
        payload = report.to_dict()
        self.assertEqual(payload["schema_version"], COVERAGE_SCHEMA_VERSION)
        self.assertGreater(payload["inventory"]["symbols"], 0)
        self.assertIsNotNone(payload["data_snapshot_id"])
        # No field anywhere names a chosen window.
        text = json.dumps(payload)
        for forbidden in ("recommended", "chosen_start", "selected_window"):
            self.assertNotIn(forbidden, text)

    def test_candidate_windows_are_scored_against_declared_thresholds(self):
        repository_first = min(s.first_open_ms for s in self._report().symbols)
        start = int(repository_first // 1000) + 48 * 3600
        start -= start % 3600
        report = self._report(candidate_starts=(start,),
                              thresholds=CoverageThresholds(
                                  min_rankable_symbols=1))
        self.assertEqual(len(report.candidates), 1)
        self.assertIn(report.candidates[0].verdict, {"PASS", "WARN", "FAIL"})

    def test_an_unreachable_threshold_fails_the_candidate_with_a_reason(self):
        first = min(s.first_open_ms for s in self._report().symbols)
        start = int(first // 1000) + 48 * 3600
        start -= start % 3600
        report = self._report(
            candidate_starts=(start,),
            thresholds=CoverageThresholds(min_rankable_symbols=999))
        candidate = report.candidates[0]
        self.assertEqual(candidate.verdict, "FAIL")
        self.assertTrue(any("rankable" in reason for reason in candidate.reasons))

    def test_universe_evidence_answers_the_top30_vs_top50_question(self):
        first = min(s.first_open_ms for s in self._report().symbols)
        start = int(first // 1000) + 48 * 3600
        start -= start % 3600
        report = self._report(candidate_starts=(start,), top_k=2, wide_k=4)
        universe = report.universe
        self.assertIsNotNone(universe)
        # The marginal set is exactly what top-K would omit and wide-K keeps.
        self.assertEqual(set(universe.union_wide) - set(universe.union_top_k),
                         set(universe.marginal_symbols))
        self.assertTrue(universe.promotion_rate is None
                        or 0.0 <= universe.promotion_rate <= 1.0)

    def test_the_report_writes_json_and_markdown_and_refuses_to_clobber(self):
        report = self._report()
        with TemporaryDirectory() as tmp:
            path = report.write(tmp)
            self.assertTrue(path.is_file())
            self.assertTrue((Path(tmp) / "coverage.md").is_file())
            with self.assertRaises(CoverageError):
                report.write(tmp)
            report.write(tmp, overwrite=True)

    def test_progress_is_reported_without_any_partial_conclusion(self):
        from backtest.observe import OperationalLog, Progress

        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            progress = Progress(sink=None, log=log, interval=0.0)
            self._report(observer=progress)
            rows = [json.loads(line) for line
                    in log.path.read_text(encoding="utf-8").splitlines()]
            phases = {r.get("phase") for r in rows}
            self.assertIn("coverage/symbols", phases)
            # Progress only: nothing analytical leaks while the job runs.
            for row in rows:
                for key in row:
                    self.assertNotIn("union", key)
                    self.assertNotIn("verdict", key)


class TestWindowScopedMeasurement(unittest.TestCase):
    """M13.1 — the metrics that decide a verdict are measured in the window.

    The defect: `union_min_completeness`, `gap_ratio` and `worst_gap_hours` were
    lifetime aggregates. All three Stage 1 candidates reported the *identical*
    value 0.985525 and all three failed, because one symbol (ICPUSDT) had a
    626 h gap in September 2022 — outside the 2023-01-01 window entirely.

    A window-scoped metric that is constant across three different windows is a
    contradiction in terms, so the last test below pins exactly that.
    """

    H = H1_MS
    BASE = 1_600_000_000_000 // H * H          # an hour boundary

    def series(self, hours, *, drop=()):
        """Open times for `hours` consecutive H1 candles, minus `drop` indices."""
        return np.array(
            [self.BASE + i * self.H for i in range(hours) if i not in set(drop)],
            dtype=np.int64)

    def window(self, first_hour, last_hour):
        return self.BASE + first_hour * self.H, self.BASE + last_hour * self.H

    # -- 1/2/3: where the gap sits decides whether it counts ----------------

    def test_a_gap_before_the_window_start_is_ignored(self):
        # 10 h of history, hours 2-3 missing. Evaluate hours 5..10.
        times = self.series(10, drop=(2, 3))
        start, end = self.window(5, 10)
        stats = _window_coverage(times, start, end)
        self.assertEqual(stats.gap_hours, 0)
        self.assertEqual(stats.longest_gap_hours, 0)
        self.assertEqual(stats.completeness, 1.0)

    def test_a_gap_inside_the_window_is_counted(self):
        times = self.series(10, drop=(6, 7))
        start, end = self.window(5, 10)
        stats = _window_coverage(times, start, end)
        self.assertEqual(stats.gap_hours, 2)
        self.assertEqual(stats.longest_gap_hours, 2)
        self.assertEqual(stats.rows, 3)
        self.assertEqual(stats.expected, 5)

    def test_a_gap_after_the_window_end_is_ignored(self):
        times = self.series(10, drop=(8, 9))
        start, end = self.window(0, 5)
        stats = _window_coverage(times, start, end)
        self.assertEqual(stats.gap_hours, 0)
        self.assertEqual(stats.completeness, 1.0)

    def test_the_same_symbol_scores_differently_in_different_windows(self):
        # The property the bug made impossible.
        times = self.series(20, drop=(11, 12, 13))
        early = _window_coverage(times, *self.window(0, 10))
        late = _window_coverage(times, *self.window(10, 20))
        self.assertEqual(early.completeness, 1.0)
        self.assertLess(late.completeness, 1.0)
        self.assertNotEqual(early.completeness, late.completeness)

    def test_the_lifetime_bug_cannot_reappear(self):
        # Reconstructed exactly: one symbol, one long gap, three windows. Under
        # the old implementation all three returned the same number.
        times = self.series(300, drop=range(100, 150))
        values = [
            _window_coverage(times, *self.window(a, 300)).completeness
            for a in (0, 50, 200)]
        self.assertEqual(len(set(values)), 3, f"constant across windows: {values}")
        # The window that starts after the gap sees a whole series.
        self.assertEqual(values[-1], 1.0)

    # -- boundary semantics --------------------------------------------------

    def test_the_window_is_half_open_at_both_ends(self):
        times = self.series(10)
        stats = _window_coverage(times, *self.window(2, 5))
        # Hours 2, 3, 4 — the end boundary itself belongs to the next window.
        self.assertEqual(stats.rows, 3)
        self.assertEqual(stats.expected, 3)

    def test_no_hour_is_counted_by_two_adjacent_windows(self):
        times = self.series(12)
        first = _window_coverage(times, *self.window(0, 6))
        second = _window_coverage(times, *self.window(6, 12))
        self.assertEqual(first.rows + second.rows, 12)

    def test_a_gap_straddling_the_start_is_charged_only_for_its_inside_part(self):
        # Listed from hour 0; hours 8-14 missing; window starts at hour 10.
        times = self.series(20, drop=range(8, 15))
        stats = _window_coverage(times, *self.window(10, 20))
        # Hours 10-14 are missing inside the window; 15-19 are present.
        self.assertEqual(stats.gap_hours, 5)
        self.assertEqual(stats.longest_gap_hours, 5)
        self.assertEqual(stats.expected, 10)

    def test_a_symbol_listing_mid_window_is_not_charged_for_being_absent(self):
        # First row at hour 10, window starts at hour 0. Not-yet-listed is not
        # missing data — the reason the original measured over the lifespan.
        times = self.series(20)[10:]
        stats = _window_coverage(times, *self.window(0, 20))
        self.assertEqual(stats.gap_hours, 0)
        self.assertEqual(stats.completeness, 1.0)

    def test_a_symbol_delisting_mid_window_is_not_charged_for_being_gone(self):
        times = self.series(10)
        stats = _window_coverage(times, *self.window(0, 20))
        self.assertEqual(stats.gap_hours, 0)
        self.assertEqual(stats.completeness, 1.0)

    def test_a_symbol_outside_the_window_measures_as_absent(self):
        times = self.series(5)
        self.assertIsNone(_window_coverage(times, *self.window(10, 20)))

    def test_an_empty_series_measures_as_absent(self):
        self.assertIsNone(
            _window_coverage(np.array([], dtype=np.int64), *self.window(0, 5)))

    def test_scoring_ignores_lifetime_aggregates_entirely(self):
        # The strongest form of the guard: hand `_score_window` a SymbolCoverage
        # whose lifetime aggregates are catastrophic, and a series that is clean
        # inside the window. If any lifetime field still reached the verdict the
        # window would FAIL; it must PASS.
        from backtest.coverage import _score_window
        times = self.series(50)                       # no gaps at all
        start, end = self.window(10, 40)
        lying = SymbolCoverage(
            symbol="XUSDT", rows=1, first_open_ms=int(times[0]),
            last_open_ms=int(times[-1]), expected_rows=10_000,
            gap_count=99, gap_hours=9_999, longest_gap_hours=9_999,
            zero_volume_hours=0, flat_candle_hours=0)
        self.assertLess(lying.completeness, 0.01)     # lifetime says disaster
        grid = np.arange(int(times[0]), int(times[-1]) + H1_MS, H1_MS,
                         dtype=np.int64)
        candidate = _score_window(
            start_ms=start, end_ms=end, grid=grid,
            rankable=np.full(grid.size, 30, dtype=np.int64),
            ranks=np.zeros((grid.size, 1), dtype=np.int64),
            names=["XUSDT"], coverages=[lying],
            series={"XUSDT": times},
            thresholds=CoverageThresholds(min_rankable_symbols=1),
            top_k=1)
        self.assertEqual(candidate.verdict, "PASS", candidate.reasons)
        self.assertEqual(candidate.union_min_completeness, 1.0)
        self.assertEqual(candidate.worst_gap_hours, 0)
        self.assertEqual(candidate.gap_ratio, 0.0)

    def test_gap_hours_and_row_count_are_consistent(self):
        times = self.series(30, drop=(4, 5, 17, 18, 19))
        stats = _window_coverage(times, *self.window(0, 30))
        self.assertEqual(stats.rows + stats.gap_hours, stats.expected)
        self.assertEqual(stats.gap_count, 2)
        self.assertEqual(stats.longest_gap_hours, 3)


class TestFailureModes(unittest.TestCase):

    def test_an_empty_corpus_is_refused_with_a_reason(self):
        from backtest.universe import UniverseResolution
        empty = UniverseResolution(
            universe_id="0" * 16, definition_version=1, table_version=1,
            table_sha256="0" * 64, classifications=())
        with TemporaryDirectory() as tmp:
            with self.assertRaises(CoverageError):
                analyze_coverage(tmp, resolution=empty)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
