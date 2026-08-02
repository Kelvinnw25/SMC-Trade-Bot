"""Tests for the Replay Driver (Milestone M7).

Hermetic and fast: a small synthetic repository, six-to-ten hour windows, and the
**real** production `Orchestrator`, `V2Analyzer` and `Reconciler` running over it.
Nothing here stubs the strategy — the point of the milestone is that the strategy
is untouched, so the tests exercise it for real.

Candle fixtures are reused from the M6 adapter suite rather than restated, so the
two milestones cannot drift apart on what a candle series looks like.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backtest.data.repository import dataset_path
from backtest.replay import (
    ConfigError, HEALTH_FILENAME, HistoricalMarketDataAdapter,
    MANIFEST_FILENAME, RecordingNotifier, ReplayAborted, ReplayConfig,
    ReplayDataError, ReplayDriver, ReplayPersistence, RunWindowError,
)
from backtest.replay.driver import WARMUP_D1_ROWS, WARMUP_H1_ROWS
from tests.backtest.test_historical_adapter import (
    DAY_MS, HOUR_MS, M5_MS, ending_at, write_repository,
)
from v2.app.orchestrator import Orchestrator
from v2.lifecycle.rules import LifecycleConfig

#: 2026-03-01T00:00:00Z. History runs ten hours past it so a multi-cycle window
#: stays inside coverage.
T0 = 1_772_323_200
DATA_END = T0 + 10 * 3600


def datasets(symbol: str = "BTCUSDT", *, qv: float = 1000.0,
             h1_count: int = 600) -> dict:
    return {
        (symbol, "1d"): ending_at(DAY_MS, 130, end_open_ms=T0 * 1000, qv=qv),
        (symbol, "1h"): ending_at(HOUR_MS, h1_count,
                                  end_open_ms=DATA_END * 1000, qv=qv),
        (symbol, "5m"): ending_at(M5_MS, 450, end_open_ms=DATA_END * 1000, qv=qv),
    }


class DriverCase(unittest.TestCase):
    """A repository plus an output root, both temporary."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo_root = self.tmp / "repo"
        write_repository(self.repo_root, datasets())

    def config(self, *, out: str = "out", cycles: int = 6, **kw) -> ReplayConfig:
        params = dict(
            repository_root=self.repo_root,
            output_root=self.tmp / out,
            start=T0,
            end=T0 + (cycles - 1) * 3600,
            burn_in_h1=0,
        )
        params.update(kw)
        return ReplayConfig(**params)

    def run_replay(self, **kw):
        driver = ReplayDriver(self.config(**kw))
        return driver, driver.run()


# ==========================================================================
# TASK 1 / configuration and run identity
# ==========================================================================

class TestConfig(unittest.TestCase):

    def config(self, **kw) -> ReplayConfig:
        params = dict(repository_root=Path("r"), output_root=Path("o"),
                      start=T0, end=T0 + 5 * 3600)
        params.update(kw)
        return ReplayConfig(**params)

    def test_boundaries_are_hourly_inclusive_and_gapless(self):
        boundaries = self.config().boundaries
        self.assertEqual(boundaries[0], T0)
        self.assertEqual(boundaries[-1], T0 + 5 * 3600)
        self.assertEqual(len(boundaries), 6)
        self.assertTrue(all(b - a == 3600
                            for a, b in zip(boundaries, boundaries[1:])))

    def test_a_single_boundary_window_is_one_cycle(self):
        self.assertEqual(self.config(end=T0).cycle_count, 1)

    def test_a_misaligned_start_is_refused(self):
        with self.assertRaises(ConfigError):
            self.config(start=T0 + 1)

    def test_a_misaligned_end_is_refused(self):
        with self.assertRaises(ConfigError):
            self.config(end=T0 + 59)

    def test_an_inverted_window_is_refused(self):
        with self.assertRaises(ConfigError):
            self.config(start=T0, end=T0 - 3600)

    def test_static_mode_requires_a_universe(self):
        with self.assertRaises(ConfigError):
            self.config(universe_mode="static")

    def test_a_static_universe_without_static_mode_is_refused(self):
        """The universe policy is stated once, never implied twice."""
        with self.assertRaises(ConfigError):
            self.config(static_universe=("BTCUSDT",))

    def test_an_unknown_universe_mode_is_refused(self):
        with self.assertRaises(ConfigError):
            self.config(universe_mode="whatever")

    def test_simulated_time_is_the_window_span(self):
        self.assertEqual(self.config().simulated_seconds, 5 * 3600)

    def test_burn_in_defaults_to_fourteen_days(self):
        self.assertEqual(self.config().burn_in_h1, 336)
        self.assertEqual(self.config().burn_in_until, T0 + 336 * 3600)


