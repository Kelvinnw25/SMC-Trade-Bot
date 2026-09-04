"""Tests for the lifecycle analysis (IncrementMiss + PartialRefresh) study.

Fast tests build a `Book` directly from hand-scripted `Episode`/outcome
fixtures (no bundle required). The slow tests at the bottom run against the
official frozen bundle's already-written `episode_reconstruction.json`,
guarded and skipped when either artifact is absent.
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
from backtest.analytics.studies import lifecycle_analysis as la

OFFICIAL_BUNDLE = Path("data/runs/official/runs")
BASELINE_DIR = Path("data/baselines/v2.2.0-official")


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


def _all_keys(payload: Any) -> set:
    keys: set = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(str(key).lower())
            keys |= _all_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            keys |= _all_keys(item)
    return keys


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def episode(*, signal_id: str, symbol: str = "AAA/USDT", direction: str = "LONG",
            entry_mode: str = "RETRACEMENT", start_boundary: int = 1_700_006_400,
            end_boundary: Optional[int] = None, end_reason: str = "DECAY",
            increment_miss_count: int = 0, partial_refresh_count: int = 0,
            tier_at_start: str = "VALID", tier_at_end: str = "VALID",
            ever_alert_bearing: bool = True, outcome_joined: bool = True,
            ) -> er.Episode:
    end = end_boundary if end_boundary is not None else start_boundary + 3600
    return er.Episode(
        signal_id=signal_id, symbol=symbol, direction=direction,
        entry_mode=entry_mode,
        thesis_key=f"{symbol}|{direction}|{entry_mode}",
        start_boundary=start_boundary, start_h1_last_ts=float(start_boundary - 3600),
        end_boundary=end, end_reason=end_reason,
        increment_miss_count=increment_miss_count,
        partial_refresh_count=partial_refresh_count,
        first_miss_boundary=None, last_miss_boundary=None,
        first_refresh_boundary=None, last_refresh_boundary=None,
        tier_at_start=tier_at_start, tier_at_end=tier_at_end,
        ever_alert_bearing=ever_alert_bearing, outcome_joined=outcome_joined,
    )


def row(*, filled: bool = True, realized_r: Optional[float] = 1.0,
        **episode_kwargs: Any) -> la.Row:
    return la.Row(episode=episode(**episode_kwargs), filled=filled,
                  realized_r=realized_r)


#: One row per (cell x hour-of-day) so cell + session coverage is exact and
#: predictable in fast tests.
_CELL_HOURS = {
    "CONTINUATION×SHORT": 1,   # ASIA
    "CONTINUATION×LONG": 8,    # LONDON
    "RETRACEMENT×SHORT": 13,   # LONDON_NY_OVERLAP
    "RETRACEMENT×LONG": 17,    # NEWYORK
}


def small_book(n_per_cell: int = 20) -> tuple:
    """`n_per_cell` rows in each of the 4 cells, spread across miss/refresh
    bins and end reasons, all falling on real 2024-01-01 UTC hours."""
    rows = []
    cell_specs = (
        ("CONTINUATION×SHORT", "CONTINUATION", "SHORT", 1),
        ("CONTINUATION×LONG", "CONTINUATION", "LONG", 8),
        ("RETRACEMENT×SHORT", "RETRACEMENT", "SHORT", 13),
        ("RETRACEMENT×LONG", "RETRACEMENT", "LONG", 17),
    )
    base_day = 1_704_067_200  # 2024-01-01 00:00 UTC
    reasons = ("DECAY", "REPLACED", "SUPERSEDED", "TTL", "KILL",
              "STILL_RESIDENT_AT_END")
    i = 0
    for _name, mode, direction, hour in cell_specs:
        for k in range(n_per_cell):
            miss = k % 6                 # covers 0..5 -> bins 0,1,2,3,4+,4+
            refresh = k % 5               # covers 0..4 -> bins 0,1,2,3+,3+
            reason = reasons[k % len(reasons)]
            filled = (k % 3 != 0)
            realized_r = (0.5 if k % 2 == 0 else -0.3) if filled else None
            start = base_day + hour * 3600 + (k % 4) * 86_400
            rows.append(row(
                signal_id=f"SIG-{i}", symbol=f"S{i % 5}/USDT", direction=direction,
                entry_mode=mode, start_boundary=start,
                end_boundary=start + 3600 * (1 + k % 3), end_reason=reason,
                increment_miss_count=miss, partial_refresh_count=refresh,
                filled=filled, realized_r=realized_r,
                tier_at_start=("WATCH", "VALID", "A+")[k % 3],
                tier_at_end=("WATCH", "VALID", "A+")[(k + 1) % 3],
            ))
            i += 1
    return tuple(rows)


def analyse_rows(rows: tuple) -> dict:
    """Runs the same aggregation `analyse()` does, without touching disk."""
    return {
        "run_id": "run-x", "source": "replay", "metrics_hash": "m",
        "outcome_config_hash": "o", "data_snapshot_id": "d",
        "episode_reconstruction_specification_hash": er.specification_hash(),
        "lifecycle_analysis_specification_hash": la.specification_hash(),
        "evidence_class": la.EVIDENCE_CLASS, "scope": la.SCOPE_STATEMENT,
        "governance_notes": list(la.GOVERNANCE_NOTES),
        "population": la._population_accounting(rows, {}),
        "increment_miss": {"distribution": la._miss_distribution(rows)},
        "partial_refresh": {"distribution": la._refresh_distribution(rows)},
        "joint_miss_refresh": la._joint_table(rows),
        "end_reason_accounting": la._end_reason_table(rows),
        "parent_cells": la._cell_views(rows),
        "sessions": la._session_view(rows),
    }


# ---------------------------------------------------------------------------
# 1-2 — bin boundaries
# ---------------------------------------------------------------------------

class TestBinBoundaries(unittest.TestCase):

    def test_l1_miss_bin_boundaries(self):
        self.assertEqual([la._miss_bin(n) for n in (0, 1, 2, 3, 4, 5, 100)],
                          ["0", "1", "2", "3", "4+", "4+", "4+"])

    def test_l2_refresh_bin_boundaries(self):
        self.assertEqual([la._refresh_bin(n) for n in (0, 1, 2, 3, 4, 100)],
                          ["0", "1", "2", "3+", "3+", "3+"])

    def test_l2b_joint_bin_boundaries(self):
        self.assertEqual([la._miss_bin_joint(n) for n in (0, 1, 2, 3, 9)],
                          ["0", "1", "2", "3+", "3+"])
        self.assertEqual([la._refresh_bin_joint(n) for n in (0, 1, 2, 9)],
                          ["0", "1", "2+", "2+"])


# ---------------------------------------------------------------------------
# 3-7 — accounting closure
# ---------------------------------------------------------------------------

class TestAccountingClosure(unittest.TestCase):

    def setUp(self):
        self.rows = small_book()
        self.payload = analyse_rows(self.rows)

    def test_l3_episode_accounting_closes(self):
        total = self.payload["population"]["episodes"]
        self.assertEqual(total, len(self.rows))
        self.assertEqual(
            sum(r["n"] for r in self.payload["increment_miss"]["distribution"]),
            total)
        self.assertEqual(
            sum(r["n"] for r in self.payload["partial_refresh"]["distribution"]),
            total)
        self.assertEqual(
            sum(r["n"] for r in self.payload["joint_miss_refresh"]), total)

    def test_l4_end_reason_accounting_closes(self):
        total = self.payload["population"]["episodes"]
        self.assertEqual(
            sum(r["n"] for r in self.payload["end_reason_accounting"]), total)
        self.assertEqual(
            {r["end_reason"] for r in self.payload["end_reason_accounting"]},
            set(la.END_REASON_ORDER))

    def test_l5_fill_status_accounting_closes(self):
        population = self.payload["population"]
        miss = self.payload["increment_miss"]["distribution"]
        refresh = self.payload["partial_refresh"]["distribution"]
        self.assertEqual(sum(r["filled_n"] for r in miss), population["filled"])
        self.assertEqual(sum(r["scored_n"] for r in miss), population["scored"])
        self.assertEqual(sum(r["filled_n"] for r in refresh), population["filled"])
        self.assertEqual(sum(r["scored_n"] for r in refresh), population["scored"])
        joint = self.payload["joint_miss_refresh"]
        self.assertEqual(sum(r["filled_n"] for r in joint), population["filled"])
        self.assertEqual(sum(r["scored_n"] for r in joint), population["scored"])

    def test_l6_fixed_4cell_coverage(self):
        cells = self.payload["parent_cells"]
        self.assertEqual(set(cells), {name for name, _m, _d in la.CELLS})
        self.assertEqual(sum(b["n"] for b in cells.values()),
                          self.payload["population"]["episodes"])

    def test_l7_fixed_5session_coverage(self):
        sessions = self.payload["sessions"]
        self.assertEqual({r["session"] for r in sessions["rows"]},
                          set(la.SESSION_NAMES))
        self.assertEqual(
            sum(r["n"] for r in sessions["rows"]) + sessions["unassigned"],
            self.payload["population"]["episodes"])
        self.assertEqual(sessions["unassigned"], 0)


# ---------------------------------------------------------------------------
# 8 — no cross-mode grouping
# ---------------------------------------------------------------------------

class TestNoCrossMode(unittest.TestCase):

    def test_l8_no_cross_mode_or_ad_hoc_grouping_keys(self):
        payload = analyse_rows(small_book())
        cell_keys = set(payload["parent_cells"])
        self.assertEqual(cell_keys, {name for name, _m, _d in la.CELLS})
        for label in la.PROHIBITED_GROUPING_LABELS:
            self.assertNotIn(label, {k.lower() for k in cell_keys})
        session_keys = {r["session"] for r in payload["sessions"]["rows"]}
        for label in la.PROHIBITED_GROUPING_LABELS:
            self.assertNotIn(label, {k.lower() for k in session_keys})


# ---------------------------------------------------------------------------
# 9-10 — determinism / specification hash
# ---------------------------------------------------------------------------

class TestDeterminismAndHash(unittest.TestCase):

    def test_l9_deterministic_output(self):
        rows = small_book()
        first = json.dumps(analyse_rows(rows), sort_keys=True, default=str)
        second = json.dumps(analyse_rows(rows), sort_keys=True, default=str)
        self.assertEqual(first, second)

    def test_l9b_input_order_does_not_change_output(self):
        rows = small_book()
        forward = json.dumps(analyse_rows(rows), sort_keys=True, default=str)
        backward = json.dumps(analyse_rows(tuple(reversed(rows))),
                              sort_keys=True, default=str)
        self.assertEqual(forward, backward)

    def test_l10_specification_hash_stable(self):
        self.assertEqual(la.specification_hash(), la.specification_hash())
        self.assertEqual(len(la.specification_hash()), 16)


# ---------------------------------------------------------------------------
# 11 — forbidden language
# ---------------------------------------------------------------------------

class TestGovernanceLanguage(unittest.TestCase):

    def test_l11_no_ranking_or_forbidden_language(self):
        payload = analyse_rows(small_book())
        payload["generated_at"] = "2024-01-01T00:00:00Z"
        text = la.render(payload).lower()
        for word in la.PROHIBITED_WORDS:
            self.assertNotIn(word, text)
        for word in ("pass", "fail"):
            self.assertNotIn(word, text)


# ---------------------------------------------------------------------------
# 12 — isolation
# ---------------------------------------------------------------------------

class TestIsolation(unittest.TestCase):

    def test_l12_no_forbidden_imports(self):
        modules = _imported_modules(la)
        self.assertNotIn("backtest.replay", modules)
        for module in modules:
            self.assertNotIn(module.split(".")[0], {"v2", "src"})
        for name in ("session_decomposition", "fill_null", "episode_inference",
                    "exit_census"):
            self.assertNotIn(f"backtest.analytics.studies.{name}", modules)

    def test_l12b_not_registered_anywhere(self):
        from backtest.analytics.studies import STUDIES

        names = {name for name, _runner in STUDIES}
        self.assertNotIn("lifecycle_analysis", names)
        registry = (Path(la.__file__).parent / "__init__.py").read_text(
            encoding="utf-8")
        self.assertNotIn("lifecycle_analysis", registry)
        cli = Path("backtest/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("lifecycle_analysis", cli)

    def test_l12c_no_rng(self):
        for module in _imported_modules(la):
            self.assertNotIn("random", module)

    def test_l12d_upstream_modules_unmodified_by_import(self):
        self.assertEqual(er.EVIDENCE_CLASS, "DESCRIPTIVE")
        self.assertEqual(len(er.END_REASONS), 6)

    def test_l12e_artifacts_land_only_in_the_given_directory(self):
        payload = analyse_rows(small_book())
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "lifecycle"
            written = la.write_lifecycle_analysis(payload, out)
            self.assertEqual(set(written), {
                "lifecycle_analysis.json", "lifecycle_analysis.md",
                "lifecycle_analysis.csv"})
            for path in written.values():
                self.assertTrue(path.exists())
                self.assertEqual(path.parent, out)

    def test_l12f_csv_header_matches_declared_columns(self):
        rows = la.csv_rows(analyse_rows(small_book()))
        self.assertEqual(tuple(rows[0]), la._CSV_HEADER)
        for record in rows[1:]:
            self.assertEqual(len(record), len(rows[0]))


# ---------------------------------------------------------------------------
# 13 — episode reconstruction hash is bound
# ---------------------------------------------------------------------------

class TestSpecificationBinding(unittest.TestCase):

    def test_l13_bound_to_parent_specification_hash(self):
        payload = la._specification_payload()
        self.assertEqual(payload["parent_specification_hash"],
                          er.specification_hash())

    def test_l13b_book_echoes_the_artifacts_own_recorded_hash(self):
        """`analyse()` must echo the value written into the artifact file,
        not recompute it independently, so a stale artifact is visible."""
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            episode_dir = (run_dir / "analytics" / "research"
                          / er.DIRNAME)
            episode_dir.mkdir(parents=True)
            (episode_dir / "episode_reconstruction.json").write_text(
                json.dumps({
                    "run_id": "run-x", "source": "replay",
                    "strategy_version": "9.9.9",
                    "outcome_config_hash": "deadbeef",
                    "data_snapshot_id": "cafef00d",
                    "reconstruction_specification_hash": "not_the_real_hash",
                    "reconciliation": {}, "episodes": [],
                }), encoding="utf-8")
            (run_dir / "outcomes.jsonl").write_text("", encoding="utf-8")
            book = la.load(run_dir)
            self.assertEqual(book.episode_reconstruction_specification_hash,
                              "not_the_real_hash")
            self.assertEqual(book.strategy_version, "9.9.9")


# ---------------------------------------------------------------------------
# 15 — baseline lock unchanged
# ---------------------------------------------------------------------------

class TestBaselineLock(unittest.TestCase):

    def test_l15_baseline_files_untouched(self):
        if not BASELINE_DIR.exists():
            self.skipTest("baseline absent")
        digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                   for path in sorted(BASELINE_DIR.iterdir()) if path.is_file()}
        self.assertEqual(digests["baseline.json"], "bfbfea07a6871b08")
        self.assertEqual(digests["metrics.json"], "4b8a91e77df644ef")
        self.assertEqual(digests["report.md"], "5924072f406a69bf")
        self.assertEqual(digests["segments.csv"], "2a448e5690021fd7")


# ---------------------------------------------------------------------------
# 14, 16 — real bundle
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
        cls.payload = la.analyse(cls.run_dir)

    def setUp(self):
        if self.run_dir is None:
            self.skipTest("official bundle or its episode reconstruction "
                          "artifact is absent")

    def test_14_official_run_spot_checks(self):
        population = self.payload["population"]
        self.assertEqual(population["episodes"], 76_576)
        self.assertEqual(population["outcome_joined"], 76_576)
        self.assertEqual(population["outcome_without_join"], 0)
        self.assertEqual(
            sum(r["n"] for r in self.payload["increment_miss"]["distribution"]),
            76_576)
        self.assertEqual(
            sum(r["n"] for r in self.payload["partial_refresh"]["distribution"]),
            76_576)
        self.assertEqual(
            sum(r["n"] for r in self.payload["end_reason_accounting"]), 76_576)
        self.assertEqual(
            sum(b["n"] for b in self.payload["parent_cells"].values()), 76_576)
        self.assertEqual(
            sum(r["n"] for r in self.payload["sessions"]["rows"]), 76_576)
        # cross-checked against episode_reconstruction's own reconciliation
        end_by_reason = {r["end_reason"]: r["n"]
                         for r in self.payload["end_reason_accounting"]}
        upstream = population["upstream_reconciliation"]
        self.assertEqual(end_by_reason["KILL"], upstream["evictions"]["kill"])
        self.assertEqual(end_by_reason["DECAY"], upstream["evictions"]["decay"])
        self.assertEqual(end_by_reason["TTL"], upstream["evictions"]["ttl"])
        self.assertEqual(end_by_reason["REPLACED"], upstream["replacements"])
        self.assertEqual(end_by_reason["SUPERSEDED"], upstream["supersedes"])
        self.assertEqual(end_by_reason["STILL_RESIDENT_AT_END"],
                          upstream["active_at_end"])

    def test_16_cli_audit_passes_after_artifact_generation(self):
        from backtest.audit import audit_run

        out_dir = self.run_dir / "analytics" / "research" / la.DIRNAME
        la.write_lifecycle_analysis(self.payload, out_dir)
        report = audit_run(self.run_dir)
        self.assertTrue(report.ok,
                        f"audit failed: {[f.to_dict() for f in report.errors]}")

    def test_16b_deterministic_write_except_generated_at(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = la.write_lifecycle_analysis(self.payload,
                                                Path(temporary) / "a")
            second = la.write_lifecycle_analysis(self.payload,
                                                 Path(temporary) / "b")
            left = json.loads(first["lifecycle_analysis.json"].read_text(
                encoding="utf-8"))
            right = json.loads(second["lifecycle_analysis.json"].read_text(
                encoding="utf-8"))
            left.pop("generated_at")
            right.pop("generated_at")
            self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
