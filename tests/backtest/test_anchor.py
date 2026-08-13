"""Quantile-anchored threshold derivation.

The derivation writes integers into a config that will drive a 7.8-hour replay,
so the properties worth pinning are the ones that would corrupt a result silently:

- **determinism** — the same ledger must produce the same integers, or the
  variant_id is not a behaviour identity;
- **the tie rule** — declared as "upward", and the direction matters because the
  higher threshold admits fewer rows;
- **no outcome data** — the anchor must not read R, or "we did not optimise for
  expectancy" is an unverified claim;
- **the committed configs** — the three anchored variants must actually load and
  carry the integers the derivation reported.
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.experiments.anchor import (
    AnchorReport,
    ModeAnchor,
    Scored,
    apply_to_config,
    collect,
    derive,
    pick_threshold,
    variant_weights,
    write_report,
)
from backtest.experiments.config import ExperimentConfig, load_config
from backtest.experiments.scoring_variant import load_scoring_variant
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG

REPO_ROOT = Path(__file__).resolve().parents[2]
FAMILY = REPO_ROOT / "experiments/v2_core_rebuild/scoring_v2"
DERIVATION = FAMILY / "derivation"

ANCHORED = {
    "s2-uninformative-zero": {
        "CONTINUATION": {"a_plus": 12, "valid": 9, "watch": 6},
        "RETRACEMENT": {"a_plus": 12, "valid": 9, "watch": 7},
        "WATCH_ONLY": {"watch": 12},
    },
    "s3a-momentum-zero": {
        "CONTINUATION": {"a_plus": 14, "valid": 11, "watch": 9},
        "RETRACEMENT": {"a_plus": 15, "valid": 12, "watch": 9},
        "WATCH_ONLY": {"watch": 12},
    },
    "s3b-momentum-positive": {
        "CONTINUATION": {"a_plus": 15, "valid": 12, "watch": 9},
        "RETRACEMENT": {"a_plus": 16, "valid": 13, "watch": 10},
        "WATCH_ONLY": {"watch": 12},
    },
}


class TestPickThreshold(unittest.TestCase):
    """The one numeric decision the whole derivation rests on."""

    def test_matches_an_exactly_reachable_rate(self):
        scores = list(range(10))                    # 0..9, each 10% of the mass
        # P(>= 5) = 0.5 exactly.
        self.assertEqual(pick_threshold(scores, 0.5), 5)

    def test_ties_resolve_upward(self):
        # Two candidates straddle the target symmetrically: 0 admits 1.0, 1
        # admits 0.0, and a target of 0.5 is equidistant. Upward wins, because the
        # higher threshold admits fewer rows.
        self.assertEqual(pick_threshold([0, 0, 1, 1], 0.5), 1)

    def test_zero_is_a_legal_threshold(self):
        """`0` must not be confused with "no answer" — scores can be 0 or below."""
        self.assertEqual(pick_threshold([-3, -2, -1, 0], 1.0), -3)
        self.assertEqual(pick_threshold([0, 0, 0], 1.0), 0)

    def test_a_gate_restricts_the_numerator_only(self):
        scores = [5, 5, 5, 5]
        gate = [True, True, False, False]
        # Every row clears the score, but only half pass the gate, so the highest
        # reachable rate is 0.5 and a target of 0.5 picks the lowest threshold.
        self.assertEqual(pick_threshold(scores, 0.5, gate=gate), 5)

    def test_empty_population_yields_no_threshold(self):
        self.assertIsNone(pick_threshold([], 0.5))

    def test_unreachable_target_picks_the_closest_available(self):
        # All mass at one score: only rates 1.0 (t<=7) and 0.0 (t>7) exist.
        self.assertEqual(pick_threshold([7] * 10, 0.9), 7)
        self.assertEqual(pick_threshold([7] * 10, 0.1), 8)


class TestVariantWeights(unittest.TestCase):
    def test_no_overrides_is_the_default_object(self):
        self.assertIs(variant_weights(None), DEFAULT_SCORING_CONFIG)
        self.assertIs(variant_weights({}), DEFAULT_SCORING_CONFIG)

    def test_thresholds_are_not_fed_back_into_the_derivation(self):
        """The signature admits weights only — thresholds are the output."""
        import inspect
        params = set(inspect.signature(variant_weights).parameters)
        self.assertEqual(params, {"weight_overrides", "base"})


class TestReportShape(unittest.TestCase):
    def _anchor(self, mode="RETRACEMENT", n=10):
        base = DEFAULT_SCORING_CONFIG.thresholds[mode]
        return ModeAnchor(
            mode=mode, n=n, baseline_thresholds=base, derived_thresholds=base,
            baseline_rates={"watch": 0.5, "valid": 0.2, "a_plus": 0.01},
            variant_rates={"watch": 0.55, "valid": 0.2, "a_plus": 0.01},
            baseline_tiers={"SKIP": 5, "WATCH": 3, "VALID": 2, "A+": 0},
            variant_tiers={"SKIP": 4, "WATCH": 4, "VALID": 2, "A+": 0},
            unchanged=True, brackets={})

    def test_max_rate_delta_is_the_worst_case_over_every_mode_and_tier(self):
        report = AnchorReport(
            slug="x", variant_id="0" * 16, modes=(self._anchor(),), rows=10,
            role_counts={"TRAIN": 10}, unverified=0, identity={})
        self.assertAlmostEqual(report.max_rate_delta, 0.05)

    def test_thresholds_block_omits_tiers_the_mode_does_not_award(self):
        report = AnchorReport(
            slug="x", variant_id="0" * 16,
            modes=(self._anchor("WATCH_ONLY"),), rows=10,
            role_counts={"TRAIN": 10}, unverified=0, identity={})
        self.assertEqual(report.thresholds["WATCH_ONLY"], {"watch": 12})


class TestApplyToConfig(unittest.TestCase):
    def _report(self, variant_id: str, slug: str = "target") -> AnchorReport:
        base = DEFAULT_SCORING_CONFIG.thresholds["CONTINUATION"]
        anchor = ModeAnchor(
            mode="CONTINUATION", n=5, baseline_thresholds=base,
            derived_thresholds=base,
            baseline_rates={t: 0.0 for t in ("watch", "valid", "a_plus")},
            variant_rates={t: 0.0 for t in ("watch", "valid", "a_plus")},
            baseline_tiers={}, variant_tiers={}, unchanged=True, brackets={})
        return AnchorReport(
            slug=slug, variant_id=variant_id, modes=(anchor,), rows=5,
            role_counts={"TRAIN": 5}, unverified=0,
            identity={"run_id": "replay-test", "baseline_id": "baseline-test"})

    def _write(self, root: Path, slug: str = "target") -> ExperimentConfig:
        config = ExperimentConfig(
            slug=slug, base_strategy_version="2.2.0",
            strategy_inputs={"scoring": {
                "weight_overrides": {"ALL": {"sweep": 0}},
                "thresholds": {"mode": "quantile_anchored"}}},
            metadata={"purpose": "test"})
        config.write(root / slug / "config.json")
        return config

    def test_applying_replaces_the_anchor_and_mints_a_new_id(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self._write(root)
            _, before, after = apply_to_config(
                root, "target", self._report(original.variant_id))

            self.assertEqual(before, original.variant_id)
            self.assertNotEqual(after, before)

            updated = load_config(root / "target" / "config.json")
            self.assertEqual(updated.variant_id, after)
            self.assertEqual(
                updated.strategy_inputs["scoring"]["thresholds"],
                {"CONTINUATION": {"a_plus": 14, "valid": 11, "watch": 8}})
            # The weights the variant declared must survive untouched.
            self.assertEqual(
                updated.strategy_inputs["scoring"]["weight_overrides"],
                {"ALL": {"sweep": 0}})

    def test_provenance_records_what_was_replaced(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self._write(root)
            apply_to_config(root, "target", self._report(original.variant_id))
            updated = load_config(root / "target" / "config.json")

            provenance = updated.metadata["threshold_derivation"]
            self.assertEqual(provenance["replaced"], {"mode": "quantile_anchored"})
            self.assertEqual(provenance["partition"], "TRAIN")
            self.assertEqual(provenance["predecessor_variant_id"],
                             original.variant_id)
            self.assertEqual(provenance["anchor_run_id"], "replay-test")
            # Metadata is outside the hash, so provenance cannot move the id.
            self.assertEqual(updated.metadata["purpose"], "test")

    def test_a_report_for_a_different_variant_is_refused(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root)
            with self.assertRaises(Exception) as ctx:
                apply_to_config(root, "target", self._report("f" * 16))
            self.assertIn("changed after the derivation", str(ctx.exception))


class TestDeterminism(unittest.TestCase):
    def test_write_report_is_byte_deterministic_modulo_generated_at(self):
        base = DEFAULT_SCORING_CONFIG.thresholds["CONTINUATION"]
        anchor = ModeAnchor(
            mode="CONTINUATION", n=3, baseline_thresholds=base,
            derived_thresholds=base,
            baseline_rates={t: 0.5 for t in ("watch", "valid", "a_plus")},
            variant_rates={t: 0.5 for t in ("watch", "valid", "a_plus")},
            baseline_tiers={"SKIP": 1}, variant_tiers={"SKIP": 1},
            unchanged=True, brackets={"watch": {"8": 0.5}})
        report = AnchorReport(
            slug="det", variant_id="0" * 16, modes=(anchor,), rows=3,
            role_counts={"TRAIN": 3}, unverified=0,
            identity={"generated_at": "2026-01-01T00:00:00Z"})
        with TemporaryDirectory() as tmp:
            a = write_report(report, Path(tmp) / "a").read_bytes()
            b = write_report(report, Path(tmp) / "b").read_bytes()
        self.assertEqual(a, b)

    def test_rate_computation_does_not_depend_on_row_order(self):
        rows = [Scored("RETRACEMENT", 10, 9, True),
                Scored("RETRACEMENT", 20, 18, False),
                Scored("RETRACEMENT", 5, 4, True)]
        forward = pick_threshold([r.variant_score for r in rows], 0.5)
        backward = pick_threshold([r.variant_score for r in reversed(rows)], 0.5)
        self.assertEqual(forward, backward)


class TestNoOutcomeData(unittest.TestCase):
    """"We did not optimise for expectancy" has to be checkable, not asserted."""

    SOURCE = REPO_ROOT / "backtest/experiments/anchor.py"

    def _code_tokens(self) -> set[str]:
        """Every name, attribute and string literal in *executable* code.

        Docstrings are excluded deliberately: the module's prose explains why it
        anchors on volume rather than expectancy, and a raw substring scan would
        flag that explanation as the thing it is promising not to do.
        """
        tree = ast.parse(self.SOURCE.read_text(encoding="utf-8"))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        tokens: set[str] = set()
        for node in ast.walk(tree):
            if node in docstrings:
                continue
            if isinstance(node, ast.Name):
                tokens.add(node.id)
            elif isinstance(node, ast.Attribute):
                tokens.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                tokens.add(node.value)
        return tokens

    def test_the_module_never_reads_an_outcome_or_an_r_value(self):
        tokens = self._code_tokens()
        for forbidden in ("realized_r", "outcomes.jsonl", "expectancy",
                          "total_r", "OutcomeRecord", "load_outcomes",
                          "win_rate", "max_drawdown_r"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, tokens)

    def test_the_module_imports_no_replay_or_outcome_machinery(self):
        tree = ast.parse(self.SOURCE.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
        for forbidden in ("backtest.replay", "backtest.outcome",
                          "backtest.metrics"):
            self.assertNotIn(forbidden, modules)

    def test_scored_carries_no_return_field(self):
        self.assertEqual(
            set(Scored.__dataclass_fields__),
            {"mode", "baseline_score", "variant_score", "displacement"})


class TestCommittedDerivation(unittest.TestCase):
    """The integers on disk, and that the variants they unblocked now load."""

    def test_each_anchored_config_carries_the_derived_integers(self):
        for slug, expected in ANCHORED.items():
            with self.subTest(slug=slug):
                config = load_config(FAMILY / slug / "config.json")
                self.assertEqual(
                    config.strategy_inputs["scoring"]["thresholds"], expected)

    def test_each_anchored_variant_is_now_runnable(self):
        for slug, expected in ANCHORED.items():
            with self.subTest(slug=slug):
                _, resolved = load_scoring_variant(slug, root=FAMILY)
                for mode, tiers in expected.items():
                    derived = resolved.thresholds[mode]
                    self.assertEqual(derived.watch, tiers["watch"])
                    if "valid" in tiers:
                        self.assertEqual(derived.valid, tiers["valid"])
                        self.assertEqual(derived.a_plus, tiers["a_plus"])

    def test_retracement_keeps_its_displacement_quirk_after_anchoring(self):
        """The A+ predicate is a property of the mode, not of the thresholds."""
        for slug in ANCHORED:
            with self.subTest(slug=slug):
                _, resolved = load_scoring_variant(slug, root=FAMILY)
                self.assertTrue(
                    resolved.thresholds["RETRACEMENT"]
                    .a_plus_requires_displacement)
                self.assertFalse(
                    resolved.thresholds["CONTINUATION"]
                    .a_plus_requires_displacement)

    def test_thresholds_are_ordered(self):
        for slug in ANCHORED:
            _, resolved = load_scoring_variant(slug, root=FAMILY)
            for mode in ("CONTINUATION", "RETRACEMENT"):
                tiers = resolved.thresholds[mode]
                with self.subTest(slug=slug, mode=mode):
                    self.assertGreaterEqual(tiers.a_plus, tiers.valid)
                    self.assertGreaterEqual(tiers.valid, tiers.watch)

    def test_the_derivation_evidence_is_committed_and_matches_the_configs(self):
        for slug, expected in ANCHORED.items():
            with self.subTest(slug=slug):
                path = DERIVATION / f"anchor_{slug}.json"
                self.assertTrue(path.exists(), f"missing evidence for {slug}")
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["thresholds"], expected)
                self.assertFalse(payload["is_replay"])
                self.assertEqual(payload["unverified_rows"], 0)
                self.assertEqual(payload["method"]["partition"], "TRAIN only")

    def test_the_evidence_records_only_train_rows(self):
        for slug in ANCHORED:
            payload = json.loads(
                (DERIVATION / f"anchor_{slug}.json").read_text(encoding="utf-8"))
            with self.subTest(slug=slug):
                self.assertEqual(payload["rows_train"], 267933)
                self.assertEqual(payload["role_counts"],
                                 {"DEV": 105789, "TRAIN": 267933})
                self.assertNotIn("HOLDOUT", payload["role_counts"])

    def test_every_residual_is_bracketed_by_its_neighbours(self):
        """A large residual must be visibly the best available integer."""
        for slug in ANCHORED:
            payload = json.loads(
                (DERIVATION / f"anchor_{slug}.json").read_text(encoding="utf-8"))
            for mode in payload["modes"]:
                if mode["n"] == 0:
                    continue
                for tier in ("watch", "valid", "a_plus"):
                    chosen = mode["derived_thresholds"][tier]
                    target = mode["cumulative_rates"][tier]["baseline"]
                    bracket = mode["bracket"][tier]
                    with self.subTest(slug=slug, mode=mode["mode"], tier=tier):
                        error = abs(bracket[str(chosen)] - target)
                        for other, rate in bracket.items():
                            if int(other) != chosen:
                                self.assertGreaterEqual(
                                    abs(rate - target), error,
                                    f"{other} beats the chosen {chosen}")


if __name__ == "__main__":
    unittest.main()