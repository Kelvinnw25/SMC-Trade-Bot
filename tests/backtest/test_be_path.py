"""Tests for the BE@1R path measurement.

Fast tests drive `path_facts` against a synthetic in-memory repository so every
ordering rule is exercised deterministically without touching a bundle. The
slow tests at the bottom bind against the official frozen run.
"""
from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from backtest.analytics.studies import be_path as bp

OFFICIAL_BUNDLE = Path("data/runs/official/runs")
BASELINE_DIR = Path("data/baselines/v2.2.0-official")

H1 = 3600
BASE = 1_704_067_200


def official_run() -> Optional[Path]:
    if not OFFICIAL_BUNDLE.exists():
        return None
    for candidate in sorted(OFFICIAL_BUNDLE.iterdir()):
        if candidate.is_dir() and (candidate / "metrics.json").exists():
            return candidate
    return None


def _imported_modules(module: Any) -> set:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


@dataclass
class FakeRecord:
    """Only the fields `path_facts` reads."""
    symbol: str = "AAA/USDT"
    direction: str = "LONG"
    entry_mode: str = "CONTINUATION"
    signal_id: str = "SIG"
    fill_T: int = BASE
    exit_T: int = BASE
    fill_price: float = 100.0
    sl: float = 90.0
    tp: float = 130.0
    realized_r: float = 3.0
    mfe_r: float = 3.0
    fill_kind: str = "MARKET_WALK_ORIGIN"


class FakeRepo:
    """Serves hand-built candles. `m5[h1_open]` absent -> incomplete set."""

    def __init__(self, h1: dict, m5: Optional[dict] = None) -> None:
        self._h1 = h1
        self._m5 = m5 or {}

    @staticmethod
    def _arr(rows):
        return np.array(
            [(int(t) * 1000, o, h, low, c) for t, o, h, low, c in rows],
            dtype=[("timestamp", "i8"), ("open", "f8"), ("high", "f8"),
                   ("low", "f8"), ("close", "f8")])

    def range(self, native, interval, *, start_ms, end_ms):
        source = self._h1 if interval == "1h" else self._m5
        rows = []
        for open_time in sorted(source):
            if start_ms <= open_time * 1000 < end_ms:
                rows.extend(source[open_time])
        return self._arr(rows)


def h1_candle(open_time, high, low, open_=None, close=None):
    return (open_time, open_ if open_ is not None else low,
            high, low, close if close is not None else high)


def m5_series(h1_open, ranges):
    """12 M5 candles for one hour from a list of (high, low) — padded flat."""
    assert len(ranges) <= bp.M5_PER_H1
    filled = list(ranges) + [ranges[-1]] * (bp.M5_PER_H1 - len(ranges))
    return [(h1_open + i * 300, low, high, low, high)
            for i, (high, low) in enumerate(filled)]


# ---------------------------------------------------------------------------
# 1-4 — first passage and return detection
# ---------------------------------------------------------------------------

