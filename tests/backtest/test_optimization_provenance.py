"""Optimization provenance and the replay CLI's cache switch (B1, B2).

Two harness defects found reviewing Gate B, both of which would have surfaced
only at step 9 — after the code freeze had closed the window to fix them:

**B1.** `backtest.cli._replay` built `ReplayConfig` without `caching_analyzer`
and no flag existed to set it, so *every* CLI replay — both shakedowns and the
official baseline — ran with the C-8.4 cache off, while step 9 requires the
manifest to record it on.

**B2.** The manifest's optimization block named `caching_analyzer` only. Step 9
requires `vectorized_swings` beside it, and without it a rolled-back baseline
(`SWING_IMPL = "v1"`) is indistinguishable from a vectorized one in its own
manifest. `git_commit` does not close that gap — it reads `.git/HEAD` and
carries no dirty-tree flag.

Both are provenance and wiring. Neither may change a result, which is what
`test_the_bundle_is_byte_identical_with_the_cache_on_and_off` holds them to.
"""
from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest.cli import build_parser
from backtest.lineage import optimizations
from backtest.replay import MANIFEST_FILENAME, ReplayConfig
from tests.backtest.test_replay_driver import DriverCase
from v2.analysis import structure as ST

#: Written by the driver and by `JobRecorder`; step 9 §4 names both keys.
EXPECTED_KEYS = {"caching_analyzer", "vectorized_swings"}


# ---------------------------------------------------------------------------
# B2 — the provenance block itself
# ---------------------------------------------------------------------------

class TestOptimizationsBlock(unittest.TestCase):

    def test_the_block_is_exactly_the_two_declared_flags(self):
        self.assertEqual(EXPECTED_KEYS,
                         set(optimizations(caching_analyzer=False)))

    def test_vectorized_swings_is_true_on_the_shipped_default(self):
        self.assertTrue(optimizations(caching_analyzer=False)["vectorized_swings"])

    def test_vectorized_swings_reads_the_seam_live_not_at_import_time(self):
        """The rollback must be visible in the manifest of the run that used it.

        Binding `SWING_IMPL` at import time would record the value the module
        held when `backtest.lineage` was first imported, which is precisely the
        wrong one after a rollback.
        """
        original = ST.SWING_IMPL
        try:
            ST.SWING_IMPL = "v1"
            self.assertFalse(
                optimizations(caching_analyzer=False)["vectorized_swings"])
        finally:
            ST.SWING_IMPL = original
        self.assertTrue(optimizations(caching_analyzer=False)["vectorized_swings"])

    def test_caching_analyzer_is_passed_through_as_a_bool(self):
        self.assertTrue(optimizations(caching_analyzer=True)["caching_analyzer"])
        self.assertFalse(optimizations(caching_analyzer=False)["caching_analyzer"])
        self.assertIs(
            True, optimizations(caching_analyzer=1)["caching_analyzer"])

    def test_the_provenance_layer_owns_the_analysis_import_not_the_driver(self):
        """B2's architectural constraint, asserted rather than trusted.

        `test_the_driver_contains_no_strategy_logic` holds `backtest/replay/` to
        a closed set of `v2` imports. Widening it to write one boolean would
        trade an architectural guarantee for a convenience, so the dependency
        lives in `backtest.lineage`, which is already the provenance layer.
        """
        from tests.backtest.test_historical_adapter import imported_modules

        for path in (Path("backtest") / "replay").glob("*.py"):
            for module in imported_modules(path):
                self.assertNotEqual(
                    "v2.analysis.structure", module,
                    f"{path.name} must not import the swing implementation")


# ---------------------------------------------------------------------------
# B1 — the CLI switch
# ---------------------------------------------------------------------------

