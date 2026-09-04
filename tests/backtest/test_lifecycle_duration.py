"""Tests for the duration-conditioned lifecycle study.

Fast tests build `Row` fixtures directly, so no bundle is required. The slow
tests at the bottom run against the official frozen bundle's already-written
`episode_reconstruction.json`, guarded and skipped when it is absent.
"""
from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from backtest.analytics.studies import episode_reconstruction as er
from backtest.analytics.studies import lifecycle_duration as ld

OFFICIAL_BUNDLE = Path("data/runs/official/runs")
BASELINE_DIR = Path("data/baselines/v2.2.0-official")

HOUR = 3600
BASE = 1_704_067_200          # 2024-01-01 00:00 UTC


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


def row(*, signal_id: str = "S", symbol: str = "AAA/USDT",
        direction: str = "LONG", entry_mode: str = "RETRACEMENT",
        start: int = BASE, duration_h: float = 2.0,
        end_reason: str = "DECAY", miss: int = 0, refresh: int = 0,
        tier: str = "VALID", filled: bool = True,
        realized_r: Optional[float] = 1.0,
        fill_T: Optional[int] = None) -> ld.Row:
    end = start + int(duration_h * HOUR)
    episode = er.Episode(
        signal_id=signal_id, symbol=symbol, direction=direction,
        entry_mode=entry_mode,
        thesis_key=f"{symbol}|{direction}|{entry_mode}",
        start_boundary=start, start_h1_last_ts=float(start - HOUR),
        end_boundary=end, end_reason=end_reason,
        increment_miss_count=miss, partial_refresh_count=refresh,
        first_miss_boundary=None, last_miss_boundary=None,
        first_refresh_boundary=None, last_refresh_boundary=None,
        tier_at_start=tier, tier_at_end=tier, ever_alert_bearing=True,
        outcome_joined=True,
    )
    return ld.Row(episode=episode, filled=filled,
                  realized_r=realized_r if filled else None,
                  fill_T=fill_T if (fill_T is not None or not filled)
                  else start + HOUR)


def spread_book(*, n_per_group: int = 200) -> tuple:
    """A book where every cell has both miss levels present at two age bins,
    sized above `MIN_STRATUM_N` so matched strata are comparable."""
    rows = []
    index = 0
    for _name, mode, direction in ld.CELLS:
        for age_hours in (2.0, 6.0):
            for miss, refresh, fill in ((0, 0, True), (3, 2, True)):
                for _k in range(n_per_group):
                    rows.append(row(
                        signal_id=f"S{index}", direction=direction,
                        entry_mode=mode, duration_h=age_hours, miss=miss,
                        refresh=refresh, filled=fill, realized_r=0.4))
                    index += 1
    return tuple(rows)


# ---------------------------------------------------------------------------
# 1 — duration and bins
# ---------------------------------------------------------------------------

class TestDurationAndBins(unittest.TestCase):

    def test_d1_duration_calculation(self):
        self.assertEqual(row(duration_h=0.0).duration_hours, 0.0)
        self.assertEqual(row(duration_h=3.0).duration_hours, 3.0)
        self.assertEqual(row(duration_h=39.0).duration_hours, 39.0)

    def test_d2_age_bin_boundaries_are_half_open(self):
        cases = {
            0.0: "<1h", 0.99: "<1h", 1.0: "1-4h", 3.99: "1-4h",
            4.0: "4-12h", 11.99: "4-12h", 12.0: "12-24h", 23.99: "12-24h",
            24.0: "1-3d", 71.99: "1-3d", 72.0: "3-7d", 167.99: "3-7d",
            168.0: ">7d", 10_000.0: ">7d",
        }
        for hours, expected in cases.items():
            self.assertEqual(ld._age_bin(hours), expected, msg=str(hours))

    def test_d2b_declared_bins_are_exhaustive_and_ordered(self):
        self.assertEqual(len(ld.AGE_BINS), 7)
        previous_high = None
        for _name, low, high in ld.AGE_BINS:
            if previous_high is not None:
                self.assertEqual(low, previous_high)
            previous_high = high
        self.assertIsNone(ld.AGE_BINS[-1][2])
        self.assertEqual(ld.AGE_BINS[0][1], 0.0)

    def test_d3_miss_and_refresh_levels(self):
        self.assertEqual([ld._miss_level(n) for n in (0, 1, 2, 3, 9)],
                         ["0", "1", "2", "3", "3"])
        self.assertEqual([ld._refresh_level(n) for n in (0, 1, 2, 9)],
                         ["0", "1", "2+", "2+"])

    def test_d3b_non_comparable_bin_is_declared(self):
        self.assertIn("<1h", ld.NON_COMPARABLE_AGE_BINS)


