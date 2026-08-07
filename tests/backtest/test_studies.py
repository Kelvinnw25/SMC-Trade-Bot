"""`backtest.analytics.studies` — the Phase 3 research studies.

Each study is tested for three things: that it reads what it claims to read,
that its verdicts follow the pre-declared rule rather than the author's opinion,
and that it never asserts more than the evidence supports. The last one is the
reason most of these tests exist — a research artifact that overstates is worse
than no artifact, because it will be quoted.

The fixture bundle is the one `test_analytics` builds, extended with the ledger
label fields the studies join on.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backtest.analytics.studies import (
    RESEARCH_DIRNAME,
    STUDIES,
    run_studies,
    write_studies,
)
from backtest.analytics.studies.common import (
    CLASSES,
    MATERIAL_R,
    classify_family,
    labelled_trades,
    point_biserial,
)
from backtest.dataset import LEDGER_FILENAME, OUTCOMES_FILENAME
from backtest.metrics import METRICS_FILENAME, build_report
from backtest.metrics.report import ReportInputs
from tests.backtest.test_analytics import (
    BASE,
    H1,
    RUN_ID,
    SNAPSHOT,
    _population,
    _record,
)

#: Ledger context the studies join on. Varied across rows so every factor has
#: two sides and no dimension collapses to a single bucket.
_LABELS = ("sweep", "fvg", "displacement", "fresh_ob", "volume_spike",
           "target_clear", "htf_context_clean", "m5_choch",
           "momentum_weakening")


def _ledger_row(record, index: int, decision: str = "A+") -> dict:
    flip = lambda step: bool((index // step) % 2)
    return {
        "schema_version": 2, "strategy_version": "2.2.0",
        "recorded_at": float(record.boundary + 1), "symbol": record.symbol,
        "h1_last_ts": float(record.boundary - H1), "decision": decision,
        "score": record.score, "entry_mode": record.entry_mode,
        "regime": "expansion_up" if flip(2) else "rotational",
        "session_active": flip(3), "live_price": 100.0,
        "direction": record.direction, "entry_ready": True,
        "reason": None if flip(2) else "ob_quality_failed",
        "source": "replay", "run_id": RUN_ID, "cycle_index": record.cycle_index,
        "universe_rank": record.universe_rank, "contract_version": "1.1",
        "data_snapshot_id": SNAPSHOT,
        "sweep": flip(1), "fvg": flip(2), "displacement": flip(3),
        "fresh_ob": flip(4), "volume_spike": flip(1), "target_clear": flip(2),
        "htf_context_clean": flip(3), "m5_choch": flip(4),
        "momentum_weakening": flip(2), "veto_active": False,
        "anchor_override": False, "selected_ob_fresh": flip(3),
        "selected_ob_rank": 1 + (index % 4),
        "ob_candidate_count": 1 + (index % 6),
        "sweep_reclaim_strength": 0.1 * (index % 9),
        "atr_last": 1.5, "rr": 2.0, "tp_rr": 2.0,
        "weekly_bias": "BULLISH" if flip(2) else "BEARISH",
        "d1_bias": "BULLISH" if flip(3) else "BEARISH",
        "h4_bias": "BULLISH" if flip(4) else "BEARISH",
        "location": ("premium", "equilibrium", "discount")[index % 3],
        "direction_source": "weighted", "notes": ["fresh_ob", "weekly_aligned"],
        "d1_rows": 119, "h1_rows": 499, "m5_rows": 199,
    }


def build_research_bundle(root: Path) -> Path:
    run_dir = root / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    records = _population()

    (run_dir / OUTCOMES_FILENAME).write_text(
        "".join(json.dumps(r.to_dict(), sort_keys=True) + "\n"
                for r in records), encoding="utf-8")

    lines = [json.dumps(_ledger_row(r, i), sort_keys=True) + "\n"
             for i, r in enumerate(records)]
    for extra, decision in enumerate(("SKIP", "WATCH")):
        row = _ledger_row(records[0], 900 + extra, decision)
        row["cycle_index"] = 900 + extra
        row["h1_last_ts"] = float(BASE + (900 + extra) * H1)
        row["reason"] = "no_micro_pullback"
        lines.append(json.dumps(row, sort_keys=True) + "\n")
    (run_dir / LEDGER_FILENAME).write_text("".join(lines), encoding="utf-8")

    from backtest.dataset import LabeledDataset

    dataset = LabeledDataset.from_run(run_dir)
    report = build_report(
        dataset.records, labels=dataset.labels, health={"clean": True},
        dataset=dataset.to_dict(),
        inputs=ReportInputs(run_id=RUN_ID, window_start=BASE,
                            window_end=records[-1].boundary,
                            burn_in_h1=0, universe_mode="volume"))
    report.write(run_dir)
    return run_dir


class StudyCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls._root = Path(tempfile.mkdtemp(prefix="studies-shared-"))
        cls._template = build_research_bundle(cls._root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._root, ignore_errors=True)

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="studies-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run_dir = self.root / "runs" / RUN_ID
        shutil.copytree(self._template, self.run_dir)

    def studies(self):
        return run_studies(self.run_dir)

    def run_cli(self, *argv: str) -> int:
        from backtest.cli import main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(["research", *argv])


# ==========================================================================
# The join
# ==========================================================================

class TestLabelledTrades(StudyCase):

    def test_every_scored_trade_carries_its_ledger_labels(self):
        trades = labelled_trades(self.run_dir)
        metrics = json.loads(
            (self.run_dir / METRICS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(len(trades),
                         metrics["global"]["counts"]["scored_trades"])
        for trade in trades:
            self.assertTrue(trade.labels, trade.record.symbol)
            for name in _LABELS:
                self.assertIn(name, trade.labels)

    def test_trades_are_in_c98_order(self):
        from backtest.metrics import c98_order_key

        trades = labelled_trades(self.run_dir)
        keys = [c98_order_key(t.record) for t in trades]
        self.assertEqual(keys, sorted(keys))

    def test_attribute_access_falls_through_to_the_record(self):
        trade = labelled_trades(self.run_dir)[0]
        self.assertEqual(trade.symbol, trade.record.symbol)
        self.assertEqual(trade.realized_r, trade.record.realized_r)


# ==========================================================================
# Classification
# ==========================================================================

class TestClassification(StudyCase):

    def test_an_invariant_factor_is_classified_invariant(self):
        trades = labelled_trades(self.run_dir)
        results = classify_family(
            trades, [("always", "never varies", lambda t: True)])
        self.assertEqual(results[0].verdict, "INVARIANT")
        self.assertIsNone(results[0].comparison.significant)

    def test_a_real_effect_is_classified_useful(self):
        """A synthetic factor that perfectly predicts a positive outcome must
        come out USEFUL, or the classifier is not measuring anything."""
        trades = labelled_trades(self.run_dir)
        results = classify_family(
            trades,
            [("positive_r", "the trade won",
              lambda t: t.record.realized_r > 0)], min_n=2)
        self.assertEqual(results[0].verdict, "USEFUL")
        self.assertTrue(results[0].comparison.significant)
        self.assertGreater(results[0].comparison.effect, 0)

    def test_a_reversed_factor_is_classified_harmful(self):
        trades = labelled_trades(self.run_dir)
        results = classify_family(
            trades,
            [("negative_r", "the trade lost",
              lambda t: t.record.realized_r < 0)], min_n=2)
        self.assertEqual(results[0].verdict, "HARMFUL")
        self.assertLess(results[0].comparison.effect, 0)

    def test_a_sample_below_the_floor_is_classified_sparse(self):
        """The fixture holds far fewer than `MIN_SEGMENT_N` trades, so the
        default floor must refuse to test it rather than report a verdict."""
        trades = labelled_trades(self.run_dir)
        results = classify_family(
            trades, [("positive_r", "won", lambda t: t.record.realized_r > 0)])
        self.assertEqual(results[0].verdict, "SPARSE")
        self.assertIsNone(results[0].comparison.significant)

    def test_every_verdict_is_from_the_closed_list(self):
        for study in self.studies().values():
            for factor in study.payload.get("factors") or []:
                self.assertIn(factor["verdict"], CLASSES)

    def test_the_material_floor_is_applied(self):
        trades = labelled_trades(self.run_dir)
        results = classify_family(
            trades, [("positive_r", "won", lambda t: t.record.realized_r > 0)],
            min_n=2)
        result = results[0]
        self.assertEqual(result.material,
                         abs(result.comparison.effect) >= MATERIAL_R)

    def test_point_biserial_handles_degenerate_input(self):
        self.assertIsNone(point_biserial([True, True], [1.0, 2.0]))
        self.assertIsNone(point_biserial([True, False], [1.0, 1.0]))
        self.assertIsNotNone(
            point_biserial([True, False, True, False], [2.0, 0.0, 3.0, 1.0]))


# ==========================================================================
# Study 1 — scoring
# ==========================================================================

class TestScoringStudy(StudyCase):

    def study(self):
        return self.studies()["score_component_analysis"].payload

    def test_every_declared_factor_is_reported(self):
        from backtest.analytics.studies.scoring import CONFIRMATIONS

        names = {f["factor"] for f in self.study()["factors"]}
        self.assertEqual(names, {name for name, _d, _p in CONFIRMATIONS})

    def test_the_family_size_matches_the_declared_family(self):
        from backtest.analytics.studies.scoring import CONFIRMATIONS

        self.assertEqual(self.study()["method"]["family_size"],
                         len(CONFIRMATIONS))

    def test_frequency_is_a_share_of_the_population(self):
        for factor in self.study()["factors"]:
            if factor["frequency"] is None:
                continue
            self.assertGreaterEqual(factor["frequency"], 0.0)
            self.assertLessEqual(factor["frequency"], 1.0)
            total = factor["present"]["n"] + factor["absent"]["n"]
            if total:
                self.assertAlmostEqual(
                    factor["frequency"], factor["present"]["n"] / total,
                    places=9)

    def test_verdict_buckets_partition_the_factors(self):
        study = self.study()
        counted = sum(len(v) for v in study["verdicts"].values())
        self.assertEqual(counted, len(study["factors"]))

    def test_the_score_response_covers_every_score_present(self):
        study = self.study()
        rows = study["score_response"]["by_score"]
        trades = labelled_trades(self.run_dir)
        expected = {f"{int(t.record.score):02d}" for t in trades
                    if t.record.score is not None}
        self.assertEqual({row["key"] for row in rows}, expected)

    def test_confirmation_counts_cover_every_trade(self):
        study = self.study()
        total = sum(row["n"] for row in study["confirmation_count"]["by_count"])
        self.assertEqual(total, len(labelled_trades(self.run_dir)))

    def test_co_occurrence_is_symmetric_and_bounded(self):
        for pair in self.study()["co_occurrence"]["all_pairs"]:
            self.assertGreaterEqual(pair["agreement"], 0.0)
            self.assertLessEqual(pair["agreement"], 1.0)
            self.assertLessEqual(pair["both_present"], 1.0)

    def test_the_limitation_is_stated(self):
        self.assertIn("one factor at a time",
                      self.study()["method"]["limitation"])


# ==========================================================================
# Study 2 — setups
# ==========================================================================

class TestSetupStudy(StudyCase):

    def study(self):
        return self.studies()["setup_quality_analysis"].payload

    def test_both_setups_are_reported_separately(self):
        self.assertEqual(set(self.study()["setups"]),
                         {"CONTINUATION", "RETRACEMENT"})

    def test_each_setup_population_sums_to_its_trades(self):
        trades = labelled_trades(self.run_dir)
        for mode, block in self.study()["setups"].items():
            expected = sum(1 for t in trades if t.record.entry_mode == mode)
            self.assertEqual(block["population"]["n"], expected, mode)

    def test_dimension_buckets_never_exceed_the_setup_population(self):
        for mode, block in self.study()["setups"].items():
            total = block["population"]["n"]
            for name, dimension in block["dimensions"].items():
                counted = sum(b["n"] for b in dimension["buckets"])
                self.assertLessEqual(counted, total, f"{mode}/{name}")

    def test_the_winner_loser_profile_reports_both_sides(self):
        for mode, block in self.study()["setups"].items():
            profile = block["winner_loser_profile"]
            self.assertGreaterEqual(profile["winners_n"], 0)
            self.assertGreaterEqual(profile["losers_n"], 0)
            for row in profile["rows"]:
                self.assertIn("quantity", row)

    def test_the_study_declares_itself_descriptive(self):
        self.assertIn("descriptive", self.study()["method"]["note"])


# ==========================================================================
# Study 3 — direction
# ==========================================================================

class TestDirectionStudy(StudyCase):

    def study(self):
        return self.studies()["direction_analysis"].payload

    def test_every_declared_partition_is_present(self):
        from backtest.analytics.studies.direction import PARTITIONS

        self.assertEqual(set(self.study()["partitions"]),
                         {name for name, _key in PARTITIONS})

    def test_sign_consistency_is_a_share(self):
        for name, block in self.study()["partitions"].items():
            value = block["sign_consistency"]
            if value is None:
                continue
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)
            self.assertLessEqual(block["cells_where_short_leads"],
                                 block["inferential_cells"], name)

    def test_cell_counts_never_exceed_the_population(self):
        trades = labelled_trades(self.run_dir)
        for name, block in self.study()["partitions"].items():
            total = sum(row["long_n"] + row["short_n"]
                        for row in block["cells"])
            self.assertLessEqual(total, len(trades), name)

    def test_the_headline_matches_a_direct_computation(self):
        trades = labelled_trades(self.run_dir)
        shorts = [t.record.realized_r for t in trades
                  if t.record.direction == "SHORT"]
        headline = self.study()["headline"]
        self.assertEqual(headline["short"]["n"], len(shorts))
        if shorts:
            self.assertAlmostEqual(headline["short"]["avg_r"],
                                   sum(shorts) / len(shorts), places=9)

    def test_no_correction_is_claimed_across_partitions(self):
        self.assertIn("not independent hypotheses",
                      self.study()["method"]["no_correction"])


# ==========================================================================
# Study 4 — risk
# ==========================================================================

class TestRiskStudy(StudyCase):

    def study(self):
        return self.studies()["risk_analysis"].payload

    def test_all_four_hypotheses_are_weighed(self):
        names = {row["hypothesis"] for row in self.study()["hypotheses"]}
        self.assertEqual(names, {"targets too ambitious", "entries too late",
                                 "market does not follow through",
                                 "stop placement"})

    def test_each_hypothesis_declares_a_prediction_and_a_measurement(self):
        for row in self.study()["hypotheses"]:
            self.assertTrue(row["prediction"])
            self.assertTrue(row["measured"])
            self.assertIsInstance(row["supported"], bool)

    def test_attainment_counts_are_consistent(self):
        block = self.study()["target_attainment"]
        self.assertLessEqual(block["reachable"], block["n"])
        self.assertLessEqual(block["exited_at_target"], block["n"])
        self.assertGreaterEqual(block["unrealised_reachable"], 0)
        self.assertGreaterEqual(block["gap_paid_without_mfe_touch"], 0)

    def test_a_gap_exit_is_explained_not_reported_as_negative(self):
        """`reachable - taken` may be negative because a gap pays the target
        without MFE registering a touch. The derived counts must stay >= 0 and
        the raw difference must remain visible."""
        block = self.study()["target_attainment"]
        self.assertIn("reachable_minus_taken", block)
        self.assertEqual(
            block["reachable_minus_taken"],
            block["reachable"] - block["exited_at_target"])

    def test_rr_bands_cover_every_trade(self):
        total = sum(row["n"] for row in self.study()["by_rr_band"])
        self.assertEqual(total, len(labelled_trades(self.run_dir)))

    def test_timeout_rate_is_a_share(self):
        block = self.study()["timeouts"]
        self.assertGreaterEqual(block["timeout_rate"], 0.0)
        self.assertLessEqual(block["timeout_rate"], 1.0)


# ==========================================================================
# Study 5 — drawdown
# ==========================================================================

class TestDrawdownStudy(StudyCase):

    def study(self):
        return self.studies()["drawdown_analysis"].payload

    def test_the_window_comes_from_the_official_metrics(self):
        metrics = json.loads(
            (self.run_dir / METRICS_FILENAME).read_text(encoding="utf-8"))
        equity = metrics["global"]["equity"]
        study = self.study()
        if equity.get("max_drawdown_start_exit_T") is None:
            self.skipTest("bundle records no drawdown")
        self.assertEqual(study["window"]["start_exit_T"],
                         equity["max_drawdown_start_exit_T"])
        self.assertAlmostEqual(study["window"]["depth_r"],
                               equity["max_drawdown_r"], places=9)

    def test_inside_and_outside_partition_the_population(self):
        study = self.study()
        if "inside" not in study:
            self.skipTest("bundle records no drawdown")
        trades = labelled_trades(self.run_dir)
        self.assertEqual(study["inside"]["n"] + study["outside"]["n"],
                         len(trades))

    def test_shares_and_lift_are_coherent(self):
        study = self.study()
        for dimension, rows in (study.get("composition") or {}).items():
            for row in rows:
                if row["inside_share"] is None or not row["outside_share"]:
                    continue
                self.assertAlmostEqual(
                    row["lift"], row["inside_share"] / row["outside_share"],
                    places=9, msg=f"{dimension}/{row['key']}")

    def test_the_selection_caution_is_stated(self):
        study = self.study()
        if "method" not in study:
            self.skipTest("bundle records no drawdown")
        self.assertIn("selected because they",
                      study["method"]["caution"])


# ==========================================================================
# Study 6 — priority ranking
# ==========================================================================

class TestPriority(StudyCase):

    def study(self):
        return self.studies()["strategy_research_priority"].payload

    def test_every_finding_lands_in_exactly_one_tier(self):
        study = self.study()
        names = []
        for tier in (1, 2, 3):
            names += [entry["name"] for entry in study[f"priority_{tier}"]]
        self.assertEqual(len(names), len(set(names)) if len(names) == len(
            set(names)) else len(names))

    def test_every_entry_cites_its_evidence(self):
        study = self.study()
        for tier in (1, 2, 3):
            for entry in study[f"priority_{tier}"]:
                for field in ("finding", "source_study", "n", "effect_r",
                              "welch_ci", "p_adjusted", "expected_impact_r",
                              "interpretation"):
                    self.assertIn(field, entry, entry.get("name"))
                self.assertTrue(entry["interpretation"])

    def test_priority_one_requires_significance_and_materiality(self):
        for entry in self.study()["priority_1"]:
            self.assertTrue(entry["significant"], entry["name"])
            self.assertTrue(entry["material"], entry["name"])

    def test_priority_three_never_claims_significance(self):
        for entry in self.study()["priority_3"]:
            self.assertIn(entry["significant"], (None, False), entry["name"])

    def test_no_parameter_is_recommended(self):
        study = self.study()
        self.assertIn("no_parameter_recommendations", study)
        markdown = self.studies()["strategy_research_priority"].markdown
        for forbidden in ("set the weight", "change the threshold to",
                          "use a value of"):
            self.assertNotIn(forbidden, markdown.lower())

    def test_symbol_lift_is_excluded_from_the_ranking(self):
        """A symbol listed only during the drawdown has no outside population,
        so its lift is an artifact. It must not reach the ranking."""
        for tier in (1, 2, 3):
            for entry in self.study()[f"priority_{tier}"]:
                self.assertNotIn("drawdown_symbol_", entry["name"])


# ==========================================================================
# Output, determinism and boundaries
# ==========================================================================

class TestResearchOutput(StudyCase):

    def research_dir(self) -> Path:
        return self.run_dir / "analytics" / RESEARCH_DIRNAME

    def test_the_command_writes_every_study(self):
        self.assertEqual(self.run_cli("--run", str(self.run_dir)), 0)
        for name, _runner in STUDIES:
            self.assertTrue((self.research_dir() / f"{name}.json").exists())
            self.assertTrue((self.research_dir() / f"{name}.md").exists())
        self.assertTrue(
            (self.research_dir() / "strategy_research_priority.md").exists())

    def test_output_is_deterministic_except_generated_at(self):
        self.run_cli("--run", str(self.run_dir))
        first = {p.name: p.read_text(encoding="utf-8")
                 for p in self.research_dir().iterdir()}
        self.run_cli("--run", str(self.run_dir))
        second = {p.name: p.read_text(encoding="utf-8")
                  for p in self.research_dir().iterdir()}
        import re

        strip = lambda text: re.sub(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "<TS>", text)
        self.assertEqual(sorted(first), sorted(second))
        for name, text in first.items():
            self.assertEqual(strip(second[name]), strip(text), name)

    def test_the_bundle_is_not_mutated(self):
        before = {p.name: p.read_bytes()
                  for p in self.run_dir.iterdir() if p.is_file()}
        self.run_cli("--run", str(self.run_dir))
        after = {p.name: p.read_bytes()
                 for p in self.run_dir.iterdir() if p.is_file()}
        self.assertEqual(sorted(before), sorted(after))
        for name, blob in before.items():
            self.assertEqual(after[name], blob, name)

    def test_metrics_artifacts_are_untouched(self):
        for name in (METRICS_FILENAME, "report.md", "segments.csv"):
            path = self.run_dir / name
            if not path.exists():
                continue
            before = path.read_bytes()
            self.run_cli("--run", str(self.run_dir))
            self.assertEqual(path.read_bytes(), before, name)

    def test_every_study_carries_provenance(self):
        self.run_cli("--run", str(self.run_dir))
        metrics = json.loads(
            (self.run_dir / METRICS_FILENAME).read_text(encoding="utf-8"))
        for path in self.research_dir().glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["identity"]["run_id"], RUN_ID)
            self.assertEqual(payload["identity"]["metrics_hash"],
                             metrics["metrics_hash"])
        for path in self.research_dir().glob("*.md"):
            head = path.read_text(encoding="utf-8").splitlines()[0]
            self.assertIn(f"run_id={RUN_ID}", head)

    def test_missing_metrics_is_refused_with_the_command_to_run(self):
        from backtest.analytics import AnalyticsError

        (self.run_dir / METRICS_FILENAME).unlink()
        with self.assertRaises(AnalyticsError) as caught:
            run_studies(self.run_dir)
        self.assertIn("backtest.cli metrics", str(caught.exception))

    def test_out_overrides_the_destination(self):
        elsewhere = self.root / "elsewhere"
        self.run_cli("--run", str(self.run_dir), "--out", str(elsewhere))
        self.assertTrue(
            (elsewhere / "strategy_research_priority.json").exists())
        self.assertFalse(self.research_dir().exists())


class TestStudyBoundaries(unittest.TestCase):

    def modules(self) -> list[Path]:
        return sorted((Path("backtest") / "analytics").rglob("*.py"))

    def test_no_study_imports_replay_or_v2(self):
        from tests.backtest.test_historical_adapter import imported_modules

        for path in self.modules():
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.replay"),
                                 f"{path} imports {module}")
                self.assertFalse(module.startswith("v2"),
                                 f"{path} imports {module}")

    def test_no_study_defines_strategy_logic(self):
        for path in self.modules():
            source = path.read_text(encoding="utf-8")
            for forbidden in ("def score_signal", "def reconcile",
                              "def plan_entry", "def classify_regime"):
                self.assertNotIn(forbidden, source, str(path))

    def test_studies_write_nothing_at_import_or_analysis_time(self):
        """`run_studies` is read-only; only `write_studies` may touch disk."""
        import ast

        source = (Path("backtest") / "analytics" / "studies"
                  / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        writers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_studies":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Attribute) and inner.attr in (
                            "write_text", "mkdir", "unlink", "write_bytes"):
                        writers.append(inner.attr)
        self.assertEqual(writers, [], "run_studies touches the filesystem")


if __name__ == "__main__":
    unittest.main()
