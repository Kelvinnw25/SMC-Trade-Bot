"""The scoring seam: `ScoringConfig`, and `score_signal` threading it.

Four things are worth testing here and one of them matters far more than the
others. The one that matters is **identity**: the default config must reproduce
the pre-seam engine exactly, because 886,954 recorded ledger rows were produced
by that engine and every experiment is measured against them. The behavioural
half of that claim is checked against V1 itself by
`tests/analysis/test_scoring.py` (which now exercises the seam on every call);
what is checked here is that the *table* still says what the engine used to say,
literal by literal, and that the override machinery cannot silently do nothing.
"""
from __future__ import annotations

import unittest

from v2.analysis.pipeline import AnalysisPipeline, default_stages, scoring_config_of
from v2.analysis.scoring import ScoringStage, score_signal
from v2.analysis.scoring_config import (
    ALL_MODES,
    COMPONENT_ARMS,
    DEFAULT_SCORING_CONFIG,
    ModeThresholds,
    ScoreAccumulator,
    ScoringConfig,
    ScoringConfigError,
)


def htf_map(**kw):
    base = {
        "weekly_bias": "BULLISH",
        "d1_bias": "BULLISH",
        "h4_bias": "BULLISH",
        "location": "equilibrium",
        "momentum_weakening": False,
    }
    base.update(kw)
    return base


def h1_setup(**kw):
    base = {
        "direction": "LONG",
        "entry_mode": "RETRACEMENT",
        "entry_source": "retracement_ob",
        "sweep_valid": True,
        "fvg_valid": True,
        "displacement_valid": True,
        "fresh_ob": True,
        "volume_spike": True,
        "target_clear": True,
        "session_active": True,
        "htf_context_clean": True,
        # rr = 2.0: above the 1.2 floor, so the row reaches the tier logic rather
        # than taking `score_signal`'s `bad_rr` early return.
        "entry_price": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "tp_source": "SWING_HIGH",
        "tp_rr": 1.0,
    }
    base.update(kw)
    return base


class TestDefaultIsTheLiteralTable(unittest.TestCase):
    """The default table, checked against the literals the engine used to hold.

    Written out longhand rather than derived: a table that verifies itself
    against its own source is a tautology. These numbers come from the pre-seam
    `score_signal` (v2/analysis/scoring.py:136-356) and from V1's
    `strategy.py:761-1071` behind it.
    """

    SHARED = {
        "weekly_aligned": 2, "d1_aligned": 2, "h4_aligned": 1,
        "discount": 2, "premium": 2,
        "target_clear": 1, "session_active": 1, "htf_context_clean": 1,
        "expansion": 1, "squeeze": 1, "neutral_penalty": -2,
        "momentum_weakening": -1,
    }
    PER_MODE = {
        "CONTINUATION": {
            "weak_volume": -2, "sweep": 1, "fvg": 1, "displacement": 4,
            "fresh_ob": 1, "m5_continuation_confirm": 1,
        },
        "RETRACEMENT": {
            "sweep": 2, "weak_sweep": -1, "fvg": 1, "displacement": 1,
            "fresh_ob": 2, "no_ob": -1, "m5_choch": 2,
        },
        "WATCH_ONLY": {
            "sweep": 1, "fvg": 1, "displacement": 1, "fresh_ob": 1,
            "m5_watch_confirm": 1,
        },
    }

    def test_weight_tables_are_exactly_the_engine_literals(self):
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(
                    dict(DEFAULT_SCORING_CONFIG.weights[mode]),
                    {**self.SHARED, **self.PER_MODE[mode]},
                )

    def test_thresholds_are_exactly_the_engine_literals(self):
        self.assertEqual(
            DEFAULT_SCORING_CONFIG.thresholds["CONTINUATION"],
            ModeThresholds(a_plus=14, valid=11, watch=8))
        self.assertEqual(
            DEFAULT_SCORING_CONFIG.thresholds["RETRACEMENT"],
            ModeThresholds(a_plus=15, valid=12, watch=9,
                           a_plus_requires_displacement=True))
        self.assertEqual(
            DEFAULT_SCORING_CONFIG.thresholds["WATCH_ONLY"],
            ModeThresholds(watch=12))

    def test_rr_and_regime_literals(self):
        c = DEFAULT_SCORING_CONFIG
        self.assertEqual(c.rr_floor, 1.2)
        self.assertEqual(c.rr_bands, ((3.0, 2), (2.0, 1), (1.5, 0)))
        self.assertEqual(c.choppy_penalty, 2)
        self.assertEqual(c.expansion_regimes, ("expansion_up", "expansion_down"))
        self.assertEqual(c.squeeze_regimes, ("accumulation", "distribution"))
        self.assertEqual(c.penalty_regimes, ("neutral", "compression", "unknown"))
        self.assertEqual(c.continuation_entry_source, "continuation_micro_pullback")
        self.assertEqual(c.default_entry_mode, "RETRACEMENT")

    def test_alignment_literals(self):
        self.assertEqual(dict(DEFAULT_SCORING_CONFIG.alignment["LONG"]),
                         {"bias": "BULLISH", "location": "discount"})
        self.assertEqual(dict(DEFAULT_SCORING_CONFIG.alignment["SHORT"]),
                         {"bias": "BEARISH", "location": "premium"})


