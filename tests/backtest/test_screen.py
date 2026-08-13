"""Tier-0 screening of scoring variants.

The properties that matter are the ones whose failure would produce a plausible
but wrong recommendation:

- the **canonical key** must join, or every rule silently becomes the identity
  rule and every variant reports "no effect" (this happened once — 118,201 of
  118,201 records unjoined, because the key included `cycle_index` and the
  dataset's does not);
- the **attribution** must be exact, because separating quality from exposure is
  the entire point of the exercise;
- the **control** must be deterministic, or the benchmark moves under the variant
  it is judging;
- the **coverage** count must be honest about what tier 0 cannot see.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from backtest.dataset import LabeledDataset, canonical_key
from backtest.experiments.screen import (
    Coverage,
    VariantBook,
    _attribution,
    _control_rule,
    _ledger_view,
    decisions_for,
    drawdown,
)
from backtest.experiments.tier0 import AdmissionRule, key_of
from backtest.outcome.record import OutcomeRecord
from v2.analysis.scoring_config import DEFAULT_SCORING_CONFIG

from tests.backtest.test_experiments import RUN_ID, _outcome, _row

REPO_ROOT = Path(__file__).resolve().parents[2]
SCREENS = REPO_ROOT / "experiments/v2_core_rebuild/scoring_v2/screen"

SLUGS = ("s2-uninformative-zero", "s3a-momentum-zero", "s3b-momentum-positive")


class TestCanonicalKeyJoin(unittest.TestCase):
    """The join that silently broke everything once."""

    def test_key_of_omits_cycle_index_like_the_dataset(self):
        record = _outcome(0, cycle=7)
        self.assertEqual(
            key_of(record),
            canonical_key(record.run_id, record.symbol, record.h1_last_ts))
        self.assertIsNone(key_of(record)[3])

    def test_key_of_matches_the_dataset_label_index(self):
        record = _outcome(0, cycle=3)
        dataset = LabeledDataset(records=(record,), labels={
            canonical_key(record.run_id, record.symbol, record.h1_last_ts):
                {"score": 12}})
        self.assertEqual(dataset.labels_for(record), {"score": 12})
        self.assertIn(key_of(record), dataset.labels)

    def test_two_records_at_different_cycles_do_not_collide(self):
        a, b = _outcome(0, cycle=1), _outcome(1, cycle=2)
        self.assertNotEqual(key_of(a), key_of(b))


class TestDrawdown(unittest.TestCase):
    def test_a_monotonically_rising_curve_has_no_drawdown(self):
        self.assertEqual(drawdown([1.0, 1.0, 1.0]), 0.0)

    def test_drawdown_is_peak_to_trough_not_worst_trade(self):
        # Cumulative: 5, 3, 1, 4. Peak 5, trough 1 -> 4.
        self.assertEqual(drawdown([5.0, -2.0, -2.0, 3.0]), 4.0)

    def test_a_losing_book_measures_from_the_zero_peak(self):
        self.assertEqual(drawdown([-1.0, -1.0]), 2.0)

    def test_empty_series(self):
        self.assertEqual(drawdown([]), 0.0)


class TestVariantBook(unittest.TestCase):
    def test_metrics(self):
        book = VariantBook.of("x", [2.0, -1.0, -1.0, 2.0])
        self.assertEqual(book.n, 4)
        self.assertAlmostEqual(book.total_r, 2.0)
        self.assertAlmostEqual(book.expectancy, 0.5)
        self.assertAlmostEqual(book.win_rate, 0.5)
        self.assertAlmostEqual(book.max_drawdown_r, 2.0)
        self.assertAlmostEqual(book.recovery_factor, 1.0)

    def test_no_drawdown_leaves_recovery_undefined_rather_than_infinite(self):
        book = VariantBook.of("x", [1.0, 1.0])
        self.assertEqual(book.max_drawdown_r, 0.0)
        self.assertIsNone(book.recovery_factor)

    def test_an_empty_book_reports_none_not_zero(self):
        book = VariantBook.of("x", [])
        self.assertEqual(book.n, 0)
        self.assertIsNone(book.expectancy)
        self.assertIsNone(book.win_rate)


class TestAttribution(unittest.TestCase):
    """The split must be exact — it is the deliverable, not a diagnostic."""

    def test_terms_sum_to_the_total_delta(self):
        baseline = VariantBook.of("b", [1.0, -1.0, 1.0, -1.0, 2.0])
        variant = VariantBook.of("v", [1.0, 1.0, 2.0])
        split = _attribution(baseline, variant)
        self.assertAlmostEqual(
            split["exposure_term_r"] + split["quality_term_r"],
            split["delta_total_r"], places=6)
        self.assertAlmostEqual(split["residual_r"], 0.0, places=6)

    def test_pure_volume_change_is_all_exposure(self):
        """Same expectancy, fewer trades -> zero quality term."""
        baseline = VariantBook.of("b", [1.0, -1.0, 1.0, -1.0])
        variant = VariantBook.of("v", [1.0, -1.0])
        split = _attribution(baseline, variant)
        self.assertAlmostEqual(split["delta_expectancy"], 0.0)
        self.assertAlmostEqual(split["quality_term_r"], 0.0)
        self.assertAlmostEqual(split["exposure_term_r"], split["delta_total_r"])

    def test_pure_quality_change_is_all_quality(self):
        """Same trade count, better trades -> zero exposure term."""
        baseline = VariantBook.of("b", [1.0, -1.0])
        variant = VariantBook.of("v", [2.0, 0.0])
        split = _attribution(baseline, variant)
        self.assertEqual(split["delta_trades"], 0)
        self.assertAlmostEqual(split["exposure_term_r"], 0.0)
        self.assertAlmostEqual(split["quality_term_r"], split["delta_total_r"])

    def test_an_empty_book_reports_unavailable_rather_than_zero(self):
        self.assertFalse(
            _attribution(VariantBook.of("b", []), VariantBook.of("v", []))[
                "available"])


class TestControlRule(unittest.TestCase):
    def _decisions(self, scores):
        from backtest.experiments.screen import Decisions
        out = Decisions()
        for i, score in enumerate(scores):
            out.scores[canonical_key(RUN_ID, f"SYM{i}", 1000 + i)] = (score, score)
        return out

    def test_rejects_the_lowest_scoring_trades(self):
        decisions = self._decisions([15, 9, 12, 8])
        rule = _control_rule(None, decisions, 2)
        rejected = {key[1] for key in rule.reject_keys}
        self.assertEqual(rejected, {"SYM3", "SYM1"})       # scores 8 and 9

    def test_rejecting_zero_leaves_the_identity_rule(self):
        rule = _control_rule(None, self._decisions([1, 2]), 0)
        self.assertTrue(rule.is_identity)

    def test_the_control_is_deterministic_under_score_ties(self):
        decisions = self._decisions([7, 7, 7, 7])
        first = _control_rule(None, decisions, 2).reject_keys
        second = _control_rule(None, decisions, 2).reject_keys
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)


class TestRejectKeys(unittest.TestCase):
    def test_a_key_set_makes_a_rule_non_identity(self):
        self.assertTrue(AdmissionRule(name="x").is_identity)
        self.assertFalse(
            AdmissionRule(name="x",
                          reject_keys=frozenset({(RUN_ID, "A", 1, None)})
                          ).is_identity)

    def test_the_digest_ignores_set_order(self):
        keys = [(RUN_ID, "A", 1, None), (RUN_ID, "B", 2, None)]
        a = AdmissionRule(name="x", reject_keys=frozenset(keys))
        b = AdmissionRule(name="x", reject_keys=frozenset(reversed(keys)))
        self.assertEqual(a.reject_keys_digest, b.reject_keys_digest)
        self.assertIsNotNone(a.reject_keys_digest)

    def test_a_different_set_gives_a_different_digest(self):
        a = AdmissionRule(name="x", reject_keys=frozenset({(RUN_ID, "A", 1, None)}))
        b = AdmissionRule(name="x", reject_keys=frozenset({(RUN_ID, "B", 1, None)}))
        self.assertNotEqual(a.reject_keys_digest, b.reject_keys_digest)

    def test_an_empty_set_has_no_digest(self):
        self.assertIsNone(AdmissionRule(name="x").reject_keys_digest)

    def test_to_dict_reports_size_and_digest_not_the_keys(self):
        rule = AdmissionRule(name="x",
                             reject_keys=frozenset({(RUN_ID, "A", 1, None)}))
        payload = rule.to_dict()
        self.assertEqual(payload["reject_keys"], 1)
        self.assertIsInstance(payload["reject_keys_digest"], str)
        json.dumps(payload)          # must stay serialisable


class TestLedgerView(unittest.TestCase):
    def test_levels_come_from_the_outcome_not_the_rounded_label(self):
        record = _outcome(0)
        view = _ledger_view(record, {"score": 12, "notes": [], "rr": 2.0})
        self.assertEqual(view["entry_price"], record.entry_price)
        self.assertEqual(view["sl"], record.sl)
        self.assertEqual(view["tp"], record.tp)
        self.assertEqual(view["entry_mode"], record.entry_mode)


class TestDecisionsAreConservative(unittest.TestCase):
    """An unevaluable row must not be rejected — that would fake a filter."""

    def test_an_unjoined_record_is_counted_not_rejected(self):
        dataset = LabeledDataset(records=(_outcome(0),), labels={})
        decisions = decisions_for(dataset, DEFAULT_SCORING_CONFIG)
        self.assertEqual(decisions.unjoined, 1)
        self.assertEqual(decisions.rejected_keys, set())

    def test_a_row_failing_the_decomposition_identity_is_not_rejected(self):
        record = _outcome(0)
        dataset = LabeledDataset(records=(record,), labels={
            key_of(record): {"score": 999, "notes": ["fvg"], "rr": 2.0}})
        decisions = decisions_for(dataset, DEFAULT_SCORING_CONFIG)
        self.assertEqual(decisions.unverified, 1)
        self.assertEqual(decisions.rejected_keys, set())

    def test_the_default_config_rejects_nothing(self):
        """The control config must reproduce the baseline's own decisions."""
        row = _row()
        record = _outcome(0)
        dataset = LabeledDataset(records=(record,), labels={
            key_of(record): {
                "score": row["score"], "notes": row["notes"],
                "rr": row["rr"], "reason": row["reason"],
                "displacement": row["displacement"]}})
        decisions = decisions_for(dataset, DEFAULT_SCORING_CONFIG)
        self.assertEqual(decisions.unverified, 0)
        self.assertEqual(decisions.evaluated, 1)
        self.assertEqual(decisions.rejected_keys, set())