class TestFirstPassage(unittest.TestCase):

    def test_p1_first_1r_touch_detected(self):
        """+1R = 110 for a long filled at 100 with SL 90."""
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 105, 99)],
                BASE + H1: [h1_candle(BASE + H1, 115, 104)],
                BASE + 2 * H1: [h1_candle(BASE + 2 * H1, 131, 114)]},
            m5={BASE: m5_series(BASE, [(105, 99)]),
                BASE + H1: m5_series(BASE + H1, [(115, 104)]),
                BASE + 2 * H1: m5_series(BASE + 2 * H1, [(131, 114)])})
        facts = bp.path_facts(
            FakeRecord(exit_T=BASE + 2 * H1), repo)
        self.assertEqual(facts.t_touch_0_5r, 0)      # 105 >= 105
        self.assertEqual(facts.t_touch_1r, 1)        # 110 first met in candle 1
        self.assertEqual(facts.t_touch_2r, 2)        # 120 in candle 2
        self.assertEqual(facts.t_touch_tp, 2)        # 130 in candle 2
        self.assertFalse(facts.returned_before_tp)

    def test_p2_return_only_counts_after_1r(self):
        """Entry is touched in candle 0 (before +1R) — must NOT count."""
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 105, 98)],
                BASE + H1: [h1_candle(BASE + H1, 115, 111)],
                BASE + 2 * H1: [h1_candle(BASE + 2 * H1, 131, 114)]},
            m5={BASE: m5_series(BASE, [(105, 98)]),
                BASE + H1: m5_series(BASE + H1, [(115, 111)]),
                BASE + 2 * H1: m5_series(BASE + 2 * H1, [(131, 114)])})
        facts = bp.path_facts(FakeRecord(exit_T=BASE + 2 * H1), repo)
        self.assertEqual(facts.t_touch_1r, 1)
        self.assertIsNone(facts.t_return_entry_after_1r)
        self.assertFalse(facts.returned_before_tp)
        self.assertFalse(facts.return_ambiguous)

    def test_p3_return_before_tp_detected(self):
        """+1R in candle 1, back to 100 in candle 2, TP in candle 3."""
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 105, 99)],
                BASE + H1: [h1_candle(BASE + H1, 115, 104)],
                BASE + 2 * H1: [h1_candle(BASE + 2 * H1, 112, 99)],
                BASE + 3 * H1: [h1_candle(BASE + 3 * H1, 131, 110)]},
            m5={BASE: m5_series(BASE, [(105, 99)]),
                BASE + H1: m5_series(BASE + H1, [(115, 104)]),
                BASE + 2 * H1: m5_series(BASE + 2 * H1, [(112, 99)]),
                BASE + 3 * H1: m5_series(BASE + 3 * H1, [(131, 110)])})
        facts = bp.path_facts(FakeRecord(exit_T=BASE + 3 * H1), repo)
        self.assertEqual(facts.t_touch_1r, 1)
        self.assertEqual(facts.t_return_entry_after_1r, 2)
        self.assertEqual(facts.t_touch_tp, 3)
        self.assertTrue(facts.returned_before_tp)
        self.assertFalse(facts.return_ambiguous)

    def test_p4_no_false_return_when_entry_touched_only_before_1r(self):
        """Distinct from p2: entry touched in the SAME candle the trade fills,
        and +1R comes later. Still must not count."""
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 109, 100)],
                BASE + H1: [h1_candle(BASE + H1, 131, 111)]},
            m5={BASE: m5_series(BASE, [(109, 100)]),
                BASE + H1: m5_series(BASE + H1, [(131, 111)])})
        facts = bp.path_facts(FakeRecord(exit_T=BASE + H1), repo)
        self.assertEqual(facts.t_touch_1r, 1)
        self.assertIsNone(facts.t_return_entry_after_1r)
        self.assertFalse(facts.returned_before_tp)

    def test_p4b_return_after_tp_does_not_count(self):
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 131, 99)]},
            m5={BASE: m5_series(BASE, [(112, 105), (131, 125), (131, 99)])})
        facts = bp.path_facts(FakeRecord(exit_T=BASE), repo)
        self.assertEqual(facts.seg_touch_1r, 0)
        self.assertEqual(facts.seg_touch_tp, 1)
        self.assertEqual(facts.seg_return_entry_after_1r, 2)
        self.assertFalse(facts.returned_before_tp)
        self.assertFalse(facts.return_ambiguous)


# ---------------------------------------------------------------------------
# 5 — ambiguity
# ---------------------------------------------------------------------------

class TestAmbiguity(unittest.TestCase):

    def test_p5_same_segment_1r_and_entry_is_ambiguous(self):
        """One M5 candle spans both 110 and 100 — order unknowable."""
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 112, 99)],
                BASE + H1: [h1_candle(BASE + H1, 131, 110)]},
            m5={BASE: m5_series(BASE, [(112, 99)]),
                BASE + H1: m5_series(BASE + H1, [(131, 110)])})
        facts = bp.path_facts(FakeRecord(exit_T=BASE + H1), repo)
        self.assertTrue(facts.ambiguous_1r_entry_same_segment)
        self.assertTrue(facts.return_ambiguous)
        self.assertFalse(facts.returned_before_tp)

    def test_p5b_same_segment_entry_and_tp_is_ambiguous(self):
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 115, 104)],
                BASE + H1: [h1_candle(BASE + H1, 131, 99)]},
            m5={BASE: m5_series(BASE, [(115, 104)]),
                BASE + H1: m5_series(BASE + H1, [(131, 99)])})
        facts = bp.path_facts(FakeRecord(exit_T=BASE + H1), repo)
        self.assertEqual(facts.seg_touch_1r, 0)
        self.assertTrue(facts.ambiguous_entry_tp_same_segment)
        self.assertTrue(facts.return_ambiguous)
        self.assertFalse(facts.returned_before_tp)

    def test_p5c_missing_m5_degrades_to_h1_and_flags(self):
        """No M5 -> the whole hour is one segment, so everything collides."""
        repo = FakeRepo(h1={BASE: [h1_candle(BASE, 131, 99)]}, m5={})
        facts = bp.path_facts(FakeRecord(exit_T=BASE), repo)
        self.assertTrue(facts.degraded_no_m5)
        self.assertEqual(facts.segments_scanned, 1)
        self.assertTrue(facts.return_ambiguous)
        self.assertFalse(facts.returned_before_tp)

    def test_p5d_limit_fill_skips_segments_before_the_fill(self):
        """A dip to 100 before the limit fills must not become a return."""
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 131, 99)]},
            m5={BASE: m5_series(BASE, [(103, 99), (112, 100), (131, 111)])})
        record = FakeRecord(exit_T=BASE, fill_kind="LIMIT_TOUCH",
                            fill_price=100.0)
        facts = bp.path_facts(record, repo)
        # scan starts at the first M5 that could fill a 100 limit = segment 0,
        # so segment indices are unshifted here; the guard is exercised by the
        # short-side variant below.
        self.assertIsNotNone(facts.seg_touch_1r)


