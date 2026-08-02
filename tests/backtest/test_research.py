"""M11 — walk-forward splits and the permanent holdout.

Every boundary here is hand-computed from the spec, never read back from the
generator: a test that records what `walk_forward` produced would pass just as
happily on arithmetic that leaks a week of training data into the test window.

Coverage follows the milestone's task list — rolling windows, train/validate/OOS,
permanent holdout, strict non-overlap, configuration versioning, reproducible
splits, declare-once immutability, explicit metadata, and loud failure on a
different holdout — plus the properties §11 makes normative and the task list does
not name: the embargo covering `max_holding_h1`, burn-in inside every window, and
walk-forward never producing the canonical baseline (C-5.7.4).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backtest.metrics import ReportInputs, build_report
from backtest.research import (
    DEFAULT_MAX_HOLDING_H1, Holdout, HoldoutMismatch, HoldoutViolation,
    ProtocolError, SplitError, WalkForwardSpec, Window, assert_is_holdout,
    assert_optimization_report, assert_test_report, assert_untouched,
    evaluate_holdout, guard_splits, holdout_uses, report_window, walk_forward,
)
from backtest.research import holdout as holdout_module
from tests.backtest.test_metrics import LABEL_FIELDS, T0, win

HOUR = 3600

#: An origin on an hour boundary, so every window in this file is legal by C-5.1.
ORIGIN = 1_700_000_000 // HOUR * HOUR


def window(cycles: int, *, start: int = ORIGIN, burn_in_h1: int = 0,
           role: str = "") -> Window:
    return Window(start=start, end=start + (cycles - 1) * HOUR,
                  burn_in_h1=burn_in_h1, role=role)


# ==========================================================================
# TASK 6 — windows
# ==========================================================================

class TestWindow(unittest.TestCase):

    def test_a_window_is_inclusive_at_both_ends(self):
        """Matching ReplayConfig.start/end, so conversion is off-by-one free."""
        self.assertEqual(window(1).cycles, 1)
        self.assertEqual(window(500).cycles, 500)
        self.assertEqual(window(500).end, ORIGIN + 499 * HOUR)

    def test_a_boundary_off_the_hour_is_refused(self):
        with self.assertRaises(SplitError):
            Window(start=ORIGIN + 60, end=ORIGIN + 3 * HOUR)

    def test_an_inverted_window_is_refused(self):
        with self.assertRaises(SplitError):
            Window(start=ORIGIN + HOUR, end=ORIGIN)

    def test_burn_in_moves_the_evaluated_start(self):
        w = window(500, burn_in_h1=336)
        self.assertEqual(w.evaluated_start, ORIGIN + 336 * HOUR)
        self.assertEqual(w.evaluated_cycles, 164)

    def test_a_window_that_is_entirely_burn_in_is_refused(self):
        """It would measure nothing, which is worse than failing."""
        with self.assertRaises(SplitError):
            window(336, burn_in_h1=336)

    def test_touching_windows_count_as_overlapping(self):
        """A boundary belongs to one window, or the split is not a split."""
        first = Window(start=ORIGIN, end=ORIGIN + 10 * HOUR)
        second = Window(start=ORIGIN + 10 * HOUR, end=ORIGIN + 20 * HOUR)
        self.assertTrue(first.overlaps(second))
        third = Window(start=ORIGIN + 11 * HOUR, end=ORIGIN + 20 * HOUR)
        self.assertFalse(first.overlaps(third))


class TestSpec(unittest.TestCase):

    def test_the_embargo_defaults_to_the_contract_holding_cap(self):
        """C-9.7 max_holding_h1 = 168. A shorter embargo admits leakage."""
        self.assertEqual(DEFAULT_MAX_HOLDING_H1, 168)
        self.assertEqual(WalkForwardSpec(train_h1=1000, test_h1=500).embargo_h1,
                         168)

    def test_the_step_defaults_to_the_test_length(self):
        """Test windows then tile the period exactly: every hour tested once."""
        spec = WalkForwardSpec(train_h1=1000, test_h1=500)
        self.assertEqual(spec.step, 500)

    def test_a_split_with_no_out_of_sample_window_is_refused(self):
        with self.assertRaises(SplitError):
            WalkForwardSpec(train_h1=1000, test_h1=0)

    def test_a_window_shorter_than_its_burn_in_is_refused(self):
        with self.assertRaises(SplitError):
            WalkForwardSpec(train_h1=100, test_h1=500, burn_in_h1=336)
        with self.assertRaises(SplitError):
            WalkForwardSpec(train_h1=1000, test_h1=100, burn_in_h1=336)

    def test_the_span_accounts_for_every_embargo(self):
        two_gaps = WalkForwardSpec(train_h1=1000, validate_h1=300, test_h1=500,
                                   burn_in_h1=48, embargo_h1=168)
        self.assertEqual(two_gaps.span_h1, 1000 + 300 + 500 + 336)
        one_gap = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48,
                                  embargo_h1=168)
        self.assertEqual(one_gap.span_h1, 1000 + 500 + 168)


# ==========================================================================
# TASK 6 — rolling windows, strict non-overlap, reproducible splits
# ==========================================================================

class TestWalkForward(unittest.TestCase):

    SPEC = WalkForwardSpec(train_h1=1000, validate_h1=300, test_h1=500,
                           burn_in_h1=48, embargo_h1=168)

    def test_the_first_split_lands_exactly_where_the_arithmetic_says(self):
        splits = walk_forward(window(6000), self.SPEC)
        first = splits[0]
        self.assertEqual(first.train.start, ORIGIN)
        self.assertEqual(first.train.end, ORIGIN + 999 * HOUR)
        self.assertEqual(first.validate.start, ORIGIN + (1000 + 168) * HOUR)
        self.assertEqual(first.validate.end, ORIGIN + (1000 + 168 + 299) * HOUR)
        self.assertEqual(first.test.start,
                         ORIGIN + (1000 + 168 + 300 + 168) * HOUR)
        self.assertEqual(first.test.end,
                         ORIGIN + (1000 + 168 + 300 + 168 + 499) * HOUR)

    def test_windows_within_a_split_never_overlap(self):
        for split in walk_forward(window(6000), self.SPEC):
            parts = split.windows
            for i, first in enumerate(parts):
                for second in parts[i + 1:]:
                    self.assertFalse(first.overlaps(second))

    def test_the_embargo_gap_is_exactly_the_declared_length(self):
        split = walk_forward(window(6000), self.SPEC)[0]
        gap = (split.validate.start - split.train.end) // HOUR - 1
        self.assertEqual(gap, 168)
        gap = (split.test.start - split.validate.end) // HOUR - 1
        self.assertEqual(gap, 168)

    def test_test_windows_tile_without_overlapping(self):
        tests = walk_forward(window(9000), self.SPEC).test_windows
        self.assertGreater(len(tests), 2)
        for first, second in zip(tests, tests[1:]):
            self.assertFalse(first.overlaps(second))
            self.assertEqual((second.start - first.start) // HOUR, 500)

    def test_a_step_shorter_than_the_test_window_is_refused(self):
        """Overlapping test windows double-count out-of-sample evidence."""
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, step_h1=100,
                               burn_in_h1=48)
        with self.assertRaises(SplitError) as caught:
            walk_forward(window(9000), spec)
        self.assertIn("double-counted", str(caught.exception))

    def test_rolling_moves_the_training_origin(self):
        splits = walk_forward(window(9000), self.SPEC)
        self.assertEqual(splits[1].train.start - splits[0].train.start,
                         500 * HOUR)
        self.assertEqual(splits[0].train.cycles, splits[1].train.cycles)

    def test_anchored_grows_the_training_window_from_a_fixed_origin(self):
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48,
                               anchored=True)
        splits = walk_forward(window(9000), spec)
        self.assertEqual(splits[0].train.start, splits[1].train.start)
        self.assertEqual(splits[1].train.cycles - splits[0].train.cycles, 500)

    def test_a_trailing_remainder_is_dropped_not_stretched(self):
        """A short final test window is not comparable with the others."""
        splits = walk_forward(window(6000), self.SPEC)
        lengths = {split.test.cycles for split in splits}
        self.assertEqual(lengths, {500})
        self.assertLessEqual(splits[-1].test.end, ORIGIN + 5999 * HOUR)

    def test_a_window_too_short_for_one_split_is_refused(self):
        with self.assertRaises(SplitError) as caught:
            walk_forward(window(100), self.SPEC)
        self.assertIn("does not fit", str(caught.exception).replace(
            "cannot be shortened to fit", "does not fit"))

    def test_every_window_carries_the_burn_in(self):
        for split in walk_forward(window(6000), self.SPEC):
            for part in split.windows:
                self.assertEqual(part.burn_in_h1, 48)
                self.assertEqual(part.evaluated_cycles, part.cycles - 48)

    def test_walk_forward_is_never_the_canonical_baseline(self):
        """C-5.7.4: the baseline must be a single unsharded run."""
        self.assertFalse(walk_forward(window(6000), self.SPEC).is_baseline)

    # -- determinism / versioning -----------------------------------------

    def test_the_same_inputs_produce_identical_splits(self):
        first = walk_forward(window(9000), self.SPEC)
        second = walk_forward(window(9000), self.SPEC)
        self.assertEqual(first.splits_hash, second.splits_hash)
        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True),
                         json.dumps(second.to_dict(), sort_keys=True))

    def test_any_spec_change_changes_the_hash(self):
        base = walk_forward(window(9000), self.SPEC).splits_hash
        for change in (dict(train_h1=1001), dict(validate_h1=301),
                       dict(test_h1=501, step_h1=501), dict(burn_in_h1=49),
                       dict(embargo_h1=169), dict(anchored=True)):
            spec = WalkForwardSpec(**{**self.SPEC.to_dict(), **change})
            self.assertNotEqual(walk_forward(window(9000), spec).splits_hash,
                                base, change)

    def test_a_different_period_changes_the_hash(self):
        first = walk_forward(window(9000), self.SPEC).splits_hash
        second = walk_forward(window(9000, start=ORIGIN + 24 * HOUR),
                              self.SPEC).splits_hash
        self.assertNotEqual(first, second)

    def test_splits_read_no_clock_and_no_data(self):
        """Windows are integer arithmetic: nothing to seed, nothing to reproduce."""
        import ast
        source = Path("backtest/research/splits.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for forbidden in ("random", "time", "datetime", "numpy",
                          "backtest.outcome", "backtest.replay"):
            self.assertNotIn(forbidden, imported, forbidden)

    def test_a_single_variant_may_skip_the_validate_window(self):
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48)
        split = walk_forward(window(6000), spec)[0]
        self.assertIsNone(split.validate)
        self.assertEqual(split.windows, (split.train, split.test))
        self.assertEqual((split.test.start - split.train.end) // HOUR - 1, 168)


# ==========================================================================
# TASK 7 — the permanent holdout
# ==========================================================================

class HoldoutCase(unittest.TestCase):

    SNAPSHOT = "snap000000000000"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def declare(self, *, start=ORIGIN + 20_000 * HOUR, cycles=2000,
                snapshot=None, label="v1", burn_in_h1=336):
        return holdout_module.declare(
            self.root,
            Window(start=start, end=start + (cycles - 1) * HOUR,
                   burn_in_h1=burn_in_h1),
            data_snapshot_id=snapshot or self.SNAPSHOT,
            label=label, reason="declared before A1", now="2026-07-31T00:00:00Z")


class TestHoldoutDeclaration(HoldoutCase):

    def test_declaring_writes_an_immutable_lock_with_explicit_metadata(self):
        declared = self.declare()
        payload = json.loads(
            (self.root / "holdout.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["holdout_id"], declared.holdout_id)
        self.assertEqual(payload["data_snapshot_id"], self.SNAPSHOT)
        self.assertEqual(payload["label"], "v1")
        self.assertEqual(payload["declared_at"], "2026-07-31T00:00:00Z")
        self.assertEqual(payload["window"]["cycles"], 2000)
        self.assertEqual(payload["contract_version"], "1.1")

    def test_redeclaring_the_identical_holdout_is_idempotent(self):
        self.assertEqual(self.declare().holdout_id, self.declare().holdout_id)

    def test_redeclaring_a_different_window_fails_loudly(self):
        self.declare()
        with self.assertRaises(HoldoutViolation) as caught:
            self.declare(start=ORIGIN + 30_000 * HOUR)
        self.assertIn("declared once", str(caught.exception))

    def test_redeclaring_on_a_rebuilt_corpus_fails_loudly(self):
        """C-6.10: the same calendar window over new bytes is a new holdout."""
        self.declare()
        with self.assertRaises(HoldoutViolation):
            self.declare(snapshot="snap111111111111")

    def test_the_identity_excludes_the_declaration_time(self):
        first = self.declare()
        (self.root / "holdout.lock.json").unlink()
        second = holdout_module.declare(
            self.root, first.window, data_snapshot_id=self.SNAPSHOT,
            label="v1", now="2030-01-01T00:00:00Z")
        self.assertEqual(first.holdout_id, second.holdout_id)

    def test_the_identity_includes_the_window_and_the_snapshot(self):
        first = self.declare()
        (self.root / "holdout.lock.json").unlink()
        moved = self.declare(start=ORIGIN + 30_000 * HOUR)
        self.assertNotEqual(first.holdout_id, moved.holdout_id)
        (self.root / "holdout.lock.json").unlink()
        rebuilt = self.declare(snapshot="snap111111111111")
        self.assertNotEqual(first.holdout_id, rebuilt.holdout_id)

    def test_loading_without_a_declaration_fails(self):
        with self.assertRaises(holdout_module.HoldoutError) as caught:
            holdout_module.load(self.root)
        self.assertIn("before the first experiment", str(caught.exception))

    def test_find_returns_none_instead_of_raising(self):
        self.assertIsNone(holdout_module.find(self.root))
        self.declare()
        self.assertIsNotNone(holdout_module.find(self.root))

    def test_the_lock_round_trips(self):
        declared = self.declare()
        loaded = holdout_module.load(self.root)
        self.assertEqual(loaded.holdout_id, declared.holdout_id)
        self.assertEqual(loaded.window.burn_in_h1, 336)
        self.assertEqual(loaded.reason, "declared before A1")


class TestHoldoutEnforcement(HoldoutCase):

    def test_an_overlapping_optimization_window_is_refused(self):
        declared = self.declare()
        overlapping = Window(start=declared.window.end - 5 * HOUR,
                             end=declared.window.end + 100 * HOUR)
        with self.assertRaises(HoldoutViolation) as caught:
            assert_untouched(declared, overlapping)
        self.assertIn("6 hour(s)", str(caught.exception))

    def test_a_single_shared_hour_is_enough_to_refuse(self):
        declared = self.declare()
        touching = Window(start=declared.window.end,
                          end=declared.window.end + 50 * HOUR)
        with self.assertRaises(HoldoutViolation):
            assert_untouched(declared, touching)

    def test_a_clear_window_passes(self):
        declared = self.declare()
        clear = Window(start=ORIGIN, end=declared.window.start - HOUR)
        assert_untouched(declared, clear)

    def test_a_different_evaluation_window_fails_loudly(self):
        declared = self.declare()
        with self.assertRaises(HoldoutMismatch) as caught:
            assert_is_holdout(declared,
                              Window(start=declared.window.start,
                                     end=declared.window.end - HOUR))
        self.assertIn("is not the declared holdout", str(caught.exception))

    def test_the_same_window_on_a_rebuilt_snapshot_fails_loudly(self):
        declared = self.declare()
        with self.assertRaises(HoldoutMismatch) as caught:
            assert_is_holdout(declared, declared.window,
                              data_snapshot_id="snap111111111111")
        self.assertIn("different holdout", str(caught.exception))

    def test_the_declared_window_on_the_declared_snapshot_passes(self):
        declared = self.declare()
        assert_is_holdout(declared, declared.window,
                          data_snapshot_id=self.SNAPSHOT)

    def test_walk_forward_splits_are_guarded_as_a_whole(self):
        declared = self.declare(start=ORIGIN + 3000 * HOUR, cycles=2000)
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48)
        splits = walk_forward(window(3000), spec)
        guard_splits(declared, splits)
        overlapping = walk_forward(window(9000), spec)
        with self.assertRaises(HoldoutViolation):
            guard_splits(declared, overlapping)

    def test_splits_can_be_generated_around_the_holdout(self):
        """The holdout is normally the tail; the splits that reach it vanish."""
        declared = self.declare(start=ORIGIN + 4000 * HOUR, cycles=2000)
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48)
        splits = walk_forward(window(9000), spec,
                              exclude=[declared.window])
        guard_splits(declared, splits)
        self.assertTrue(splits.splits)
        self.assertLess(len(splits), len(walk_forward(window(9000), spec)))

    def test_a_holdout_leaving_no_room_is_refused_rather_than_shrunk(self):
        declared = self.declare(start=ORIGIN, cycles=8000)
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48)
        with self.assertRaises(SplitError) as caught:
            walk_forward(window(9000), spec, exclude=[declared.window])
        self.assertIn("never shrink the holdout", str(caught.exception))


class TestHoldoutAudit(HoldoutCase):

    def test_the_first_look_records_a_use_and_cautions_nothing(self):
        declared = self.declare()
        self.assertIsNone(holdout_module.usage_caution(self.root, declared))
        holdout_module.record_use(
            self.root, declared, run_id="replay-a", strategy_version="v2.0",
            metrics_hash="abc", now="2026-08-01T00:00:00Z")
        uses = holdout_uses(self.root)
        self.assertEqual(len(uses), 1)
        self.assertEqual(uses[0].run_id, "replay-a")
        self.assertEqual(uses[0].holdout_id, declared.holdout_id)

    def test_a_second_look_produces_a_caution_naming_the_count(self):
        declared = self.declare()
        holdout_module.record_use(self.root, declared, run_id="a",
                                  strategy_version="v2.0", metrics_hash="x")
        caution = holdout_module.usage_caution(self.root, declared)
        self.assertIn("already been evaluated 1 time(s)", caution)
        self.assertIn("less out-of-sample", caution)

    def test_the_log_is_append_only(self):
        declared = self.declare()
        for index in range(3):
            holdout_module.record_use(
                self.root, declared, run_id=f"run-{index}",
                strategy_version="v2.0", metrics_hash="x")
        self.assertEqual([use.run_id for use in holdout_uses(self.root)],
                         ["run-0", "run-1", "run-2"])

    def test_uses_of_another_holdout_do_not_count(self):
        declared = self.declare()
        other = Holdout(window=Window(start=ORIGIN, end=ORIGIN + 100 * HOUR),
                        data_snapshot_id="other", label="x")
        holdout_module.record_use(self.root, other, run_id="a",
                                  strategy_version="v2.0", metrics_hash="x")
        self.assertIsNone(holdout_module.usage_caution(self.root, declared))


# ==========================================================================
# The protocol — what a report may be used for
# ==========================================================================

class TestProtocol(HoldoutCase):

    def report(self, start, end, *, snapshot=None, run_id="replay-test",
               version="v2.0"):
        cycles = (end - start) // HOUR + 1
        records = [
            win(2.0, boundary=start + (index % cycles) * HOUR,
                data_snapshot_id=snapshot or self.SNAPSHOT,
                strategy_version=version)
            for index in range(40)
        ]
        records[0] = win(2.0, boundary=start,
                         data_snapshot_id=snapshot or self.SNAPSHOT,
                         strategy_version=version)
        records[1] = win(2.0, boundary=end,
                         data_snapshot_id=snapshot or self.SNAPSHOT,
                         strategy_version=version)
        return build_report(
            records, n_resamples=64,
            inputs=ReportInputs(run_id=run_id, window_start=start,
                                window_end=end, burn_in_h1=336,
                                universe_mode="volume",
                                label_fields=LABEL_FIELDS))

    def test_the_window_comes_from_the_records(self):
        declared = self.declare()
        report = self.report(declared.window.start, declared.window.end)
        found = report_window(report)
        self.assertEqual((found.start, found.end),
                         (declared.window.start, declared.window.end))

    def test_an_empty_report_supports_no_claim(self):
        empty = build_report([], n_resamples=64,
                             inputs=ReportInputs(run_id="r"))
        with self.assertRaises(ProtocolError):
            report_window(empty)

    def test_an_optimization_report_inside_the_holdout_is_refused(self):
        declared = self.declare()
        report = self.report(declared.window.start, declared.window.end)
        with self.assertRaises(HoldoutViolation):
            assert_optimization_report(report, declared)

    def test_an_optimization_report_clear_of_the_holdout_passes(self):
        declared = self.declare()
        report = self.report(ORIGIN, ORIGIN + 500 * HOUR)
        assert_optimization_report(report, declared)

    def test_a_train_window_report_cannot_be_labeled_out_of_sample(self):
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48)
        split = walk_forward(window(6000), spec)[0]
        report = self.report(split.train.start, split.train.end)
        with self.assertRaises(ProtocolError) as caught:
            assert_test_report(report, split)
        self.assertIn("TRAIN window", str(caught.exception))

    def test_the_test_window_report_is_accepted(self):
        spec = WalkForwardSpec(train_h1=1000, test_h1=500, burn_in_h1=48)
        split = walk_forward(window(6000), spec)[0]
        report = self.report(split.test.start, split.test.end)
        assert_test_report(report, split)

    def test_evaluating_the_declared_holdout_records_a_use(self):
        declared = self.declare()
        report = self.report(declared.window.start, declared.window.end)
        result = evaluate_holdout(self.root, report, note="A1 promotion",
                                  now="2026-08-01T00:00:00Z")
        self.assertTrue(result.is_first_look)
        self.assertIsNone(result.caution)
        self.assertEqual(result.use.metrics_hash, report.metrics_hash)
        self.assertEqual(result.use.note, "A1 promotion")
        self.assertEqual(len(holdout_uses(self.root)), 1)

    def test_a_second_holdout_evaluation_is_counted_and_cautioned(self):
        declared = self.declare()
        report = self.report(declared.window.start, declared.window.end)
        evaluate_holdout(self.root, report)
        second = evaluate_holdout(self.root, report)
        self.assertFalse(second.is_first_look)
        self.assertEqual(second.previous_uses, 1)
        self.assertIn("already been evaluated", second.caution)

    def test_evaluating_a_different_window_fails_and_records_nothing(self):
        """A rejected evaluation must not consume a look."""
        self.declare()
        report = self.report(ORIGIN, ORIGIN + 500 * HOUR)
        with self.assertRaises(HoldoutMismatch):
            evaluate_holdout(self.root, report)
        self.assertEqual(holdout_uses(self.root), ())

    def test_evaluating_on_a_rebuilt_snapshot_fails_and_records_nothing(self):
        declared = self.declare()
        report = self.report(declared.window.start, declared.window.end,
                             snapshot="snap111111111111")
        with self.assertRaises(HoldoutMismatch):
            evaluate_holdout(self.root, report)
        self.assertEqual(holdout_uses(self.root), ())

    def test_a_report_mixing_snapshots_cannot_claim_the_holdout(self):
        declared = self.declare()
        records = [
            win(2.0, boundary=declared.window.start, data_snapshot_id="a"),
            win(2.0, boundary=declared.window.end, data_snapshot_id="b"),
        ] * 20
        report = build_report(
            records, n_resamples=64,
            inputs=ReportInputs(run_id="r", window_start=declared.window.start,
                                window_end=declared.window.end))
        with self.assertRaises(HoldoutMismatch):
            evaluate_holdout(self.root, report)

    def test_the_protocol_reads_lineage_and_never_metric_values(self):
        source = Path("backtest/research/protocol.py").read_text(encoding="utf-8")
        for forbidden in ("global_metrics.values", "expectancy", "win_rate",
                          "profit_factor"):
            self.assertNotIn(forbidden, source, forbidden)


class TestResearchBoundaries(unittest.TestCase):

    def test_research_imports_nothing_from_replay_or_outcome(self):
        from tests.backtest.test_historical_adapter import imported_modules
        allowed_prefixes = ("backtest.metrics", "backtest.data",
                            "backtest.research")
        for path in Path("backtest/research").rglob("*.py"):
            for module in imported_modules(path):
                if not module.startswith("backtest"):
                    continue
                self.assertTrue(
                    any(module.startswith(prefix)
                        for prefix in allowed_prefixes),
                    f"{path.name} imports {module}")

    def test_no_engine_imports_research(self):
        """Methodology is a leaf: no engine may consult the rules it is judged by.

        `cli.py` is exempt and must be: composing the stages is exactly its job,
        and it is the one place a holdout guard can be wired in front of a command
        without an engine learning that holdouts exist.
        """
        from tests.backtest.test_historical_adapter import imported_modules
        entry_points = {"cli.py", "__main__.py"}
        for path in Path("backtest").rglob("*.py"):
            if "research" in path.parts or path.name in entry_points:
                continue
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.research"),
                                 f"{path} imports {module}")

    def test_production_still_imports_nothing_from_backtest(self):
        from tests.backtest.test_historical_adapter import imported_modules
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"), str(path))


if __name__ == "__main__":
    unittest.main()
