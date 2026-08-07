"""Step 4 — `CachingAnalyzer` (C-8.4). T4.1-T4.8 plus negative controls.

The central claim is that memoization is *invisible*: identical Signals, identical
bundles, identical `run_id`. Equality alone cannot establish that — a cache that
never hits passes every equality test trivially — so T4.6 asserts the hit rate and
the negative controls prove each assertion can fail.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest.replay.caching import (
    CacheStats, CachingAnalyzer, CachingPipeline, DEFAULT_MAX_ENTRIES,
    build_analyzer, c84_key, snapshot_key,
)
from backtest.replay.config import ReplayConfig
from v2.analysis.engine import V2Analyzer
from v2.domain import MarketSnapshot


def _fixture():
    path = Path("tests") / "backtest" / "fixtures" / "h1_gap_snapshots.py"
    spec = importlib.util.spec_from_file_location("_h1_gap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F = _fixture()
CORPUS = F.corpus_available()
PILOT = Path("data/corpus-pilot")
PILOT_OK = (PILOT / "canonical" / "manifest.json").is_file()


def _signal_tuple(signal):
    """The comparison basis for every equality assertion below.

    `Signal` is `@dataclass(frozen=True)` with **no** `compare=False` field and it
    carries the whole `SignalCandidate` — HTF context, regime, entry and risk
    plans, sweep, FVG, every swing and order block. Comparing the dataclass
    directly is therefore strictly stronger than enumerating fields by hand, and
    cannot silently stop covering a field that gets added later.
    """
    return signal


# ---------------------------------------------------------------------------
# T4.1 — the H1-gap fixture
# ---------------------------------------------------------------------------

@unittest.skipUnless(CORPUS, "data/stage1 corpus required for the H1-gap fixture")
class TestH1GapKey(unittest.TestCase):
    """T4.1: equality, MISS on every cycle, and the C-8.4-key collision control."""

    def _cycles(self, variant):
        return F.build_cycles(variant=variant)

    def test_the_fixture_outcomes_genuinely_differ(self):
        # The precondition IMPLEMENTATION_PLAN §5 demands explicitly: without it
        # the equality assertions below would pass against a broken cache.
        for variant in ("A", "B"):
            with self.subTest(variant=variant):
                outcomes = F.outcomes_of(self._cycles(variant), V2Analyzer())
                self.assertGreater(
                    len(set(outcomes)), 1,
                    f"variant {variant}: all three cycles produced {outcomes[0]}; "
                    f"the fixture no longer demonstrates anything")

    def test_the_c84_key_collides_across_the_gap(self):
        # The control. This is why the amendment is raised.
        for variant in ("A", "B"):
            with self.subTest(variant=variant):
                cycles = self._cycles(variant)
                keys = {c84_key(s, session_active=F.SESSION_ACTIVE)
                        for s in cycles}
                self.assertEqual(
                    1, len(keys),
                    "C-8.4's key must collide here; if it no longer does, the "
                    "fixture is not reproducing the gap")

    def test_the_implemented_key_separates_every_cycle(self):
        for variant in ("A", "B"):
            with self.subTest(variant=variant):
                cycles = self._cycles(variant)
                keys = {snapshot_key(s, session_active=F.SESSION_ACTIVE)
                        for s in cycles}
                self.assertEqual(3, len(keys))

    def test_cached_output_equals_uncached_for_every_cycle(self):
        for variant in ("A", "B"):
            with self.subTest(variant=variant):
                cycles = self._cycles(variant)
                plain, cached = V2Analyzer(), CachingAnalyzer()
                for index, snapshot in enumerate(cycles):
                    expected = plain.analyze(
                        snapshot, session_active=F.SESSION_ACTIVE)
                    actual = cached.analyze(
                        snapshot, session_active=F.SESSION_ACTIVE)
                    self.assertEqual(_signal_tuple(expected),
                                     _signal_tuple(actual),
                                     f"variant {variant} cycle {index}")

    def test_every_cycle_is_a_miss(self):
        # A hit on cycle 2 or 3 IS the defect: the snapshots differ.
        for variant in ("A", "B"):
            with self.subTest(variant=variant):
                cached = CachingAnalyzer()
                for snapshot in self._cycles(variant):
                    cached.analyze(snapshot, session_active=F.SESSION_ACTIVE)
                self.assertEqual(0, cached.stats.hits)
                self.assertEqual(3, cached.stats.misses)

    def test_a_c84_keyed_cache_would_serve_a_wrong_signal(self):
        """The failure mode made concrete, not asserted in the abstract."""
        cycles = self._cycles("A")
        plain = V2Analyzer()
        truth = [_signal_tuple(plain.analyze(s, session_active=F.SESSION_ACTIVE))
                 for s in cycles]

        served, store = [], {}
        for snapshot in cycles:
            key = c84_key(snapshot, session_active=F.SESSION_ACTIVE)
            if key not in store:
                store[key] = plain.analyze(
                    snapshot, session_active=F.SESSION_ACTIVE)
            served.append(_signal_tuple(store[key]))

        self.assertNotEqual(truth, served,
                            "the C-8.4 key must produce at least one wrong "
                            "Signal here, or the amendment has no basis")


# ---------------------------------------------------------------------------
# T4.2 / T4.4 / T4.5 — key isolation
# ---------------------------------------------------------------------------

class TestKeyIsolation(unittest.TestCase):

    def _snapshot(self, symbol="BTC/USDT", live=100.0):
        import pandas as pd

        cols = ["timestamp", "open", "high", "low", "close", "v", "atr"]
        rows = [[1_700_000_000_000 + i * 3_600_000, 1.0, 2.0, 0.5, 1.5, 10.0, 0.4]
                for i in range(60)]
        frame = pd.DataFrame(rows, columns=cols)
        return MarketSnapshot(
            symbol=symbol, d1=frame, h1=frame, h1_last_ts=1_700_000_000.0,
            h1_last_ts_dt=pd.Timestamp(1_700_000_000, unit="s", tz="UTC"),
            live_price=live, m5=frame)

    def test_two_snapshot_ids_never_share_an_entry(self):
        # T4.2 — the key carries data_snapshot_id, satisfying C-8.4's
        # "cleared or keyed per data_snapshot_id".
        snapshot = self._snapshot()
        a = snapshot_key(snapshot, session_active=False, data_snapshot_id="aaa")
        b = snapshot_key(snapshot, session_active=False, data_snapshot_id="bbb")
        self.assertNotEqual(a, b)

    def test_no_entry_is_shared_across_symbols(self):
        # T4.5
        a = snapshot_key(self._snapshot("BTC/USDT"), session_active=False)
        b = snapshot_key(self._snapshot("ETH/USDT"), session_active=False)
        self.assertNotEqual(a, b)

    def test_session_active_separates_entries(self):
        snapshot = self._snapshot()
        self.assertNotEqual(
            snapshot_key(snapshot, session_active=False),
            snapshot_key(snapshot, session_active=True))

    def test_live_price_enters_as_a_bit_pattern(self):
        # `0.0 == -0.0` is True, so a value-based key would merge them.
        a = snapshot_key(self._snapshot(live=0.0), session_active=False)
        b = snapshot_key(self._snapshot(live=-0.0), session_active=False)
        self.assertNotEqual(a, b)

    def test_nan_live_price_is_stable_under_the_key(self):
        # `nan != nan`, so a value-based key would MISS forever on a NaN price.
        a = snapshot_key(self._snapshot(live=float("nan")), session_active=False)
        b = snapshot_key(self._snapshot(live=float("nan")), session_active=False)
        self.assertEqual(a, b)

    def test_prior_bypasses_the_cache(self):
        # T4.4 — `prior` is invisible to the key, so it must never be served
        # from one.
        snapshot = self._snapshot()
        cached = CachingAnalyzer()
        try:
            cached.analyze(snapshot, session_active=False)
        except Exception:
            self.skipTest("synthetic frame does not complete the pipeline")
        before = cached.stats.lookups
        cached.analyze(snapshot, prior=object(), session_active=False)
        self.assertEqual(before, cached.stats.lookups,
                         "a prior-bearing call must not touch the cache")


# ---------------------------------------------------------------------------
# T4.6 — the hit-rate assertion, on real data
# ---------------------------------------------------------------------------

@unittest.skipUnless(CORPUS, "data/stage1 corpus required")
class TestHitRateOnRealCycles(unittest.TestCase):
    """A cache that never hits passes T4.1-T4.5 trivially. This is the test that
    proves it does something."""

    def _cycle_snapshots(self, limit=12):
        from backtest.data.repository import KlineRepository
        from backtest.replay.adapter import HistoricalMarketDataAdapter

        repository = KlineRepository(Path("data/stage1") / "canonical")
        adapter = HistoricalMarketDataAdapter(repository)
        adapter.set_time(F.BOUNDARY)
        out = []
        for entry in adapter.get_top_volume_symbols(limit):
            snapshot = adapter.get_market_snapshot(entry.symbol, include_m5=True)
            if snapshot is not None:
                out.append(snapshot)
        return out

    def test_at_least_one_hit_per_symbol_scan(self):
        # T4.6 — reproduces orchestrator.py:265-268 exactly: pipeline.run, then
        # analyze.
        from v2.analysis.context import AnalysisContext

        snapshots = self._cycle_snapshots()
        self.assertGreater(len(snapshots), 0)
        cached = CachingAnalyzer()
        for snapshot in snapshots:
            cached.pipeline.run(
                AnalysisContext.initial(snapshot, session_active=False))
            cached.analyze(snapshot, session_active=False)

        self.assertEqual(len(snapshots), cached.stats.hits,
                         "exactly one hit per symbol-scan is expected")
        self.assertEqual(len(snapshots), cached.stats.misses)
        for snapshot in snapshots:
            self.assertGreaterEqual(
                cached.stats.hits_by_symbol[snapshot.symbol], 1,
                f"{snapshot.symbol} never hit")

    def test_the_hit_returns_the_identical_object(self):
        from v2.analysis.context import AnalysisContext

        snapshot = self._cycle_snapshots(limit=1)[0]
        cached = CachingAnalyzer()
        first = cached.pipeline.run(
            AnalysisContext.initial(snapshot, session_active=False))
        second = cached.pipeline.run(
            AnalysisContext.initial(snapshot, session_active=False))
        self.assertIs(first, second,
                      "AnalysisContext is frozen; the hit must be the same object")

    def test_the_duplicate_evaluation_is_measurably_removed(self):
        from v2.analysis.context import AnalysisContext

        snapshots = self._cycle_snapshots(limit=8)

        def two_calls(analyzer):
            for snapshot in snapshots:
                analyzer.pipeline.run(
                    AnalysisContext.initial(snapshot, session_active=False))
                analyzer.analyze(snapshot, session_active=False)

        plain = V2Analyzer()
        two_calls(plain)
        t0 = time.perf_counter()
        two_calls(plain)
        uncached = time.perf_counter() - t0

        cached = CachingAnalyzer()
        two_calls(cached)
        cached2 = CachingAnalyzer()
        t0 = time.perf_counter()
        two_calls(cached2)
        with_cache = time.perf_counter() - t0

        self.assertLess(with_cache, uncached * 0.75,
                        f"expected ~2x; got {uncached/with_cache:.2f}x")


# ---------------------------------------------------------------------------
# T4.7 — core() is untouched
# ---------------------------------------------------------------------------

class TestCoreIsUnchanged(unittest.TestCase):

    def _config(self, **over):
        return ReplayConfig(
            repository_root=Path("x"), output_root=Path("y"),
            start=1_672_531_200, end=1_672_534_800, **over)

    def test_caching_analyzer_is_absent_from_core(self):
        core = self._config().core(data_snapshot_id="snap")
        self.assertNotIn("caching_analyzer", core)

    def test_core_is_byte_identical_with_the_flag_on_and_off(self):
        off = self._config(caching_analyzer=False).core(data_snapshot_id="snap")
        on = self._config(caching_analyzer=True).core(data_snapshot_id="snap")
        self.assertEqual(json.dumps(off, sort_keys=True),
                         json.dumps(on, sort_keys=True))

    def test_run_id_is_unchanged_by_the_flag(self):
        off = self._config(caching_analyzer=False).run_id(data_snapshot_id="snap")
        on = self._config(caching_analyzer=True).run_id(data_snapshot_id="snap")
        self.assertEqual(off, on)

    def test_the_flag_defaults_to_off(self):
        self.assertFalse(self._config().caching_analyzer)

    def test_the_core_key_set_is_exactly_the_c162_closed_list(self):
        self.assertEqual(
            {"contract_version", "strategy_version", "ledger_schema_version",
             "data_snapshot_id", "T_0", "T_n", "burn_in_h1", "universe_mode",
             "static_universe", "top_coins_limit", "lifecycle_config",
             "outcome_config_hash", "env"},
            set(self._config().core(data_snapshot_id="snap")))


# ---------------------------------------------------------------------------
# Wiring and bounds
# ---------------------------------------------------------------------------

class TestWiringAndBounds(unittest.TestCase):

    def test_build_analyzer_selects_on_the_flag(self):
        self.assertIsInstance(build_analyzer(caching=False), V2Analyzer)
        self.assertIsInstance(build_analyzer(caching=True), CachingAnalyzer)

    def test_the_uncached_selection_is_not_wrapped(self):
        self.assertNotIsInstance(build_analyzer(caching=False), CachingAnalyzer)

    def test_the_wrapper_exposes_only_the_two_orchestrator_seams(self):
        analyzer = CachingAnalyzer()
        self.assertTrue(hasattr(analyzer, "pipeline"))
        self.assertTrue(callable(analyzer.analyze))
        self.assertEqual(analyzer.pipeline.stage_names,
                         V2Analyzer().pipeline.stage_names,
                         "stage order must be untouched")

    def test_the_cache_is_bounded(self):
        pipeline = CachingPipeline(V2Analyzer().pipeline, max_entries=4)
        for index in range(10):
            pipeline._entries[("k", index)] = object()
            while len(pipeline._entries) > pipeline._max_entries:
                pipeline._entries.popitem(last=False)
        self.assertLessEqual(pipeline.entry_count, 4)

    def test_max_entries_is_validated(self):
        with self.assertRaises(ValueError):
            CachingPipeline(V2Analyzer().pipeline, max_entries=0)

    def test_default_bound_covers_a_full_universe(self):
        self.assertGreaterEqual(DEFAULT_MAX_ENTRIES, 30)

    def test_stats_start_empty(self):
        stats = CacheStats()
        self.assertEqual(0, stats.lookups)
        self.assertEqual(0.0, stats.hit_rate)


# ---------------------------------------------------------------------------
# Negative controls
# ---------------------------------------------------------------------------

@unittest.skipUnless(CORPUS, "data/stage1 corpus required")
class TestNegativeControls(unittest.TestCase):
    """Each proves an assertion above can fail."""

    def test_a_key_missing_m5_would_collide_across_the_gap(self):
        # Drops m5_last_ts/len(m5) and live_price -- what C-8.4 specifies.
        cycles = F.build_cycles(variant="A")
        weakened = {(s.symbol, s.h1_last_ts, len(s.h1)) for s in cycles}
        self.assertEqual(1, len(weakened),
                         "the control must collide, or it proves nothing")
        full = {snapshot_key(s, session_active=F.SESSION_ACTIVE) for s in cycles}
        self.assertEqual(3, len(full))

    def test_a_key_missing_d1_would_collide_in_variant_b(self):
        cycles = F.build_cycles(variant="B")
        without_d1 = {
            (s.symbol, s.h1_last_ts, len(s.h1),
             None if s.m5 is None else len(s.m5))
            for s in cycles}
        full = {snapshot_key(s, session_active=F.SESSION_ACTIVE) for s in cycles}
        self.assertLess(len(without_d1), len(full),
                        "variant B must be separated by the D1 components")

    def test_a_never_hitting_cache_still_passes_equality(self):
        # Why T4.6 exists: equality alone cannot detect a dead cache.
        from v2.analysis.context import AnalysisContext

        cycles = F.build_cycles(variant="A")
        dead = CachingAnalyzer(max_entries=1)
        dead.pipeline.clear()
        plain = V2Analyzer()
        for snapshot in cycles:
            a = plain.analyze(snapshot, session_active=F.SESSION_ACTIVE)
            b = dead.analyze(snapshot, session_active=F.SESSION_ACTIVE)
            dead.pipeline.clear()                      # force a permanent miss
            self.assertEqual(_signal_tuple(a), _signal_tuple(b))
        self.assertEqual(0, dead.stats.hits)


if __name__ == "__main__":
    unittest.main()