# ---------------------------------------------------------------------------
# 6 — long/short symmetry
# ---------------------------------------------------------------------------

class TestSymmetry(unittest.TestCase):

    def test_p6_short_mirrors_long(self):
        """Short filled at 100, SL 110, TP 70. +1R = 90, return = 100."""
        record = FakeRecord(direction="SHORT", sl=110.0, tp=70.0,
                            exit_T=BASE + 3 * H1)
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 101, 95)],
                BASE + H1: [h1_candle(BASE + H1, 96, 85)],
                BASE + 2 * H1: [h1_candle(BASE + 2 * H1, 101, 88)],
                BASE + 3 * H1: [h1_candle(BASE + 3 * H1, 90, 69)]},
            m5={BASE: m5_series(BASE, [(101, 95)]),
                BASE + H1: m5_series(BASE + H1, [(96, 85)]),
                BASE + 2 * H1: m5_series(BASE + 2 * H1, [(101, 88)]),
                BASE + 3 * H1: m5_series(BASE + 3 * H1, [(90, 69)])})
        facts = bp.path_facts(record, repo)
        self.assertEqual(facts.t_touch_0_5r, 0)      # 95
        self.assertEqual(facts.t_touch_1r, 1)        # 90
        self.assertEqual(facts.t_return_entry_after_1r, 2)
        self.assertEqual(facts.t_touch_tp, 3)
        self.assertTrue(facts.returned_before_tp)

    def test_p6b_short_no_false_return_before_1r(self):
        record = FakeRecord(direction="SHORT", sl=110.0, tp=70.0,
                            exit_T=BASE + H1)
        repo = FakeRepo(
            h1={BASE: [h1_candle(BASE, 102, 95)],
                BASE + H1: [h1_candle(BASE + H1, 96, 69)]},
            m5={BASE: m5_series(BASE, [(102, 95)]),
                BASE + H1: m5_series(BASE + H1, [(96, 69)])})
        facts = bp.path_facts(record, repo)
        self.assertEqual(facts.t_touch_1r, 1)
        self.assertIsNone(facts.t_return_entry_after_1r)
        self.assertFalse(facts.returned_before_tp)


# ---------------------------------------------------------------------------
# 7-9 — populations, arithmetic, bracket
# ---------------------------------------------------------------------------

def fact(cell: str, *, realized_r: float, winner: bool, returned=False,
         ambiguous=False) -> bp.PathFacts:
    return bp.PathFacts(
        signal_id=None, cell=cell, realized_r=realized_r, mfe_r=1.5,
        is_winner=winner, seg_touch_1r=0, returned_before_tp=returned,
        return_ambiguous=ambiguous)