class TestRunIdentity(unittest.TestCase):

    def config(self, **kw) -> ReplayConfig:
        params = dict(repository_root=Path("r"), output_root=Path("o"),
                      start=T0, end=T0 + 5 * 3600)
        params.update(kw)
        return ReplayConfig(**params)

    def run_id(self, *, snapshot="snap", **kw) -> str:
        return self.config(**kw).run_id(data_snapshot_id=snapshot)

    def test_the_run_id_is_deterministic(self):
        self.assertEqual(self.run_id(), self.run_id())

    def test_the_run_id_is_prefixed_and_short(self):
        value = self.run_id()
        self.assertTrue(value.startswith("replay-"))
        self.assertEqual(len(value), len("replay-") + 16)

    def test_every_input_that_can_change_a_result_changes_the_run_id(self):
        base = self.run_id()
        variants = {
            "snapshot": self.run_id(snapshot="other"),
            "T_0": self.run_id(start=T0 + 3600),
            "T_n": self.run_id(end=T0 + 6 * 3600),
            "burn_in": self.run_id(burn_in_h1=1),
            "limit": self.run_id(top_coins_limit=5),
            "strategy": self.run_id(strategy_version="v9"),
            "outcome_cfg": self.run_id(outcome_config_hash="abc"),
            "universe": self.run_id(universe_mode="static",
                                    static_universe=("BTCUSDT",)),
            "lifecycle": self.run_id(
                lifecycle_config=replace(LifecycleConfig(), max_open_positions=3)),
        }
        for label, value in variants.items():
            self.assertNotEqual(base, value, label)
        self.assertEqual(len(set(variants.values())), len(variants))

    def test_the_output_directory_is_deterministic(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            write_repository(root / "repo", datasets())
            config = ReplayConfig(repository_root=root / "repo",
                                  output_root=root / "out",
                                  start=T0, end=T0 + 3600)
            first = ReplayDriver(config).run_dir
            second = ReplayDriver(config).run_dir
            self.assertEqual(first, second)
            self.assertEqual(first.parent.name, "runs")


# ==========================================================================
# TASK 3 — run window validation
# ==========================================================================

class TestRunWindowValidation(DriverCase):

    def test_a_valid_window_reports_its_warmup(self):
        report = ReplayDriver(self.config()).validate()
        self.assertEqual(report.symbols_total, 1)
        self.assertEqual(report.eligible_at_start, ("BTCUSDT",))
        self.assertEqual(report.fully_warmed, ("BTCUSDT",))
        self.assertEqual(report.guard_skipped_at_start, ())
        self.assertEqual(report.last_boundary_with_data, DATA_END + 3600)

    def test_an_empty_repository_is_refused(self):
        write_repository(self.tmp / "empty", {("BTCUSDT", "1h"):
                                              ending_at(HOUR_MS, 2)})
        config = self.config()
        config = replace(config, repository_root=self.tmp / "empty")
        with self.assertRaises(RunWindowError) as caught:
            ReplayDriver(config).validate()
        self.assertIn("sufficiency guards", str(caught.exception))

    def test_a_missing_repository_is_refused_at_construction(self):
        config = replace(self.config(), repository_root=self.tmp / "nowhere")
        with self.assertRaises(RunWindowError):
            ReplayDriver(config)

    def test_a_start_before_the_data_is_refused(self):
        config = replace(self.config(), start=T0 - 5000 * 3600,
                         end=T0 - 4999 * 3600)
        with self.assertRaises(RunWindowError) as caught:
            ReplayDriver(config).validate()
        self.assertIn("T_0", str(caught.exception))

    def test_an_end_past_the_data_is_refused(self):
        config = replace(self.config(), end=DATA_END + 50 * 3600)
        with self.assertRaises(RunWindowError) as caught:
            ReplayDriver(config).validate()
        self.assertIn("past the end of history", str(caught.exception))

    def test_the_last_replayable_boundary_is_accepted(self):
        """History ends when its final candle closes — that boundary is
        replayable, the next one is not."""
        ReplayDriver(replace(self.config(), end=DATA_END + 3600)).validate()
        with self.assertRaises(RunWindowError):
            ReplayDriver(replace(self.config(), end=DATA_END + 7200)).validate()

    def test_a_short_symbol_is_reported_as_guard_skipped_not_fatal(self):
        """C-5.0: warmup is enforced per symbol by the existing guards. A thin
        symbol alongside a good one is skipped, not a reason to refuse the run."""
        combined = dict(datasets("BTCUSDT"))
        combined[("NEWUSDT", "1h")] = ending_at(HOUR_MS, 20,
                                                end_open_ms=DATA_END * 1000)
        combined[("NEWUSDT", "1d")] = ending_at(DAY_MS, 3,
                                                end_open_ms=T0 * 1000)
        write_repository(self.tmp / "mixed", combined)
        report = ReplayDriver(
            replace(self.config(), repository_root=self.tmp / "mixed")).validate()
        self.assertEqual(report.eligible_at_start, ("BTCUSDT",))
        self.assertEqual(report.guard_skipped_at_start, ("NEWUSDT",))
        self.assertEqual(report.to_dict()["guard_skips_at_T0"], 1)

    def test_validation_runs_no_cycles_and_writes_nothing(self):
        config = self.config()
        ReplayDriver(config).validate()
        self.assertFalse((config.output_root / "runs").exists())

    def test_warmup_thresholds_are_the_closed_candle_equivalents(self):
        """C-6.11's 500/120/200 are live's raw request sizes, which include the
        forming candle it strips."""
        self.assertEqual((WARMUP_H1_ROWS, WARMUP_D1_ROWS), (499, 119))


# ==========================================================================
# TASK 2 — the historical time model
# ==========================================================================

class _SpyAdapter(HistoricalMarketDataAdapter):
    """Records every boundary the driver positions it at."""

    instances: list["_SpyAdapter"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.boundaries: list[int] = []
        self.clock_at_set: list[float] = []
        _SpyAdapter.instances.append(self)

    def set_time(self, boundary: int) -> None:
        super().set_time(boundary)
        self.boundaries.append(int(boundary))


class _FailingAdapter(HistoricalMarketDataAdapter):
    """Behaves normally until the third cycle, then reports a dataset error the
    way a corrupt `.npy` discovered mid-run would."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cycles = -1

    def set_time(self, boundary: int) -> None:
        super().set_time(boundary)
        self._cycles += 1

    def get_market_snapshot(self, symbol, *, include_m5=False):
        if self._cycles >= 2:
            self.counters.data_errors += 1
            raise ReplayDataError(f"simulated corruption for {symbol}")
        return super().get_market_snapshot(symbol, include_m5=include_m5)


class TestTimeModel(DriverCase):

    def setUp(self) -> None:
        super().setUp()
        _SpyAdapter.instances = []

    def run_with_spy(self, **kw):
        with mock.patch("backtest.replay.driver.HistoricalMarketDataAdapter",
                        _SpyAdapter):
            driver = ReplayDriver(self.config(**kw))
            result = driver.run()
        return result, _SpyAdapter.instances[-1]

    def test_one_step_per_closed_h1_boundary(self):
        result, spy = self.run_with_spy(cycles=6)
        self.assertEqual(result.cycles, 6)
        self.assertEqual(spy.boundaries,
                         [T0 + index * 3600 for index in range(6)])

    def test_boundaries_are_strictly_increasing_with_no_gaps(self):
        _, spy = self.run_with_spy(cycles=8)
        deltas = {b - a for a, b in zip(spy.boundaries, spy.boundaries[1:])}
        self.assertEqual(deltas, {3600})

    def test_the_clock_reads_the_boundary_plus_one_second(self):
        """C-5.2. Asserted by the driver every cycle, so a run that completes
        has proved it for every boundary."""
        clocks: list[float] = []
        real = ReplayDriver._assert_clock

        def spy(clock, boundary):
            clocks.append(clock.timestamp())
            real(clock, boundary)

        with mock.patch.object(ReplayDriver, "_assert_clock",
                               staticmethod(spy)):
            self.run_replay(cycles=5)
        self.assertEqual(clocks,
                         [float(T0 + index * 3600 + 1) for index in range(5)])

    def test_clock_drift_aborts_the_run(self):
        with mock.patch("backtest.replay.driver.FakeClock") as fake:
            fake.return_value = mock.Mock(timestamp=mock.Mock(return_value=0.0))
            with self.assertRaises(ReplayAborted) as caught:
                self.run_replay()
        self.assertIn("drift", str(caught.exception))

    def test_replay_never_sleeps(self):
        with mock.patch("time.sleep") as sleeper:
            self.run_replay(cycles=4)
        sleeper.assert_not_called()

    def test_replay_reads_no_wall_clock(self):
        """The only wall-clock reads are the driver's own wall-time measurement
        and the manifest stamp — never anything a cycle consumes."""
        with mock.patch("v2.infra.clock.SystemClock.now") as now:
            self.run_replay(cycles=3)
        now.assert_not_called()

    def test_simulated_time_is_reported(self):
        _, result = self.run_replay(cycles=6)
        self.assertEqual(result.simulated_seconds, 5 * 3600)
        self.assertEqual(result.cycles, 6)


# ==========================================================================
# TASK 4 — dependency wiring
# ==========================================================================

class TestWiring(DriverCase):

    def test_the_driver_builds_the_production_orchestrator(self):
        built = {}
        real = Orchestrator.__init__

        def capture(self, **kwargs):
            built.update(kwargs)
            real(self, **kwargs)

        with mock.patch.object(Orchestrator, "__init__", capture):
            driver, _ = self.run_replay()

        self.assertIsInstance(built["market_data"], HistoricalMarketDataAdapter)
        self.assertIsInstance(built["persistence"], ReplayPersistence)
        self.assertIsInstance(built["notifier"], RecordingNotifier)
        self.assertEqual(type(built["clock"]).__name__, "FakeClock")
        self.assertEqual(built["source"], "replay")
        self.assertEqual(built["run_id"], driver.run_id)
        self.assertEqual(built["data_snapshot_id"], driver.data_snapshot_id)

    def test_the_orchestrator_class_is_the_production_one(self):
        import v2.app.orchestrator as production
        from backtest.replay import driver as replay_driver
        self.assertIs(replay_driver.Orchestrator, production.Orchestrator)

    def test_replay_starts_from_empty_lifecycle_memory(self):
        """C-5.7.3: never from live `active_pois.json`."""
        persistence = ReplayPersistence(self.tmp / "run")
        self.assertEqual(persistence.load_active_signals(), {})
        self.assertEqual(persistence.load_killed_theses(), {})

    def test_the_notifier_records_and_never_raises(self):
        notifier = RecordingNotifier(self.tmp / "run")
        notifier.set_cycle(run_id="r", cycle_index=2)
        notifier.send(mock.Mock(spec=[]))
        self.assertEqual(len(notifier.alerts), 1)

    def test_nothing_is_written_outside_the_run_directory(self):
        driver, result = self.run_replay()
        for name in ("active_pois.json", "killed_thesis.json",
                     "signal_ledger.jsonl"):
            self.assertFalse((self.tmp / name).exists(), name)
            self.assertFalse((Path.cwd() / "runs").exists())
        self.assertTrue(result.run_dir.is_relative_to(self.tmp))


# ==========================================================================
# TASK 5 — determinism
# ==========================================================================

class TestDeterminism(DriverCase):

    VOLATILE = ("generated_at", "wall_seconds")

    def test_two_runs_produce_identical_outputs(self):
        first = ReplayDriver(self.config(out="a")).run()
        second = ReplayDriver(self.config(out="b")).run()

        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.ledger_rows, second.ledger_rows)
        self.assertGreater(first.ledger_rows, 0)

        produced = sorted(p.name for p in first.run_dir.iterdir())
        self.assertEqual(produced, sorted(p.name for p in second.run_dir.iterdir()))

        for name in produced:
            left = (first.run_dir / name).read_bytes()
            right = (second.run_dir / name).read_bytes()
            if name == MANIFEST_FILENAME:
                self.assertEqual(self._stable(left), self._stable(right))
            else:
                self.assertEqual(left, right, name)

    def test_the_ledger_lifecycle_and_audit_files_all_match(self):
        first = ReplayDriver(self.config(out="a")).run()
        second = ReplayDriver(self.config(out="b")).run()
        for name in ("signal_ledger.jsonl", "active_pois.json",
                     "killed_thesis.json", HEALTH_FILENAME):
            left = first.run_dir / name
            self.assertTrue(left.is_file(), name)
            self.assertEqual(left.read_bytes(),
                             (second.run_dir / name).read_bytes(), name)

    def test_the_manifest_has_exactly_two_non_reproducible_keys(self):
        first = ReplayDriver(self.config(out="a")).run()
        second = ReplayDriver(self.config(out="b")).run()
        left = json.loads((first.run_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        right = json.loads((second.run_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        differing = {key for key in left
                     if json.dumps(left[key], sort_keys=True)
                     != json.dumps(right[key], sort_keys=True)}
        self.assertLessEqual(differing, set(self.VOLATILE))

    def test_cycle_order_is_identical(self):
        first = ReplayDriver(self.config(out="a")).run()
        second = ReplayDriver(self.config(out="b")).run()
        self.assertEqual([h.scan_T for h in first.health],
                         [h.scan_T for h in second.health])
        self.assertEqual([h.cycle_index for h in first.health],
                         [h.cycle_index for h in second.health])

    def test_decisions_are_identical(self):
        first = ReplayDriver(self.config(out="a")).run()
        second = ReplayDriver(self.config(out="b")).run()
        self.assertEqual(self._decisions(first), self._decisions(second))

    def test_a_rerun_into_a_finished_directory_is_refused(self):
        """The ledger appends; re-running in place would silently double it."""
        config = self.config()
        ReplayDriver(config).run()
        with self.assertRaises(RunWindowError) as caught:
            ReplayDriver(config).run()
        self.assertIn("overwrite=True", str(caught.exception))

    def test_overwriting_reproduces_the_same_bytes(self):
        config = self.config()
        first = ReplayDriver(config).run()
        ledger = (first.run_dir / "signal_ledger.jsonl").read_bytes()
        second = ReplayDriver(config, overwrite=True).run()
        self.assertEqual((second.run_dir / "signal_ledger.jsonl").read_bytes(),
                         ledger)

    def _stable(self, payload: bytes) -> str:
        data = json.loads(payload)
        for key in self.VOLATILE:
            data.pop(key, None)
        return json.dumps(data, sort_keys=True)

    @staticmethod
    def _decisions(result) -> list[tuple]:
        rows = (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        return [(r["symbol"], r["decision"], r["score"], r["h1_last_ts"])
                for r in (json.loads(line) for line in rows if line)]


# ==========================================================================
# TASK 6 — universe replay
# ==========================================================================

class TestUniverse(DriverCase):

    def test_the_universe_is_the_historical_volume_ranking(self):
        combined = {}
        for symbol, qv in (("AAAUSDT", 100.0), ("BBBUSDT", 900.0),
                           ("CCCUSDT", 500.0)):
            combined.update(datasets(symbol, qv=qv))
        write_repository(self.tmp / "many", combined)
        config = replace(self.config(), repository_root=self.tmp / "many",
                         top_coins_limit=2)
        result = ReplayDriver(config).run()
        symbols = {json.loads(line)["symbol"] for line in
                   (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                   if line}
        self.assertEqual(symbols, {"BBB/USDT", "CCC/USDT"})

    def test_the_universe_rank_reaches_the_ledger(self):
        result = ReplayDriver(self.config()).run()
        rows = [json.loads(line) for line in
                (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if line]
        self.assertTrue(all(row["universe_rank"] == 1 for row in rows))

    def test_a_static_universe_pins_the_scan(self):
        combined = {}
        for symbol, qv in (("AAAUSDT", 100.0), ("BBBUSDT", 900.0)):
            combined.update(datasets(symbol, qv=qv))
        write_repository(self.tmp / "many", combined)
        config = replace(self.config(), repository_root=self.tmp / "many",
                         universe_mode="static", static_universe=("AAAUSDT",))
        result = ReplayDriver(config).run()
        symbols = {json.loads(line)["symbol"] for line in
                   (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                   if line}
        self.assertEqual(symbols, {"AAA/USDT"})

    def test_a_symbol_without_a_full_trailing_day_never_enters_the_universe(self):
        combined = dict(datasets("BTCUSDT"))
        combined[("NEWUSDT", "1h")] = ending_at(HOUR_MS, 20,
                                                end_open_ms=DATA_END * 1000)
        combined[("NEWUSDT", "1d")] = ending_at(DAY_MS, 130,
                                                end_open_ms=T0 * 1000)
        write_repository(self.tmp / "mixed", combined)
        result = ReplayDriver(
            replace(self.config(), repository_root=self.tmp / "mixed")).run()
        symbols = {json.loads(line)["symbol"] for line in
                   (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                   if line}
        self.assertEqual(symbols, {"BTC/USDT"})


# ==========================================================================
# TASK 7 — replay outputs
# ==========================================================================

class TestOutputs(DriverCase):

    def test_the_expected_artifacts_are_produced(self):
        _, result = self.run_replay()
        for name in (MANIFEST_FILENAME, HEALTH_FILENAME, "signal_ledger.jsonl",
                     "active_pois.json", "killed_thesis.json"):
            self.assertTrue((result.run_dir / name).is_file(), name)

    def test_the_result_reports_run_identity_and_simulated_time(self):
        driver, result = self.run_replay(cycles=6)
        self.assertEqual(result.run_id, driver.run_id)
        self.assertEqual(result.data_snapshot_id, driver.data_snapshot_id)
        self.assertEqual(result.cycles, 6)
        self.assertEqual(result.cycles_processed, 6)
        self.assertEqual(result.simulated_seconds, 5 * 3600)
        self.assertGreater(result.ledger_rows, 0)

    def test_no_outcome_or_metric_is_produced(self):
        """The Outcome Engine owns those, and does not exist yet."""
        _, result = self.run_replay()
        names = {p.name for p in result.run_dir.iterdir()}
        for forbidden in ("outcomes.jsonl", "metrics.json", "report.md",
                          "segments.csv", "trades.jsonl"):
            self.assertNotIn(forbidden, names)
        manifest = json.loads((result.run_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        for forbidden in ("pnl", "profit", "loss", "win_rate", "trades",
                          "metrics"):
            self.assertNotIn(forbidden, json.dumps(manifest).lower())

    def test_the_manifest_carries_the_run_core_and_declared_biases(self):
        _, result = self.run_replay()
        manifest = json.loads((result.run_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        core = manifest["core"]
        for key in ("contract_version", "strategy_version",
                    "ledger_schema_version", "data_snapshot_id", "T_0", "T_n",
                    "burn_in_h1", "universe_mode", "top_coins_limit",
                    "lifecycle_config", "outcome_config_hash", "env"):
            self.assertIn(key, core)
        self.assertEqual(manifest["run_id"], result.run_id)
        self.assertTrue(manifest["declared_biases"])
        self.assertIn("warmup", manifest)

    def test_health_has_one_line_per_cycle(self):
        _, result = self.run_replay(cycles=7)
        lines = [json.loads(line) for line in
                 (result.run_dir / HEALTH_FILENAME).read_text(encoding="utf-8").splitlines()
                 if line]
        self.assertEqual(len(lines), 7)
        self.assertEqual([line["cycle_index"] for line in lines], list(range(7)))
        self.assertEqual([line["scan_T"] for line in lines],
                         [T0 + index * 3600 for index in range(7)])

    def test_a_healthy_run_leaves_nothing_unaccounted(self):
        """C-15.8: universe - none_skips - ledger_rows == 0 when every symbol in
        the universe was analysed and recorded."""
        _, result = self.run_replay()
        self.assertTrue(all(h.unaccounted == 0 for h in result.health))
        self.assertTrue(all(h.data_errors == 0 for h in result.health))

    def test_ledger_rows_carry_replay_lineage(self):
        driver, result = self.run_replay()
        rows = [json.loads(line) for line in
                (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if line]
        self.assertEqual({row["source"] for row in rows}, {"replay"})
        self.assertEqual({row["run_id"] for row in rows}, {driver.run_id})
        self.assertEqual({row["data_snapshot_id"] for row in rows},
                         {driver.data_snapshot_id})
        self.assertEqual([row["cycle_index"] for row in rows], list(range(6)))
        self.assertEqual({row["schema_version"] for row in rows}, {2})

    def test_burn_in_is_flagged_per_cycle(self):
        _, result = self.run_replay(cycles=4, burn_in_h1=2)
        self.assertEqual([h.in_burn_in for h in result.health],
                         [True, True, False, False])

    def test_the_console_is_discarded_unless_requested(self):
        _, result = self.run_replay()
        self.assertFalse((result.run_dir / "console.log").exists())
        _, captured = self.run_replay(out="c", capture_console=True)
        self.assertTrue((captured.run_dir / "console.log").is_file())


# ==========================================================================
# TASK 8 — error handling
# ==========================================================================

class TestErrorHandling(DriverCase):

    def test_pre_existing_corruption_is_caught_before_any_cycle_runs(self):
        """Validation opens every dataset, so a corrupt repository fails the
        window check and costs zero cycles."""
        config = self.config()
        dataset_path(self.repo_root, "BTCUSDT", "1h").write_bytes(b"garbage")
        with self.assertRaises(RunWindowError) as caught:
            ReplayDriver(config).run()
        self.assertIn("BTCUSDT", str(caught.exception))
        self.assertFalse((config.output_root / "runs").exists())

    def test_corruption_discovered_mid_run_aborts_the_replay(self):
        """The orchestrator swallows every per-symbol exception, so a
        `ReplayDataError` never reaches the driver by propagation. The adapter's
        counter is the only channel — this proves the driver watches it
        (finding A51 / C-15.8)."""
        config = self.config()
        driver = ReplayDriver(config)
        with mock.patch("backtest.replay.driver.HistoricalMarketDataAdapter",
                        _FailingAdapter):
            with self.assertRaises(ReplayAborted) as caught:
                driver.run()
        message = str(caught.exception)
        self.assertIn("dataset error", message)
        self.assertIn("cycle 2", message)

    def test_health_is_written_even_when_the_run_aborts(self):
        driver = ReplayDriver(self.config())
        with mock.patch("backtest.replay.driver.HistoricalMarketDataAdapter",
                        _FailingAdapter):
            with self.assertRaises(ReplayAborted):
                driver.run()
        health = driver.run_dir / HEALTH_FILENAME
        self.assertTrue(health.is_file())
        lines = [json.loads(line) for line in
                 health.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 3)          # the failing cycle is recorded
        self.assertEqual(lines[-1]["cycle_index"], 2)
        self.assertGreater(lines[-1]["data_errors"], 0)
        self.assertEqual([line["data_errors"] for line in lines[:2]], [0, 0])

    def test_a_missing_symbol_skips_the_symbol_and_the_run_continues(self):
        """A symbol ranked into the universe but absent from another timeframe
        is skipped, exactly as live skips a symbol whose fetch returns None."""
        combined = dict(datasets("BTCUSDT"))
        combined[("GHOSTUSDT", "1h")] = ending_at(HOUR_MS, 600,
                                                  end_open_ms=DATA_END * 1000,
                                                  qv=9999.0)
        write_repository(self.tmp / "ghost", combined)
        result = ReplayDriver(
            replace(self.config(), repository_root=self.tmp / "ghost")).run()
        symbols = {json.loads(line)["symbol"] for line in
                   (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                   if line}
        self.assertEqual(symbols, {"BTC/USDT"})
        self.assertEqual(result.cycles, 6)
        self.assertTrue(all(h.snapshots_none == 1 for h in result.health))

    def test_insufficient_history_skips_the_symbol_per_the_guards(self):
        combined = dict(datasets("BTCUSDT"))
        combined[("THINUSDT", "1h")] = ending_at(HOUR_MS, 40,
                                                 end_open_ms=DATA_END * 1000,
                                                 qv=9999.0)
        combined[("THINUSDT", "1d")] = ending_at(DAY_MS, 130,
                                                 end_open_ms=T0 * 1000)
        combined[("THINUSDT", "5m")] = ending_at(M5_MS, 450,
                                                 end_open_ms=DATA_END * 1000)
        write_repository(self.tmp / "thin", combined)
        result = ReplayDriver(
            replace(self.config(), repository_root=self.tmp / "thin")).run()
        rows = [json.loads(line) for line in
                (result.run_dir / "signal_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if line]
        self.assertEqual({row["symbol"] for row in rows}, {"BTC/USDT"})
        # The guard-skipped symbol shows up as unaccounted, never as silence.
        self.assertTrue(all(h.unaccounted == 1 for h in result.health))

    def test_a_window_error_names_what_is_wrong(self):
        config = replace(self.config(), end=DATA_END + 100 * 3600)
        with self.assertRaises(RunWindowError) as caught:
            ReplayDriver(config).run()
        message = str(caught.exception)
        self.assertIn("past the end of history", message)
        self.assertIn("2026-03-01", message)


# ==========================================================================
# TASK 10 — production is untouched
# ==========================================================================

class TestProductionUntouched(unittest.TestCase):

    def test_production_never_imports_replay(self):
        from tests.backtest.test_historical_adapter import imported_modules
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"),
                                 f"{path} imports {module}")

    def test_the_driver_contains_no_strategy_logic(self):
        from tests.backtest.test_historical_adapter import imported_modules
        allowed_v2 = {
            "v2.analysis.engine", "v2.app.orchestrator", "v2.app.ledger",
            "v2.infra.clock", "v2.infra.persistence", "v2.infra.market_data",
            "v2.lifecycle.rules", "v2.domain.market", "v2.domain.active_signal",
            "v2.domain.killed_thesis", "v2.domain.lifecycle",
            "v2.domain.ob_audit", "v2.domain.signal_ledger", "v2.infra.config",
            # C-8.3 spells the wiring out: the driver constructs
            # `RecordingReconciler(Reconciler(clock, config), recorder)`, so it
            # must be able to name the production reconciler. It calls no method
            # on it — the wrapper does — and `app -> lifecycle` is already the
            # sanctioned direction, which is why R-6 chose this seam.
            "v2.lifecycle",
        }
        for path in (Path("backtest") / "replay").glob("*.py"):
            for module in imported_modules(path):
                if module.startswith("v2"):
                    self.assertIn(module, allowed_v2, f"{path.name}: {module}")

    def test_the_driver_defines_no_scoring_or_lifecycle_rule(self):
        source = (Path("backtest") / "replay" / "driver.py").read_text(encoding="utf-8")
        for forbidden in ("def score", "def reconcile", "def analyze",
                          "SKIP", "A+", "miss_count"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