class TestDecisionTiers(unittest.TestCase):
    """`decide` reproduces the engine's branch order, including V1's quirk."""

    def d(self, mode, score, disp=False):
        return DEFAULT_SCORING_CONFIG.decide(mode, score, disp)

    def test_continuation_bands(self):
        for score, expected in ((14, "A+"), (13, "VALID"), (11, "VALID"),
                                (10, "WATCH"), (8, "WATCH"), (7, "SKIP")):
            self.assertEqual(self.d("CONTINUATION", score), expected, score)

    def test_retracement_a_plus_needs_displacement(self):
        self.assertEqual(self.d("RETRACEMENT", 15, True), "A+")
        # V1's quirk: at or above the A+ threshold without displacement the row
        # is published VALID, not demoted into the `valid` band.
        self.assertEqual(self.d("RETRACEMENT", 15, False), "VALID")
        self.assertEqual(self.d("RETRACEMENT", 99, False), "VALID")

    def test_retracement_lower_bands(self):
        for score, expected in ((12, "VALID"), (11, "WATCH"), (9, "WATCH"),
                                (8, "SKIP")):
            self.assertEqual(self.d("RETRACEMENT", score), expected, score)

    def test_watch_only_has_no_upper_tiers(self):
        self.assertEqual(self.d("WATCH_ONLY", 99), "WATCH")
        self.assertEqual(self.d("WATCH_ONLY", 12), "WATCH")
        self.assertEqual(self.d("WATCH_ONLY", 11), "SKIP")


class TestRrBands(unittest.TestCase):
    def test_first_match_wins_and_below_all_bands_is_none(self):
        band = DEFAULT_SCORING_CONFIG.rr_band_weight
        self.assertEqual(band(5.0), 2)
        self.assertEqual(band(3.0), 2)
        self.assertEqual(band(2.999), 1)
        self.assertEqual(band(2.0), 1)
        self.assertEqual(band(1.5), 0)
        # None means "append no note at all" — distinct from a band worth 0.
        self.assertIsNone(band(1.4999))


class TestAwardSemantics(unittest.TestCase):
    """Weight 0 keeps the note; an absent key removes score AND note."""

    def test_zero_weight_keeps_the_note(self):
        acc = ScoreAccumulator({"fvg": 0})
        acc.award("fvg")
        self.assertEqual(acc.score, 0)
        self.assertEqual(acc.notes, ["fvg"])

    def test_absent_component_leaves_no_trace(self):
        acc = ScoreAccumulator({})
        acc.award("fvg")
        self.assertEqual(acc.score, 0)
        self.assertEqual(acc.notes, [])

    def test_zero_weight_component_still_appears_in_the_scored_result(self):
        """The whole reason the never-fires class is zeroed instead of deleted."""
        zeroed = DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"htf_context_clean": 0}})
        base = score_signal(htf_map(), "rotational", h1_setup())
        after = score_signal(htf_map(), "rotational", h1_setup(), config=zeroed)

        self.assertIn("htf_context_clean", base["notes"])
        self.assertIn("htf_context_clean", after["notes"])
        self.assertEqual(after["score"], base["score"] - 1)

    def test_dropped_component_disappears_from_the_scored_result(self):
        dropped = DEFAULT_SCORING_CONFIG.with_overrides(
            drop_components=["htf_context_clean"])
        base = score_signal(htf_map(), "rotational", h1_setup())
        after = score_signal(htf_map(), "rotational", h1_setup(), config=dropped)

        self.assertIn("htf_context_clean", base["notes"])
        self.assertNotIn("htf_context_clean", after["notes"])
        self.assertEqual(after["score"], base["score"] - 1)

    def test_note_order_is_unchanged_by_a_weight_override(self):
        """A weight change must not move a note: `notes` is a recorded field."""
        reweighted = DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"sweep": 7}})
        self.assertEqual(
            score_signal(htf_map(), "rotational", h1_setup(),
                         config=reweighted)["notes"],
            score_signal(htf_map(), "rotational", h1_setup())["notes"])


