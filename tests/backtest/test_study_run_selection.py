"""Study entry points must state which run they read.

Both `exit_census` and `episode_inference` used to fall back to the first run
under `data/runs/official/runs` when no run directory was given, and neither
parsed `--run`. After the market-fill admissibility amendment moved the current
bundle to a different tree, `python -m ... --run <new>` silently produced output
from the **superseded** `outcome_config_hash` — output that looks entirely
normal and carries the wrong lineage stamps. C-4.12 forbids mixing configs, so
a study that cannot say which run it read must refuse to run.

These tests pin the fix without touching any analytics semantics: `--run` is
required, it is the run actually used, and an unstated run raises rather than
guessing.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from backtest.analytics.studies import episode_inference as ei
from backtest.analytics.studies import exit_census as ec

MODULES = ((ec, "exit_census"), (ei, "episode_inference"))


class TestRunMustBeStated(unittest.TestCase):

    def test_main_refuses_an_unstated_run(self):
        for module, name in MODULES:
            with self.subTest(module=name):
                with self.assertRaises(ValueError) as caught:
                    module.main()
                self.assertIn("explicit run_dir", str(caught.exception))

    def test_main_never_falls_back_to_a_default_path(self):
        """The old failure mode: `main()` reading `data/runs/official/runs`."""
        for module, name in MODULES:
            with self.subTest(module=name):
                with mock.patch.object(
                        module, "analyse",
                        side_effect=AssertionError("analysis must not run")):
                    with self.assertRaises(ValueError):
                        module.main()

    def test_cli_requires_run(self):
        for module, name in MODULES:
            with self.subTest(module=name):
                with self.assertRaises(SystemExit) as caught:
                    module._cli([])
                self.assertNotEqual(caught.exception.code, 0)


class TestCliPassesTheRunThrough(unittest.TestCase):
    """`--run X` must reach `main` as X — not a default, not a sibling."""

    def test_run_argument_is_the_run_used(self):
        for module, name in MODULES:
            with self.subTest(module=name):
                seen = {}

                def capture(run_dir=None, out_path=None, _s=seen):
                    _s["run"] = run_dir
                    _s["out"] = out_path
                    return {}

                with mock.patch.object(module, "main", capture):
                    module._cli(["--run", "some/other/run",
                                 "--out", "somewhere.json"])
                self.assertEqual(seen["run"], "some/other/run")
                self.assertEqual(seen["out"], "somewhere.json")

    def test_run_argument_is_not_silently_dropped(self):
        """The precise old bug: `--run` parsed away and ignored."""
        for module, name in MODULES:
            with self.subTest(module=name):
                seen = {}
                with mock.patch.object(
                        module, "main",
                        lambda run_dir=None, out_path=None: seen.update(
                            run=run_dir) or {}):
                    module._cli(["--run", "data/runs/elsewhere"])
                self.assertIsNotNone(seen["run"])
                self.assertNotIn("official", str(seen["run"]))


class TestSemanticsUntouched(unittest.TestCase):
    """The fix is entry-point only; no analytics constant or gate moved."""

    def test_friction_references_unchanged(self):
        self.assertEqual(ec.FRICTION_LEG_CONT_SHORT, 0.0683)
        self.assertEqual(ec.FRICTION_LEG_BOOK, 0.0441)
        self.assertEqual(ec.BAND, (0.02, ec.FRICTION_LEG_CONT_SHORT))

    def test_main_still_accepts_a_positional_run(self):
        """Programmatic callers, including the recomputation path, are
        unaffected: `main(run_dir)` remains the interface."""
        for module, name in MODULES:
            with self.subTest(module=name):
                import inspect
                params = list(
                    inspect.signature(module.main).parameters)
                self.assertEqual(params[:2], ["run_dir", "out_path"])

    def test_run_dir_may_be_a_path_or_a_string(self):
        for module, name in MODULES:
            with self.subTest(module=name):
                seen = {}
                with mock.patch.object(
                        module, "main",
                        lambda run_dir=None, out_path=None: seen.update(
                            run=run_dir) or {}):
                    module._cli(["--run", str(Path("a") / "b")])
                self.assertIn("b", str(seen["run"]))


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