# ---------------------------------------------------------------------------
# 2 — accounting closure
# ---------------------------------------------------------------------------

class TestAccountingClosure(unittest.TestCase):

    def setUp(self):
        self.rows = spread_book()

    def test_d4_age_distribution_closes(self):
        block = ld._age_distribution(self.rows)
        self.assertEqual(sum(r["n"] for r in block["rows"]) + block["unassigned"],
                         len(self.rows))
        self.assertEqual(block["unassigned"], 0)
        self.assertEqual({r["age_bin"] for r in block["rows"]},
                         set(ld.AGE_BIN_NAMES))

    def test_d5_two_way_tables_close(self):
        by_miss = ld._two_way(self.rows, levels=ld.MISS_LEVELS,
                              level_of=lambda r: r.miss_level)
        by_refresh = ld._two_way(self.rows, levels=ld.REFRESH_LEVELS,
                                 level_of=lambda r: r.refresh_level)
        self.assertEqual(sum(r["n"] for r in by_miss), len(self.rows))
        self.assertEqual(sum(r["n"] for r in by_refresh), len(self.rows))
        # every declared combination is emitted, including empty ones
        self.assertEqual(len(by_miss),
                         len(ld.AGE_BIN_NAMES) * len(ld.MISS_LEVELS))
        self.assertEqual(len(by_refresh),
                         len(ld.AGE_BIN_NAMES) * len(ld.REFRESH_LEVELS))

    def test_d6_fill_status_closes_in_every_group(self):
        for record in ld._two_way(self.rows, levels=ld.MISS_LEVELS,
                                  level_of=lambda r: r.miss_level):
            self.assertEqual(record["filled_n"] + record["unfilled_n"],
                             record["n"])

    def test_d7_parent_cell_separation(self):
        block = ld._cell_conditional(self.rows)
        self.assertEqual(set(block), {name for name, _m, _d in ld.CELLS})
        self.assertEqual(sum(b["n"] for b in block.values()), len(self.rows))


# ---------------------------------------------------------------------------
# 3 — matched comparison / no cross-mode
# ---------------------------------------------------------------------------

