"""Loading a variant's `strategy_inputs` into a `ScoringConfig`.

Two properties carry the weight:

1. **The control resolves to the default object**, not to a reconstruction of it.
   `assertIs` rather than `assertEqual`, because an equal-but-rebuilt config would
   be a second source of truth for the baseline's behaviour.
2. **Nothing is silently ignored.** A mistyped key that resolved to the control
   would run a full replay and report a null result, which is the most expensive
   failure mode available here — so every unknown key is refused by name, and
   every declared-but-unimplemented block refuses with what is missing.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.experiments.config import ExperimentConfig, ExperimentConfigError
from backtest.experiments.rescore import WEIGHTS
from backtest.experiments.scoring_variant import (
    SCORING_V2_ROOT,
    UnsupportedScoringInput,
    load_scoring_variant,
    scoring_config_from_inputs,
)
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG, ScoringConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestInputResolution(unittest.TestCase):
    def test_empty_inputs_are_the_default_object_itself(self):
        self.assertIs(scoring_config_from_inputs({}), DEFAULT_SCORING_CONFIG)
        self.assertIs(scoring_config_from_inputs({"scoring": {}}),
                      DEFAULT_SCORING_CONFIG)

    def test_weight_override_is_merged_not_substituted(self):
        resolved = scoring_config_from_inputs(
            {"scoring": {"weight_overrides": {"ALL": {"fvg": 0}}}})
        self.assertEqual(resolved.weights["RETRACEMENT"]["fvg"], 0)
        # Everything unmentioned keeps its baseline weight.
        self.assertEqual(resolved.weights["RETRACEMENT"]["sweep"], 2)
        self.assertEqual(resolved.weights["CONTINUATION"]["displacement"], 4)

    def test_explicit_integer_thresholds_load(self):
        resolved = scoring_config_from_inputs(
            {"scoring": {"thresholds": {"CONTINUATION": {"watch": 6}}}})
        self.assertEqual(resolved.thresholds["CONTINUATION"].watch, 6)
        # Unspecified tiers of the same mode are inherited, not reset.
        self.assertEqual(resolved.thresholds["CONTINUATION"].a_plus, 14)

    def test_unknown_strategy_input_block_is_refused(self):
        with self.assertRaises(ExperimentConfigError) as ctx:
            scoring_config_from_inputs({"targt": {"rr_cap": 2.5}})
        self.assertIn("targt", str(ctx.exception))

    def test_unknown_scoring_key_is_refused(self):
        with self.assertRaises(ExperimentConfigError) as ctx:
            scoring_config_from_inputs({"scoring": {"weights": {"fvg": 0}}})
        self.assertIn("weights", str(ctx.exception))

    def test_unknown_component_is_refused_by_the_config_object(self):
        # The seam validates its own vocabulary and raises its own error; the
        # loader below is what translates that into a config-level failure.
        with self.assertRaises(ScoringConfigError) as ctx:
            scoring_config_from_inputs(
                {"scoring": {"weight_overrides": {"ALL": {"fvgg": 0}}}})
        self.assertIn("fvgg", str(ctx.exception))

    def test_the_loader_names_the_variant_that_contains_the_typo(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ExperimentConfig(
                slug="typo", base_strategy_version="2.2.0",
                strategy_inputs={"scoring": {"weight_overrides": {
                    "ALL": {"fvgg": 0}}}},
            ).write(root / "typo" / "config.json")
            with self.assertRaises(ExperimentConfigError) as ctx:
                load_scoring_variant("typo", root=root)
        self.assertIn("typo", str(ctx.exception))
        self.assertIn("fvgg", str(ctx.exception))

    def test_gates_are_refused_as_unimplemented(self):
        with self.assertRaises(UnsupportedScoringInput) as ctx:
            scoring_config_from_inputs(
                {"scoring": {"gates": {"require_fvg": True}}})
        self.assertIn("not implemented", str(ctx.exception))

    def test_a_derived_threshold_anchor_is_refused_as_unimplemented(self):
        with self.assertRaises(UnsupportedScoringInput) as ctx:
            scoring_config_from_inputs(
                {"scoring": {"thresholds": {"mode": "quantile_anchored"}}})
        self.assertIn("quantile_anchored", str(ctx.exception))

    def test_an_empty_gates_block_is_not_a_gate(self):
        self.assertIs(scoring_config_from_inputs({"scoring": {"gates": {}}}),
                      DEFAULT_SCORING_CONFIG)


class TestRoundTrip(unittest.TestCase):
    """Write a config, load it back, resolve it — the full path a variant takes."""

    def test_config_survives_write_load_resolve(self):
        inputs = {"scoring": {"weight_overrides": {
            "ALL": {"momentum_weakening": 1},
            "RETRACEMENT": {"sweep": 0, "weak_sweep": 0},
        }}}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = ExperimentConfig(
                slug="round-trip", base_strategy_version="2.2.0",
                strategy_inputs=inputs, metadata={"note": "not hashed"})
            written.write(root / "round-trip" / "config.json")

            config, resolved = load_scoring_variant("round-trip", root=root)

            self.assertEqual(config.variant_id, written.variant_id)
            self.assertEqual(resolved.weights["RETRACEMENT"]["sweep"], 0)
            self.assertEqual(resolved.weights["RETRACEMENT"]["weak_sweep"], 0)
            for mode in ("CONTINUATION", "RETRACEMENT", "WATCH_ONLY"):
                self.assertEqual(resolved.weights[mode]["momentum_weakening"], 1)
            # A mode-specific override must not leak into another mode.
            self.assertEqual(resolved.weights["CONTINUATION"]["sweep"], 1)

    def test_metadata_changes_do_not_change_the_resolved_config(self):
        base = {"scoring": {"weight_overrides": {"ALL": {"fvg": 0}}}}
        a = scoring_config_from_inputs(base)
        b = scoring_config_from_inputs(json.loads(json.dumps(base)))
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_a_hand_edited_variant_id_is_refused_before_resolution(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "tampered"
            root.mkdir(parents=True)
            (root / "config.json").write_text(json.dumps({
                "schema_version": 1, "slug": "tampered",
                "base_strategy_version": "2.2.0",
                "strategy_inputs": {"scoring": {"weight_overrides": {
                    "ALL": {"fvg": 0}}}},
                "metadata": {},
                "variant_id": "0000000000000000",
            }), encoding="utf-8")
            with self.assertRaises(ExperimentConfigError):
                load_scoring_variant("tampered", root=root.parent)


class TestCommittedScoringV2Family(unittest.TestCase):
    """The real configs on disk, and exactly which of them are runnable today."""

    def variant(self, slug):
        return load_scoring_variant(slug, root=REPO_ROOT / SCORING_V2_ROOT)

    def test_control_resolves_to_the_default_object(self):
        config, resolved = self.variant("s0-control")
        self.assertTrue(config.is_control)
        self.assertIs(resolved, DEFAULT_SCORING_CONFIG)

    def test_s1_zeroes_exactly_the_five_never_fires_components(self):
        _, resolved = self.variant("s1-dead-weights-zero")
        changed = {
            (mode, note)
            for mode, table in resolved.weights.items()
            for note, weight in table.items()
            if DEFAULT_SCORING_CONFIG.weights[mode][note] != weight
        }
        self.assertEqual({note for _, note in changed},
                         {"discount", "premium", "neutral_penalty", "no_ob",
                          "m5_watch_confirm"})
        self.assertTrue(all(resolved.weights[m][n] == 0 for m, n in changed))

    def test_s1_keeps_every_note_in_the_vocabulary(self):
        """Zeroed, not dropped — a future firing must stay visible in `notes`."""
        _, resolved = self.variant("s1-dead-weights-zero")
        for mode in DEFAULT_SCORING_CONFIG.weights:
            self.assertEqual(set(resolved.weights[mode]),
                             set(DEFAULT_SCORING_CONFIG.weights[mode]))

    def test_the_anchored_variants_now_load(self):
        """s2/s3 declared a derived anchor; the integers have since been derived.

        `tests/backtest/test_anchor.py` owns the values themselves. What matters
        here is that resolution no longer refuses them.
        """
        for slug in ("s2-uninformative-zero", "s3a-momentum-zero",
                     "s3b-momentum-positive"):
            with self.subTest(slug=slug):
                _, resolved = self.variant(slug)
                for mode in ("CONTINUATION", "RETRACEMENT"):
                    self.assertIsNotNone(resolved.thresholds[mode].watch)

    def test_the_gated_variants_still_refuse_with_what_is_missing(self):
        """Only the FVG gate remains unimplemented, and it says so."""
        for slug in ("s4-fvg-gate", "s5-scoring-v2"):
            with self.subTest(slug=slug):
                with self.assertRaises(UnsupportedScoringInput):
                    self.variant(slug)


class TestTableAgreement(unittest.TestCase):
    """The two independently hand-read weight tables must agree.

    `rescore.WEIGHTS` is a separate reading of the same source, used to decompose
    recorded scores. Keeping the duplication and asserting equality is deliberate:
    two independent readings that must agree is a check, one shared table is a
    tautology (see `seam_design.md`).
    """

    def test_rescore_table_equals_the_seam_default(self):
        for mode, table in WEIGHTS.items():
            with self.subTest(mode=mode):
                self.assertEqual(dict(DEFAULT_SCORING_CONFIG.weights[mode]), table)

    def test_neither_table_has_a_mode_the_other_lacks(self):
        self.assertEqual(set(WEIGHTS), set(DEFAULT_SCORING_CONFIG.weights))


if __name__ == "__main__":
    unittest.main()