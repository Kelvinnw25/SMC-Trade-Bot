"""Replay -> StrategyConfig variant injection (hunks A-D).

The wiring is four lines of plumbing and one guard, and the guard is the reason
this file exists. `scoring_config` is deliberately **not** in `core()` (C-16.2 is
a closed list), so a variant run's identity has to travel through
`strategy_version`. If it doesn't, the run hashes to the baseline's `run_id`,
resolves to the baseline's directory, and `--overwrite` unlinks the frozen
baseline's sinks. That failure is silent, destructive, and one keystroke away, so
it is pinned from several directions.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from backtest.experiments.scoring_variant import load_scoring_variant
from backtest.replay.caching import CachingAnalyzer, build_analyzer
from backtest.replay.config import STRATEGY_VERSION, ConfigError, ReplayConfig
from v2.analysis.engine import V2Analyzer
from v2.analysis.pipeline import scoring_config_of
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG

FAMILY = Path(__file__).resolve().parents[2] / (
    "experiments/v2_core_rebuild/scoring_v2")

WINDOW = dict(repository_root=Path("corpus"), output_root=Path("out"),
              start=1672531200, end=1679727600)


def variant_config(slug="s3b-momentum-positive"):
    return load_scoring_variant(slug, root=FAMILY)


class TestBuildAnalyzer(unittest.TestCase):
    """Hunk A. Both arms must carry the config, not just the caching one."""

    def test_default_is_unchanged_for_every_existing_caller(self):
        for caching in (False, True):
            with self.subTest(caching=caching):
                analyzer = build_analyzer(caching=caching)
                self.assertIs(scoring_config_of(analyzer.pipeline),
                              DEFAULT_SCORING_CONFIG)

    def test_both_arms_carry_the_variant(self):
        """`--caching` must not decide whether a variant takes effect."""
        _, variant = variant_config()
        for caching in (False, True):
            with self.subTest(caching=caching):
                analyzer = build_analyzer(caching=caching, scoring_config=variant)
                self.assertIs(scoring_config_of(analyzer.pipeline), variant)

    def test_the_caching_arm_still_returns_a_caching_analyzer(self):
        _, variant = variant_config()
        self.assertIsInstance(
            build_analyzer(caching=True, scoring_config=variant), CachingAnalyzer)
        self.assertIsInstance(
            build_analyzer(caching=False, scoring_config=variant), V2Analyzer)

    def test_the_config_reaches_the_scoring_stage_itself(self):
        _, variant = variant_config()
        analyzer = build_analyzer(caching=False, scoring_config=variant)
        self.assertIs(analyzer.pipeline.stages[-1].config, variant)


class TestReplayConfigGuard(unittest.TestCase):
    """Hunk B. The one that prevents deleting the baseline."""

    def test_a_variant_with_the_base_version_is_refused(self):
        _, variant = variant_config()
        with self.assertRaises(ConfigError) as ctx:
            ReplayConfig(**WINDOW, scoring_config=variant)
        message = str(ctx.exception)
        self.assertIn("strategy_version", message)
        self.assertIn("baseline", message)

    def test_the_guard_names_the_helper_that_fixes_it(self):
        _, variant = variant_config()
        with self.assertRaises(ConfigError) as ctx:
            ReplayConfig(**WINDOW, scoring_config=variant)
        self.assertIn("variant_version", str(ctx.exception))

    def test_a_variant_with_its_own_version_is_accepted(self):
        experiment, variant = variant_config()
        config = ReplayConfig(**WINDOW, scoring_config=variant,
                              strategy_version=experiment.variant_version)
        self.assertIs(config.scoring_config, variant)

    def test_the_default_config_needs_no_version_change(self):
        config = ReplayConfig(**WINDOW)
        self.assertEqual(config.strategy_version, STRATEGY_VERSION)
        self.assertIs(config.scoring_config, DEFAULT_SCORING_CONFIG)


class TestRunIdentity(unittest.TestCase):
    """The indirection that makes a variant a distinct run."""

    SNAPSHOT = "c8542cb360d13207"

    def test_scoring_config_stays_out_of_core(self):
        """C-16.2's `core` is a closed list; this module may not extend it."""
        experiment, variant = variant_config()
        core = ReplayConfig(
            **WINDOW, scoring_config=variant,
            strategy_version=experiment.variant_version
        ).core(data_snapshot_id=self.SNAPSHOT)
        self.assertNotIn("scoring_config", core)

    def test_the_variant_version_is_what_lands_in_core(self):
        experiment, variant = variant_config()
        core = ReplayConfig(
            **WINDOW, scoring_config=variant,
            strategy_version=experiment.variant_version
        ).core(data_snapshot_id=self.SNAPSHOT)
        self.assertEqual(core["strategy_version"], experiment.variant_version)
        self.assertIn("s3b-momentum-positive", core["strategy_version"])
        self.assertIn(experiment.variant_id[:8], core["strategy_version"])

    def test_a_variant_run_id_differs_from_the_baseline(self):
        experiment, variant = variant_config()
        baseline = ReplayConfig(**WINDOW)
        variant_run = ReplayConfig(**WINDOW, scoring_config=variant,
                                   strategy_version=experiment.variant_version)
        self.assertNotEqual(
            variant_run.run_id(data_snapshot_id=self.SNAPSHOT),
            baseline.run_id(data_snapshot_id=self.SNAPSHOT))

    def test_two_variants_get_different_run_ids(self):
        ids = set()
        for slug in ("s3b-momentum-positive", "s3a-momentum-zero",
                     "s2-uninformative-zero"):
            experiment, variant = variant_config(slug)
            ids.add(ReplayConfig(**WINDOW, scoring_config=variant,
                                 strategy_version=experiment.variant_version
                                 ).run_id(data_snapshot_id=self.SNAPSHOT))
        self.assertEqual(len(ids), 3)

    def test_the_same_variant_is_idempotent(self):
        experiment, variant = variant_config()
        ids = {
            ReplayConfig(**WINDOW, scoring_config=variant,
                         strategy_version=experiment.variant_version
                         ).run_id(data_snapshot_id=self.SNAPSHOT)
            for _ in range(2)
        }
        self.assertEqual(len(ids), 1)


class TestDriverPassesItThrough(unittest.TestCase):
    """Hunk C, asserted on source rather than by running a 7.8-hour replay."""

    DRIVER = Path(__file__).resolve().parents[2] / "backtest/replay/driver.py"

    def test_the_driver_forwards_the_config_to_build_analyzer(self):
        source = self.DRIVER.read_text(encoding="utf-8")
        self.assertIn("scoring_config=config.scoring_config", source)


class TestCliWiring(unittest.TestCase):
    """Hunk D, through the parser rather than by invoking a run."""

    def parse(self, *extra):
        from backtest.cli import build_parser

        return build_parser().parse_args([
            "replay", "--corpus", "c", "--out", "o",
            "--start", "1672531200", "--end", "1679727600", *extra])

    def test_variant_defaults_to_none_so_replay_is_the_baseline(self):
        self.assertIsNone(self.parse().variant)

    def test_variant_and_experiment_are_accepted(self):
        args = self.parse("--variant", "s3b-momentum-positive")
        self.assertEqual(args.variant, "s3b-momentum-positive")
        self.assertEqual(
            args.experiment, Path("experiments/v2_core_rebuild/scoring_v2"))

    def test_the_experiment_root_is_overridable(self):
        args = self.parse("--variant", "x", "--experiment", "somewhere")
        self.assertEqual(args.experiment, Path("somewhere"))


if __name__ == "__main__":
    unittest.main()