class TestMatchedComparison(unittest.TestCase):

    def test_d8_non_comparable_bin_never_enters_a_verdict(self):
        rows = list(spread_book())
        # 400 sub-hour episodes, all miss 0, none filled
        rows += [row(signal_id=f"Z{i}", duration_h=0.0, miss=0, filled=False)
                 for i in range(400)]
        block = ld._matched_analysis(tuple(rows), levels=ld.MISS_LEVELS,
                                     level_of=lambda r: r.miss_level)
        for cell_block in block["per_cell"].values():
            for stratum in cell_block["strata"]:
                if stratum["age_bin"] in ld.NON_COMPARABLE_AGE_BINS:
                    self.assertFalse(stratum["comparable"])
                    self.assertEqual(stratum["excluded_reason"],
                                     "non_comparable_age_bin")

    def test_d9_under_floor_strata_are_reported_but_not_comparable(self):
        rows = tuple(row(signal_id=f"S{i}", duration_h=2.0,
                         miss=0 if i % 2 else 3, filled=True)
                     for i in range(10))
        block = ld._matched_analysis(rows, levels=ld.MISS_LEVELS,
                                     level_of=lambda r: r.miss_level)
        strata = [s for b in block["per_cell"].values() for s in b["strata"]]
        self.assertTrue(strata)
        for stratum in strata:
            self.assertFalse(stratum["comparable"])
            self.assertEqual(stratum["excluded_reason"], "below_min_stratum_n")

    def test_d10_verdict_is_one_of_the_declared_labels(self):
        block = ld._matched_analysis(spread_book(), levels=ld.MISS_LEVELS,
                                     level_of=lambda r: r.miss_level)
        self.assertIn(block["overall_verdict"], ld.VERDICTS)
        for cell_block in block["per_cell"].values():
            self.assertIn(cell_block["verdict"], ld.VERDICTS)

    def test_d10b_indeterminate_when_too_few_comparable_strata(self):
        rows = tuple(row(signal_id=f"S{i}", duration_h=2.0, miss=0)
                     for i in range(500))
        block = ld._matched_analysis(rows, levels=ld.MISS_LEVELS,
                                     level_of=lambda r: r.miss_level)
        for cell_block in block["per_cell"].values():
            self.assertEqual(cell_block["verdict"], "INDETERMINATE")
        self.assertEqual(block["overall_verdict"], "INDETERMINATE")

    def test_d10c_full_attenuation_reads_as_exposure_plausible(self):
        """Identical fill rates across miss levels inside every stratum, but
        a positive unconditional gradient created purely by a sub-hour,
        never-filled miss-0 population."""
        rows = list(spread_book())
        index = 0
        for _name, mode, direction in ld.CELLS:
            for _k in range(500):
                rows.append(row(signal_id=f"Z{index}", direction=direction,
                                entry_mode=mode, duration_h=0.0, miss=0,
                                filled=False))
                index += 1
        block = ld._matched_analysis(tuple(rows), levels=ld.MISS_LEVELS,
                                     level_of=lambda r: r.miss_level)
        for cell, cell_block in block["per_cell"].items():
            # the unconditional gradient is positive, created entirely by the
            # sub-hour never-filled miss-0 rows ...
            self.assertGreater(cell_block["marginal_fill_rate_delta"], 0.0,
                               msg=cell)
            # ... and vanishes completely once age is conditioned on.
            self.assertEqual(cell_block["median_retained_fraction"], 0.0, cell)
            self.assertEqual(cell_block["verdict"], "EXPOSURE_TIME_PLAUSIBLE",
                             msg=cell)

    def test_d11_no_cross_mode_pooling_anywhere(self):
        """Every matched stratum names exactly one parent cell, and the
        overall verdict is a tally of cell verdicts rather than a pooled
        recomputation."""
        block = ld._matched_analysis(spread_book(), levels=ld.MISS_LEVELS,
                                     level_of=lambda r: r.miss_level)
        declared = {name for name, _m, _d in ld.CELLS}
        for cell, cell_block in block["per_cell"].items():
            self.assertIn(cell, declared)
            for stratum in cell_block["strata"]:
                self.assertEqual(stratum["cell"], cell)
        self.assertEqual(sum(block["cell_verdict_tally"].values()),
                         len(ld.CELLS))


# ---------------------------------------------------------------------------
# 4 — determinism, hashes, governance
# ---------------------------------------------------------------------------