class TestReplayCachingFlag(unittest.TestCase):

    def _parse(self, *extra: str) -> argparse.Namespace:
        return build_parser().parse_args(
            ["replay", "--corpus", "c", "--out", "o",
             "--start", "2023-01-01T00:00:00Z", "--end", "2023-01-01T05:00:00Z",
             *extra])

    def test_the_flag_exists_and_defaults_to_off(self):
        self.assertFalse(self._parse().caching_analyzer)

    def test_the_flag_turns_the_cache_on(self):
        self.assertTrue(self._parse("--caching").caching_analyzer)

    def test_the_destination_matches_the_config_field(self):
        """The namespace attribute and the `ReplayConfig` field are one name.

        A mismatch would parse cleanly and silently leave the cache off, which
        is exactly the failure B1 was.
        """
        self.assertIn("caching_analyzer", vars(self._parse()))
        self.assertIn("caching_analyzer", ReplayConfig.__dataclass_fields__)

    def _config_built_by_cli(self, *extra: str) -> ReplayConfig:
        """Run `cli.main(["replay", ...])` far enough to capture its config.

        Parsing the flag is not the property that matters — B1 was a config that
        never received it. The corpus and universe resolutions are stubbed
        because neither is under test here; the run is stopped the moment the
        configuration exists.
        """
        from types import SimpleNamespace
        from unittest.mock import patch

        import backtest.cli as CLI

        captured: list[ReplayConfig] = []

        class Stop(Exception):
            pass

        def spy(**kwargs):
            config = ReplayConfig(**kwargs)
            captured.append(config)
            raise Stop

        repository = SimpleNamespace(symbols=lambda timeframe: ["BTCUSDT"])
        resolution = SimpleNamespace(
            eligible_symbols=frozenset({"BTCUSDT"}), universe_id="u")

        with patch("backtest.corpus.open_corpus", return_value=repository), \
             patch("backtest.universe.resolve", return_value=resolution), \
             patch("backtest.replay.ReplayConfig", spy):
            with self.assertRaises(Stop):
                CLI.main([
                    "replay", "--corpus", "c", "--out", "o",
                    "--start", "2023-01-01T00:00:00Z",
                    "--end", "2023-01-01T05:00:00Z", *extra])
        return captured[0]

    def test_the_cli_leaves_the_cache_off_by_default(self):
        self.assertFalse(self._config_built_by_cli().caching_analyzer)

    def test_the_cli_passes_the_flag_into_the_config(self):
        self.assertTrue(self._config_built_by_cli("--caching").caching_analyzer)


# ---------------------------------------------------------------------------
# The manifest, end to end
# ---------------------------------------------------------------------------

class TestManifestRecordsBothFlags(DriverCase):

    def _manifest(self, **kw) -> dict:
        _, result = self.run_replay(**kw)
        return json.loads(
            (result.run_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    def test_the_manifest_carries_the_optimization_block(self):
        self.assertEqual(EXPECTED_KEYS,
                         set(self._manifest()["optimizations"]))

    def test_the_block_records_the_cache_being_off(self):
        block = self._manifest()["optimizations"]
        self.assertFalse(block["caching_analyzer"])
        self.assertTrue(block["vectorized_swings"])

    def test_the_block_records_the_cache_being_on(self):
        block = self._manifest(caching_analyzer=True)["optimizations"]
        self.assertTrue(block["caching_analyzer"])

    def test_the_block_stays_out_of_core(self):
        manifest = self._manifest(caching_analyzer=True)
        self.assertNotIn("caching_analyzer", manifest["core"])
        self.assertNotIn("vectorized_swings", manifest["core"])

    def test_the_bundle_is_byte_identical_with_the_cache_on_and_off(self):
        """The claim both flags make, held to the same bar as T4.3 and T5.2.

        `optimizations` is excluded from the manifest comparison along with the
        two wall-clock keys — it *is* the variable under test. Everything else,
        `run_id` included, must match byte for byte.
        """
        _, off = self.run_replay(out="off", caching_analyzer=False)
        _, on = self.run_replay(out="on", caching_analyzer=True)
        self.assertEqual(off.run_id, on.run_id)

        names = sorted(p.name for p in off.run_dir.iterdir() if p.is_file())
        self.assertGreater(len(names), 1)
        for name in names:
            left = (off.run_dir / name).read_bytes()
            right = (on.run_dir / name).read_bytes()
            if name != MANIFEST_FILENAME:
                self.assertEqual(left, right, f"{name} differs")
                continue
            a = json.loads(left.decode("utf-8"))
            b = json.loads(right.decode("utf-8"))
            for volatile in ("generated_at", "wall_seconds", "optimizations"):
                a.pop(volatile, None)
                b.pop(volatile, None)
            self.assertEqual(json.dumps(a, sort_keys=True),
                             json.dumps(b, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