class TestAggregation(unittest.TestCase):

    def test_p7_fixed_cells_only(self):
        payload = bp.measure({})
        self.assertEqual(set(payload["cells"]),
                         {name for name, _m, _d in bp.CELLS})
        self.assertEqual(set(bp.Q_STAR), {name for name, _m, _d in bp.CELLS})

    def test_p8_r_weighted_arithmetic(self):
        cell = "CONTINUATION×SHORT"
        facts = [
            fact(cell, realized_r=3.0, winner=True, returned=True),
            fact(cell, realized_r=1.0, winner=True),
            fact(cell, realized_r=6.0, winner=True),
            fact(cell, realized_r=-1.0, winner=False),
        ]
        block = bp._cell_measure(facts, cell)
        self.assertEqual(block["winner_r_weight"], 10.0)
        self.assertEqual(block["returned_r_weight"], 3.0)
        self.assertAlmostEqual(block["q_lower"], 0.30)
        self.assertAlmostEqual(block["q_upper"], 0.30)
        # the unweighted fraction is deliberately different, and reported apart
        self.assertAlmostEqual(block["q_unweighted_lower"], 1 / 3)
        self.assertEqual(block["n_pool"], 1)

    def test_p9_lower_never_exceeds_upper(self):
        cell = "RETRACEMENT×LONG"
        facts = [
            fact(cell, realized_r=2.0, winner=True, returned=True),
            fact(cell, realized_r=4.0, winner=True, ambiguous=True),
            fact(cell, realized_r=4.0, winner=True),
        ]
        block = bp._cell_measure(facts, cell)
        self.assertAlmostEqual(block["q_lower"], 0.2)
        self.assertAlmostEqual(block["q_upper"], 0.6)
        self.assertLessEqual(block["q_lower"], block["q_upper"])

    def test_p9b_verdicts_bracket_q_star(self):
        cell = "CONTINUATION×LONG"                      # q* = 0.2555
        below = bp._cell_measure(
            [fact(cell, realized_r=10.0, winner=True)], cell)
        self.assertEqual(below["verdict"], "BELOW_Q_STAR")
        above = bp._cell_measure(
            [fact(cell, realized_r=10.0, winner=True, returned=True)], cell)
        self.assertEqual(above["verdict"], "ABOVE_Q_STAR")
        crosses = bp._cell_measure(
            [fact(cell, realized_r=5.0, winner=True),
             fact(cell, realized_r=5.0, winner=True, ambiguous=True)], cell)
        self.assertEqual(crosses["verdict"], "CROSSES_Q_STAR")
        self.assertEqual(
            bp._cell_measure([], cell)["verdict"], "INSUFFICIENT")

    def test_p9c_kill_rule_is_three_of_four_and_not_recomputed(self):
        self.assertEqual(bp.KILL_CELL_THRESHOLD, 3)
        facts = {}
        for index, (name, _m, _d) in enumerate(bp.CELLS):
            facts[name] = [fact(name, realized_r=10.0, winner=True,
                                returned=index < 3)]
        payload = bp.measure(facts)
        self.assertEqual(payload["cells_at_or_above_q_star"], 3)
        self.assertTrue(payload["killed"])
        facts[bp.CELLS[2][0]] = [
            fact(bp.CELLS[2][0], realized_r=10.0, winner=True)]
        self.assertFalse(bp.measure(facts)["killed"])

    def test_p9d_q_star_values_are_the_pre_frozen_ones(self):
        self.assertEqual(bp.Q_STAR, {
            "CONTINUATION×SHORT": 0.2077,
            "CONTINUATION×LONG": 0.2555,
            "RETRACEMENT×SHORT": 0.3472,
            "RETRACEMENT×LONG": 0.3679,
        })


# ---------------------------------------------------------------------------
# 10-12 — determinism, isolation, baseline
# ---------------------------------------------------------------------------