class TestOverrideRejection(unittest.TestCase):
    def test_unknown_component_is_rejected(self):
        with self.assertRaises(ScoringConfigError) as ctx:
            DEFAULT_SCORING_CONFIG.with_overrides(
                weight_overrides={"ALL": {"sweeep": 0}})
        self.assertIn("sweeep", str(ctx.exception))

    def test_component_absent_from_the_named_mode_is_rejected(self):
        # `no_ob` exists only in RETRACEMENT; naming it under CONTINUATION is a
        # mistake, and a silent no-op would look like a null result.
        with self.assertRaises(ScoringConfigError):
            DEFAULT_SCORING_CONFIG.with_overrides(
                weight_overrides={"CONTINUATION": {"no_ob": 0}})

    def test_all_scope_applies_only_where_the_component_exists(self):
        applied = DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"no_ob": 0}})
        self.assertEqual(applied.weights["RETRACEMENT"]["no_ob"], 0)
        self.assertNotIn("no_ob", applied.weights["CONTINUATION"])

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ScoringConfigError):
            DEFAULT_SCORING_CONFIG.with_overrides(
                weight_overrides={"SCALP": {"fvg": 0}})

    def test_unknown_threshold_key_and_mode_are_rejected(self):
        with self.assertRaises(ScoringConfigError):
            DEFAULT_SCORING_CONFIG.with_overrides(
                thresholds={"RETRACEMENT": {"a_pluss": 15}})
        with self.assertRaises(ScoringConfigError):
            DEFAULT_SCORING_CONFIG.with_overrides(thresholds={"SCALP": {"watch": 1}})

    def test_dropping_an_unknown_component_is_rejected(self):
        with self.assertRaises(ScoringConfigError):
            DEFAULT_SCORING_CONFIG.with_overrides(drop_components=["nope"])

    def test_bool_is_not_an_int_weight(self):
        with self.assertRaises(ScoringConfigError):
            ScoringConfig(
                weights={m: {"fvg": True} for m in ALL_MODES},
                thresholds={m: ModeThresholds(watch=1) for m in ALL_MODES},
                alignment=dict(DEFAULT_SCORING_CONFIG.alignment),
                expansion_regimes=(), squeeze_regimes=(), penalty_regimes=(),
                rr_floor=1.2, rr_bands=(), choppy_penalty=2)


class TestImmutability(unittest.TestCase):
    def test_fields_cannot_be_rebound(self):
        with self.assertRaises(Exception):
            DEFAULT_SCORING_CONFIG.rr_floor = 2.0  # type: ignore[misc]

    def test_weight_tables_cannot_be_mutated_in_place(self):
        with self.assertRaises(TypeError):
            DEFAULT_SCORING_CONFIG.weights["RETRACEMENT"]["fvg"] = 99  # type: ignore

    def test_overrides_do_not_touch_the_default(self):
        DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"fvg": 0}}, drop_components=["sweep"])
        self.assertEqual(DEFAULT_SCORING_CONFIG.weights["RETRACEMENT"]["fvg"], 1)
        self.assertEqual(DEFAULT_SCORING_CONFIG.weights["RETRACEMENT"]["sweep"], 2)

    def test_component_arms_is_a_read_only_map(self):
        self.assertEqual(COMPONENT_ARMS["sweep"], ("sweep", "weak_sweep"))
        with self.assertRaises(TypeError):
            COMPONENT_ARMS["sweep"] = ()  # type: ignore[index]


class TestThreading(unittest.TestCase):
    """The three documented seams, and that omitting the argument changes nothing."""

    def test_explicit_default_equals_omitting_the_argument(self):
        for mode in ALL_MODES:
            with self.subTest(mode=mode):
                setup = h1_setup(
                    entry_mode=mode,
                    entry_source="continuation_micro_pullback"
                    if mode == "CONTINUATION" else "retracement_ob")
                self.assertEqual(
                    score_signal(htf_map(), "expansion_up", setup),
                    score_signal(htf_map(), "expansion_up", setup,
                                 config=DEFAULT_SCORING_CONFIG))

    def test_stage_defaults_to_the_default_config(self):
        self.assertIs(ScoringStage().config, DEFAULT_SCORING_CONFIG)

    def test_stage_carries_the_config_it_was_given(self):
        variant = DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"fvg": 0}})
        self.assertIs(ScoringStage(variant).config, variant)

    def test_pipeline_default_threads_the_config_to_the_scoring_stage(self):
        variant = DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"fvg": 0}})
        pipeline = AnalysisPipeline.default(variant)
        self.assertIs(pipeline.scoring_config, variant)
        self.assertIs(pipeline.stages[-1].config, variant)
        self.assertEqual(default_stages(variant)[-1].config, variant)

    def test_pipeline_default_without_a_config_is_the_default(self):
        self.assertIs(AnalysisPipeline.default().scoring_config,
                      DEFAULT_SCORING_CONFIG)

    def test_scoring_config_of_reads_through_any_pipeline_shaped_object(self):
        """The replay harness wraps the pipeline; it forwards `.stages` only."""
        variant = DEFAULT_SCORING_CONFIG.with_overrides(
            weight_overrides={"ALL": {"fvg": 0}})

        class Wrapper:
            def __init__(self, inner):
                self._inner = inner

            @property
            def stages(self):
                return self._inner.stages

        self.assertIs(
            scoring_config_of(Wrapper(AnalysisPipeline.default(variant))), variant)

    def test_scoring_config_of_a_pipeline_without_scoring_is_the_default(self):
        self.assertIs(scoring_config_of(AnalysisPipeline([])),
                      DEFAULT_SCORING_CONFIG)


if __name__ == "__main__":
    unittest.main()