class TestDeterminismAndGovernance(unittest.TestCase):

    def _payload(self, rows: tuple) -> dict:
        return {
            "run_id": "r", "source": "replay", "metrics_hash": "m",
            "outcome_config_hash": "o", "data_snapshot_id": "d",
            "strategy_version": "2.2.0",
            "episode_reconstruction_specification_hash": er.specification_hash(),
            "lifecycle_duration_specification_hash": ld.specification_hash(),
            "evidence_class": ld.EVIDENCE_CLASS, "scope": ld.SCOPE_STATEMENT,
            "governance_notes": list(ld.GOVERNANCE_NOTES),
            "generated_at": "2024-01-01T00:00:00Z",
            "population": {"episodes": len(rows), "filled": 0, "scored": 0,
                           "upstream_reconciliation": {}},
            "age_distribution": ld._age_distribution(rows),
            "marginal_by_miss": ld._marginal(
                rows, levels=ld.MISS_LEVELS, level_of=lambda r: r.miss_level),
            "marginal_by_refresh": ld._marginal(
                rows, levels=ld.REFRESH_LEVELS,
                level_of=lambda r: r.refresh_level),
            "age_by_miss": ld._two_way(rows, levels=ld.MISS_LEVELS,
                                       level_of=lambda r: r.miss_level),
            "age_by_refresh": ld._two_way(rows, levels=ld.REFRESH_LEVELS,
                                          level_of=lambda r: r.refresh_level),
            "survivor_composition": ld._survivor_composition(rows),
            "decay_analysis": ld._decay_analysis(rows),
            "outcome_timing": ld._outcome_timing(rows),
            "parent_cells": ld._cell_conditional(rows),
            "matched_miss": ld._matched_analysis(
                rows, levels=ld.MISS_LEVELS, level_of=lambda r: r.miss_level),
            "matched_refresh": ld._matched_analysis(
                rows, levels=ld.REFRESH_LEVELS,
                level_of=lambda r: r.refresh_level),
        }

    def test_d12_deterministic_output(self):
        rows = spread_book()
        first = json.dumps(self._payload(rows), sort_keys=True, default=str)
        second = json.dumps(self._payload(rows), sort_keys=True, default=str)
        self.assertEqual(first, second)

    def test_d12b_input_order_does_not_change_output(self):
        rows = spread_book()
        forward = json.dumps(self._payload(rows), sort_keys=True, default=str)
        backward = json.dumps(self._payload(tuple(reversed(rows))),
                              sort_keys=True, default=str)
        self.assertEqual(forward, backward)

    def test_d13_specification_hash_stable_and_bound_to_parent(self):
        self.assertEqual(ld.specification_hash(), ld.specification_hash())
        self.assertEqual(len(ld.specification_hash()), 16)
        self.assertEqual(ld._specification_payload()["parent_specification_hash"],
                         er.specification_hash())

    def test_d13b_specification_hash_changes_when_a_constant_moves(self):
        before = ld.specification_hash()
        original = ld.RETAINED_HIGH
        try:
            ld.RETAINED_HIGH = original + 0.05
            self.assertNotEqual(ld.specification_hash(), before)
        finally:
            ld.RETAINED_HIGH = original
        self.assertEqual(ld.specification_hash(), before)

    def test_d14_no_forbidden_language_in_rendered_report(self):
        text = ld.render(self._payload(spread_book())).lower()
        for word in ld.PROHIBITED_WORDS:
            self.assertNotIn(word, text)

    def test_d15_no_forbidden_imports(self):
        modules = _imported_modules(ld)
        self.assertNotIn("backtest.replay", modules)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"v2", "src"})
        for name in ("session_decomposition", "fill_null", "exit_census",
                     "episode_inference"):
            self.assertNotIn(f"backtest.analytics.studies.{name}", modules)

    def test_d15b_no_rng(self):
        for module in _imported_modules(ld):
            self.assertNotIn("random", module)

    def test_d15c_not_registered_anywhere(self):
        from backtest.analytics.studies import STUDIES

        self.assertNotIn("lifecycle_duration",
                         {name for name, _runner in STUDIES})
        registry = (Path(ld.__file__).parent / "__init__.py").read_text(
            encoding="utf-8")
        self.assertNotIn("lifecycle_duration", registry)
        self.assertNotIn("lifecycle_duration",
                         Path("backtest/cli.py").read_text(encoding="utf-8"))

    def test_d16_artifacts_land_only_in_the_given_directory(self):
        payload = self._payload(spread_book())
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "duration"
            written = ld.write_lifecycle_duration(payload, out)
            self.assertEqual(set(written), {
                "lifecycle_duration.json", "lifecycle_duration.md",
                "lifecycle_duration.csv"})
            for path in written.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, out)

    def test_d16b_csv_is_rectangular(self):
        rows = ld.csv_rows(self._payload(spread_book()))
        self.assertEqual(tuple(rows[0]), ld._CSV_HEADER)
        for record in rows[1:]:
            self.assertEqual(len(record), len(rows[0]))

    def test_d17_baseline_files_untouched(self):
        if not BASELINE_DIR.exists():
            self.skipTest("baseline absent")
        digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                   for path in sorted(BASELINE_DIR.iterdir()) if path.is_file()}
        self.assertEqual(digests["baseline.json"], "bfbfea07a6871b08")
        self.assertEqual(digests["metrics.json"], "4b8a91e77df644ef")
        self.assertEqual(digests["report.md"], "5924072f406a69bf")
        self.assertEqual(digests["segments.csv"], "2a448e5690021fd7")


