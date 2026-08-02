"""M11 — the `backtest` command line.

Covers the argument contract and the commands that need no network: a scan
boundary is validated as one, a holdout is declared and re-read, splits are
planned around it, and the guards are reachable from the command line rather than
only from Python.
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.cli import build_parser, main

HOUR = 3600
ORIGIN = 1_700_000_000 // HOUR * HOUR


def run(*argv: str) -> tuple[int, dict]:
    """Invoke the CLI, returning its exit code and parsed stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(argv))
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else {})


class TestArguments(unittest.TestCase):

    def test_a_boundary_off_the_hour_is_refused(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["splits", "--start", str(ORIGIN + 60),
                               "--end", str(ORIGIN + 10 * HOUR),
                               "--train", "5", "--test", "5"])

    def test_boundaries_accept_epoch_seconds_and_iso_instants(self):
        parser = build_parser()
        args = parser.parse_args(
            ["splits", "--start", "2024-01-01T00:00:00Z",
             "--end", "2024-06-01T00:00:00Z", "--train", "1000", "--test", "500"])
        self.assertEqual(args.start, 1_704_067_200)
        self.assertEqual(args.end, 1_717_200_000)

    def test_an_unparseable_instant_is_refused(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["splits", "--start", "yesterday", "--end", "0",
                               "--train", "1", "--test", "1"])

    def test_every_subcommand_is_reachable(self):
        parser = build_parser()
        for argv in (
            ["corpus", "build", "--root", "x", "--start", "2024-01-01",
             "--end", "2024-02-01"],
            ["corpus", "verify", "--root", "x"],
            ["replay", "--corpus", "x", "--out", "y",
             "--start", str(ORIGIN), "--end", str(ORIGIN + HOUR)],
            ["outcome", "--corpus", "x", "--run", "y"],
            ["metrics", "--run", "y"],
            ["freeze", "--run", "y", "--out", "z", "--label", "l"],
            ["holdout", "status", "--root", "r"],
        ):
            self.assertTrue(hasattr(parser.parse_args(argv), "func"), argv)

    def test_no_flag_relaxes_a_holdout_guard(self):
        """A guard that can be argued with is not a guard.

        Asserted against the parser's real option strings, not the source text —
        the module docstring says the words too, and a test that greps prose
        would pass for the wrong reason.
        """
        forbidden = {"--force", "--skip-holdout", "--no-verify", "--ignore",
                     "--no-holdout", "--fix"}
        found: set[str] = set()

        def walk(parser):
            for action in parser._actions:
                found.update(action.option_strings)
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict):
                    for choice in choices.values():
                        if hasattr(choice, "_actions"):
                            walk(choice)

        walk(build_parser())
        self.assertEqual(found & forbidden, set())


class TestResearchCommands(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def declare(self, start=ORIGIN + 8000 * HOUR, cycles=2000):
        return run("holdout", "declare", "--root", str(self.root),
                   "--start", str(start),
                   "--end", str(start + (cycles - 1) * HOUR),
                   "--label", "holdout-v1", "--snapshot", "snap000000000000",
                   "--reason", "declared before A1")

    def test_status_without_a_declaration_reports_and_fails(self):
        code, payload = run("holdout", "status", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertFalse(payload["declared"])
        self.assertIn("before the first experiment", payload["note"])

    def test_declaring_then_reading_back(self):
        code, declared = self.declare()
        self.assertEqual(code, 0)
        code, status = run("holdout", "status", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertTrue(status["declared"])
        self.assertEqual(status["holdout"]["holdout_id"], declared["holdout_id"])
        self.assertEqual(status["uses"], 0)
        self.assertIsNone(status["caution"])

    def test_declaring_a_different_holdout_fails(self):
        self.declare()
        with self.assertRaises(Exception):
            self.declare(start=ORIGIN + 12_000 * HOUR)

    def test_declaring_without_a_snapshot_or_corpus_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            run("holdout", "declare", "--root", str(self.root),
                "--start", str(ORIGIN), "--end", str(ORIGIN + 500 * HOUR),
                "--label", "x")
        self.assertIn("bound to the dataset", str(caught.exception))

    def test_splits_are_planned_around_a_declared_holdout(self):
        self.declare()
        code, plan = run(
            "splits", "--start", str(ORIGIN),
            "--end", str(ORIGIN + 12_000 * HOUR),
            "--train", "1000", "--test", "500", "--burn-in", "48",
            "--research", str(self.root))
        self.assertEqual(code, 0)
        self.assertFalse(plan["is_baseline"])
        lo, hi = ORIGIN + 8000 * HOUR, ORIGIN + 9999 * HOUR
        self.assertGreater(plan["count"], 0)
        for split in plan["splits"]:
            for role in ("train", "test"):
                window = split[role]
                self.assertFalse(window["start"] <= hi and lo <= window["end"],
                                 f"split {split['index']} {role} hits the holdout")
        # Splits after the holdout are legitimate — only overlap is forbidden.
        self.assertTrue(any(split["test"]["start"] > hi
                            for split in plan["splits"]))

    def test_splits_without_a_research_root_use_the_whole_window(self):
        code, plan = run("splits", "--start", str(ORIGIN),
                         "--end", str(ORIGIN + 12_000 * HOUR),
                         "--train", "1000", "--test", "500", "--burn-in", "48")
        self.assertEqual(code, 0)
        self.assertGreater(plan["count"], 0)
        self.assertIn("splits_hash", plan)


if __name__ == "__main__":
    unittest.main()