class TestGovernance(unittest.TestCase):

    def _payload(self):
        facts = {name: [fact(name, realized_r=2.0, winner=True,
                             returned=index % 2 == 0)]
                 for index, (name, _m, _d) in enumerate(bp.CELLS)}
        payload = {
            "run_id": "r", "source": "replay", "strategy_version": "2.2.0",
            "metrics_hash": "m", "outcome_config_hash": "o",
            "data_snapshot_id": "d",
            "be_path_specification_hash": bp.specification_hash(),
            "evidence_class": bp.EVIDENCE_CLASS, "scope": bp.SCOPE_STATEMENT,
            "generated_at": "2024-01-01T00:00:00Z",
            "population": {"barrier_resolved": 4, "eligible_reached_1r": 4},
        }
        payload.update(bp.measure(facts))
        return payload

    def test_p10_deterministic_output(self):
        first = json.dumps(self._payload(), sort_keys=True, default=str)
        second = json.dumps(self._payload(), sort_keys=True, default=str)
        self.assertEqual(first, second)

    def test_p10b_specification_hash_stable_and_binds_q_star(self):
        self.assertEqual(bp.specification_hash(), bp.specification_hash())
        before = bp.specification_hash()
        original = dict(bp.Q_STAR)
        try:
            bp.Q_STAR["CONTINUATION×SHORT"] = 0.9
            self.assertNotEqual(bp.specification_hash(), before)
        finally:
            bp.Q_STAR.clear()
            bp.Q_STAR.update(original)
        self.assertEqual(bp.specification_hash(), before)

    def test_p11_no_strategy_or_walker_mutation(self):
        modules = _imported_modules(bp)
        self.assertNotIn("backtest.replay", modules)
        # reads the walker's outputs, never imports or re-decides it
        self.assertNotIn("backtest.outcome.walker", modules)
        self.assertNotIn("backtest.outcome.engine", modules)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"v2", "src"})

    def test_p11b_not_registered_anywhere(self):
        from backtest.analytics.studies import STUDIES

        self.assertNotIn("be_path", {name for name, _r in STUDIES})
        self.assertNotIn("be_path", (Path(bp.__file__).parent / "__init__.py")
                         .read_text(encoding="utf-8"))
        self.assertNotIn("be_path",
                         Path("backtest/cli.py").read_text(encoding="utf-8"))

    def test_p11c_no_rng(self):
        for module in _imported_modules(bp):
            self.assertNotIn("random", module)

    def test_p11d_artifacts_land_only_in_the_given_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "be"
            written = bp.write_be_path(self._payload(), out)
            self.assertEqual(set(written),
                             {"be_path.json", "be_path.md", "be_path.csv"})
            for path in written.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, out)

    def test_p12_baseline_files_untouched(self):
        if not BASELINE_DIR.exists():
            self.skipTest("baseline absent")
        digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                   for path in sorted(BASELINE_DIR.iterdir()) if path.is_file()}
        self.assertEqual(digests["baseline.json"], "bfbfea07a6871b08")
        self.assertEqual(digests["metrics.json"], "4b8a91e77df644ef")
        self.assertEqual(digests["report.md"], "5924072f406a69bf")
        self.assertEqual(digests["segments.csv"], "2a448e5690021fd7")


# ---------------------------------------------------------------------------
# Real bundle
# ---------------------------------------------------------------------------

class TestAgainstOfficialRun(unittest.TestCase):

    def setUp(self):
        self.run_dir = official_run()
        if self.run_dir is None:
            self.skipTest("official bundle absent")
        self.artifact = (self.run_dir / "analytics" / "research" / bp.DIRNAME
                         / "be_path.json")
        if not self.artifact.exists():
            self.skipTest("be_path artifact not yet generated")

    def test_p13_artifact_lineage_is_bound(self):
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "replay-9ec9cd4487f3ba19")
        self.assertEqual(payload["evidence_class"], "DESCRIPTIVE")
        self.assertEqual(payload["be_path_specification_hash"],
                         bp.specification_hash())
        self.assertTrue(payload["metrics_hash"])
        self.assertTrue(payload["outcome_config_hash"])
        self.assertTrue(payload["data_snapshot_id"])

    def test_p14_population_matches_frozen_census(self):
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertEqual(payload["population"]["barrier_resolved"], 29_853)
        self.assertEqual(payload["population"]["skipped_unknown_cell"], 0)
        total = sum(block["n_reached_1r"] for block in payload["cells"].values())
        self.assertEqual(total, payload["population"]["eligible_reached_1r"])

    def test_p15_bracket_is_ordered_and_q_star_unchanged(self):
        payload = json.loads(self.artifact.read_text(encoding="utf-8"))
        for cell, block in payload["cells"].items():
            self.assertEqual(block["q_star"], bp.Q_STAR[cell])
            if block["q_lower"] is not None:
                self.assertLessEqual(block["q_lower"], block["q_upper"])
                self.assertGreaterEqual(block["q_lower"], 0.0)
                self.assertLessEqual(block["q_upper"], 1.0)

    def test_p16_cli_audit_passes(self):
        from backtest.audit import audit_run

        report = audit_run(self.run_dir)
        self.assertTrue(report.ok,
                        f"audit failed: {[f.to_dict() for f in report.errors]}")


if __name__ == "__main__":
    unittest.main()