# ---------------------------------------------------------------------------
# 5 — real bundle
# ---------------------------------------------------------------------------

class TestAgainstOfficialRun(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.run_dir = official_run()
        if cls.run_dir is None:
            return
        artifact = (cls.run_dir / "analytics" / "research" / er.DIRNAME
                    / "episode_reconstruction.json")
        if not artifact.exists():
            cls.run_dir = None
            return
        cls.payload = ld.analyse(cls.run_dir)

    def setUp(self):
        if self.run_dir is None:
            self.skipTest("official bundle or episode reconstruction absent")

    def test_d18_population_and_closure(self):
        self.assertEqual(self.payload["population"]["episodes"], 76_576)
        age = self.payload["age_distribution"]
        self.assertEqual(sum(r["n"] for r in age["rows"]), 76_576)
        self.assertEqual(age["unassigned"], 0)
        self.assertEqual(sum(r["n"] for r in self.payload["age_by_miss"]),
                         76_576)
        self.assertEqual(sum(r["n"] for r in self.payload["age_by_refresh"]),
                         76_576)
        self.assertEqual(
            sum(b["n"] for b in self.payload["parent_cells"].values()), 76_576)

    def test_d19_declared_empty_bins_are_empty_and_flagged(self):
        age = self.payload["age_distribution"]
        self.assertEqual(age["empty_bins"], ["3-7d", ">7d"])
        self.assertLessEqual(age["observed_max_hours"], 72.0)
        for record in age["rows"]:
            if record["age_bin"] in ("3-7d", ">7d"):
                self.assertEqual(record["n"], 0)
                self.assertTrue(record["structurally_empty"])

    def test_d20_sub_hour_bin_holds_only_miss_zero(self):
        for record in self.payload["age_by_miss"]:
            if record["age_bin"] == "<1h" and record["level"] != "0":
                self.assertEqual(record["n"], 0)
                self.assertTrue(record["non_comparable_age_bin"])

    def test_d21_lineage_bound_to_episode_reconstruction(self):
        artifact = json.loads(
            (self.run_dir / "analytics" / "research" / er.DIRNAME
             / "episode_reconstruction.json").read_text(encoding="utf-8"))
        self.assertEqual(
            self.payload["episode_reconstruction_specification_hash"],
            artifact["reconstruction_specification_hash"])
        self.assertEqual(self.payload["run_id"], artifact["run_id"])
        self.assertTrue(self.payload["metrics_hash"])
        self.assertTrue(self.payload["outcome_config_hash"])
        self.assertTrue(self.payload["data_snapshot_id"])

    def test_d22_cli_audit_passes_after_artifact_generation(self):
        from backtest.audit import audit_run

        out_dir = self.run_dir / "analytics" / "research" / ld.DIRNAME
        ld.write_lifecycle_duration(self.payload, out_dir)
        report = audit_run(self.run_dir)
        self.assertTrue(report.ok,
                        f"audit failed: {[f.to_dict() for f in report.errors]}")

    def test_d23_deterministic_write_except_generated_at(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = ld.write_lifecycle_duration(self.payload,
                                                Path(temporary) / "a")
            second = ld.write_lifecycle_duration(self.payload,
                                                 Path(temporary) / "b")
            left = json.loads(first["lifecycle_duration.json"].read_text(
                encoding="utf-8"))
            right = json.loads(second["lifecycle_duration.json"].read_text(
                encoding="utf-8"))
            left.pop("generated_at")
            right.pop("generated_at")
            self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