class TestCoverage(unittest.TestCase):
    def test_observable_fraction_is_removals_over_moved_rows(self):
        coverage = Coverage(scored_rows=100, skip_to_admit=30,
                            admit_to_skip=10, unchanged=60, unverified=0)
        self.assertAlmostEqual(coverage.observable_fraction, 0.25)
        self.assertEqual(coverage.net_admission_change, 20)

    def test_no_moved_rows_leaves_the_fraction_undefined(self):
        coverage = Coverage(scored_rows=10, skip_to_admit=0, admit_to_skip=0,
                            unchanged=10, unverified=0)
        self.assertIsNone(coverage.observable_fraction)

    def test_a_purely_subtractive_variant_is_fully_observable(self):
        coverage = Coverage(scored_rows=10, skip_to_admit=0, admit_to_skip=4,
                            unchanged=6, unverified=0)
        self.assertAlmostEqual(coverage.observable_fraction, 1.0)


class TestCommittedScreens(unittest.TestCase):
    """The screens on disk, and the claims they are allowed to make."""

    def payload(self, slug):
        path = SCREENS / f"screen_{slug}.json"
        self.assertTrue(path.exists(), f"missing screen for {slug}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_screen_declares_it_cannot_promote(self):
        for slug in SLUGS:
            with self.subTest(slug=slug):
                payload = self.payload(slug)
                self.assertFalse(payload["is_replay"])
                self.assertIn("cannot_promote", payload)
                self.assertEqual(
                    payload["verdict"]["promotion_authority"], "none (tier 0)")

    def test_the_identity_rule_validated_on_every_screen(self):
        for slug in SLUGS:
            with self.subTest(slug=slug):
                validation = self.payload(slug)["validation"]
                self.assertEqual(validation["identity_rule_slack"], 0)
                self.assertEqual(validation["identity_rule_promotions"], 0)
                self.assertTrue(validation["identity_net_r_matches_baseline"])

    def test_the_join_succeeded_on_every_screen(self):
        for slug in SLUGS:
            with self.subTest(slug=slug):
                decisions = self.payload(slug)["decisions"]
                self.assertEqual(decisions["unjoined"], 0)
                self.assertEqual(decisions["unverified"], 0)
                self.assertEqual(decisions["evaluated"], 118201)

    def test_attribution_terms_sum_exactly(self):
        for slug in SLUGS:
            for comparison, split in self.payload(slug)["attribution"].items():
                with self.subTest(slug=slug, comparison=comparison):
                    self.assertTrue(split["available"])
                    self.assertAlmostEqual(
                        split["exposure_term_r"] + split["quality_term_r"],
                        split["delta_total_r"], places=2)

    #: Whether tier 0 can see most of what each variant changes. This is the
    #: headline caveat and it differs per variant, which is the whole reason it is
    #: measured rather than assumed: a net-more-selective variant is mostly
    #: removals and therefore mostly screenable, while a net-more-permissive one
    #: is not. Pinned so it cannot be quietly forgotten.
    OBSERVES_MAJORITY = {
        "s2-uninformative-zero": False,      # net +29,957 admissions
        "s3a-momentum-zero": False,          # net +16,666 admissions
        "s3b-momentum-positive": True,       # net -11,507 admissions
    }

    def test_coverage_matches_the_direction_of_each_variant(self):
        for slug, observes in self.OBSERVES_MAJORITY.items():
            with self.subTest(slug=slug):
                payload = self.payload(slug)
                coverage = payload["coverage"]
                self.assertEqual(
                    payload["verdict"]["screen_observes_majority_of_change"],
                    observes)
                if observes:
                    self.assertLess(coverage["net_admission_change"], 0)
                    self.assertGreater(coverage["admit_to_skip_observable"],
                                       coverage["skip_to_admit_invisible"])
                else:
                    self.assertGreater(coverage["net_admission_change"], 0)
                    self.assertGreater(coverage["skip_to_admit_invisible"],
                                       coverage["admit_to_skip_observable"])

    def test_no_variant_is_fully_observable(self):
        """Each one moves rows in both directions, so none is a pure filter."""
        for slug in SLUGS:
            with self.subTest(slug=slug):
                coverage = self.payload(slug)["coverage"]
                self.assertGreater(coverage["skip_to_admit_invisible"], 0)
                self.assertLess(coverage["observable_fraction_of_moved_rows"], 1.0)

    def test_substitution_is_reported_and_material(self):
        """The naive delta must differ from the slot-aware one, or why bother."""
        for slug in SLUGS:
            with self.subTest(slug=slug):
                substitution = self.payload(slug)["substitution"]
                self.assertLess(substitution["substitution_cost"], 0.0)
                self.assertNotAlmostEqual(
                    substitution["delta_net_r"],
                    substitution["delta_if_substitution_ignored"], places=1)


if __name__ == "__main__":
    unittest.main()