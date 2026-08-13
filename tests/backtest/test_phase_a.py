"""Phase A preparation: partition + sign test, score decomposition, studies.

Three properties carry the weight:

1. **The sign-test thresholds are derived, not typed.** A test that only checked
   "9 of 10 promotes" would pass against a hardcoded 9 and would not notice a
   twelve-slice partition silently weakening the test. So the binomial arithmetic
   is asserted directly, including the case where no threshold exists.
2. **The weight table is verified against recorded scores.** A misread weight
   would corrupt every A1-A3 result while still producing plausible output, so the
   decomposition identity is asserted on rows built to exercise each branch.
3. **"Never fires" and "always fires" are different.** Removing the first is
   bit-neutral; removing the second shifts every score. Both are asserted, because
   conflating them is the error the spec originally made.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.experiments.partition import (
    DEV_END, DEV_START, HOLDOUT_START, TRAIN_END, TRAIN_START,
    PartitionError, Slice, quarter_of, role_of, sign_test, slice_quarters,
)
from backtest.experiments.rescore import (
    ALWAYS_FIRES, NEVER_FIRES, UNINFORMATIVE, Counterfactual, census, decide,
    decompose,
)

H1 = 3600


# ===========================================================================
# partition
# ===========================================================================

class PartitionTests(unittest.TestCase):

    def test_every_boundary_lands_in_exactly_one_role(self):
        self.assertEqual(role_of(TRAIN_START - 1), "OUT_OF_WINDOW")
        self.assertEqual(role_of(TRAIN_START), "TRAIN")
        self.assertEqual(role_of(TRAIN_END), "TRAIN")
        self.assertEqual(role_of(DEV_START), "DEV")
        self.assertEqual(role_of(DEV_END), "DEV")
        self.assertEqual(role_of(HOLDOUT_START), "HOLDOUT")

    def test_train_and_dev_do_not_overlap(self):
        """A trade counted in both roles would improve selection and diagnosis."""
        self.assertLess(TRAIN_END, DEV_START)
        self.assertEqual(DEV_START - TRAIN_END, H1)

    def test_the_holdout_starts_after_the_official_run_ends(self):
        self.assertEqual(DEV_END, 1782687600)
        self.assertGreater(HOLDOUT_START, DEV_END)

    def test_quarter_labels_match_the_metrics_engine_form(self):
        self.assertEqual(quarter_of(TRAIN_START), "2023-Q1")
        self.assertEqual(quarter_of(TRAIN_END), "2025-Q2")

    def test_train_holds_exactly_ten_quarters(self):
        """The ten-slice partition is what makes 9/10 reach alpha=0.05."""
        labels = set()
        moment = TRAIN_START
        while moment <= TRAIN_END:
            labels.add(quarter_of(moment))
            moment += 7 * 24 * H1
        self.assertEqual(len(labels), 10)

    def test_slicing_keeps_only_the_requested_role(self):
        pairs = [(TRAIN_START, 1.0)] * 40 + [(DEV_START, 5.0)] * 40
        train = slice_quarters(pairs, role="TRAIN")
        self.assertEqual([s.label for s in train], ["2023-Q1"])
        self.assertEqual(train[0].n, 40)
        dev = slice_quarters(pairs, role="DEV")
        self.assertEqual([s.label for s in dev], ["2025-Q3"])

    def test_a_slice_below_min_n_is_dropped_not_counted_as_a_failure(self):
        pairs = [(TRAIN_START, 1.0)] * 5
        self.assertEqual(slice_quarters(pairs, role="TRAIN"), [])
        self.assertEqual(len(slice_quarters(pairs, role="TRAIN", min_n=5)), 1)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(PartitionError):
            slice_quarters([], role="TEST")


class SignTestTests(unittest.TestCase):

    def _slices(self, signs):
        return [Slice(label=f"q{i}", role="TRAIN",
                      values=tuple([1.0 if s else -1.0] * 30))
                for i, s in enumerate(signs)]

    def test_ten_slices_promote_at_nine_and_flag_at_eight(self):
        """The thresholds the programme quotes, derived from the binomial."""
        result = sign_test(self._slices([True] * 9 + [False]))
        self.assertEqual(result.promote_at, 9)
        self.assertEqual(result.flag_at, 8)
        self.assertEqual(result.verdict, "PROMOTE")
        self.assertAlmostEqual(result.p_value, 11 / 1024, places=6)

    def test_eight_of_ten_flags_and_seven_rejects(self):
        self.assertEqual(
            sign_test(self._slices([True] * 8 + [False] * 2)).verdict, "FLAG")
        self.assertEqual(
            sign_test(self._slices([True] * 7 + [False] * 3)).verdict, "REJECT")

    def test_four_slices_are_underpowered_and_say_so(self):
        """The old 4/4-year screen could never reach alpha=0.05: p floor 0.0625."""
        result = sign_test(self._slices([True] * 4))
        self.assertIsNone(result.promote_at)
        self.assertEqual(result.verdict, "UNDERPOWERED")
        self.assertAlmostEqual(result.p_value, 1 / 16, places=6)

    def test_twelve_slices_need_ten_not_nine(self):
        """A hardcoded 9 would silently weaken a wider partition."""
        result = sign_test(self._slices([True] * 9 + [False] * 3))
        self.assertEqual(result.promote_at, 10)
        self.assertEqual(result.verdict, "FLAG")

    def test_an_all_negative_family_rejects(self):
        self.assertEqual(sign_test(self._slices([False] * 10)).verdict, "REJECT")

    def test_a_bad_alpha_is_refused(self):
        with self.assertRaises(PartitionError):
            sign_test([], alpha=0.0)


# ===========================================================================
# rescore
# ===========================================================================

def _row(mode="RETRACEMENT", notes=(), score=0, *, entry=100.0, sl=99.0,
         tp=102.0, rr=None, reason=None, displacement=False):
    return {
        "entry_mode": mode, "notes": list(notes), "score": score,
        "entry_price": entry, "sl": sl, "tp": tp, "rr": rr, "reason": reason,
        "displacement": displacement,
    }


class DecompositionTests(unittest.TestCase):

    def test_weights_sum_to_the_recorded_score(self):
        row = _row("RETRACEMENT",
                   ("weekly_aligned", "d1_aligned", "sweep", "fresh_ob"),
                   score=2 + 2 + 2 + 2)
        d = decompose(row)
        self.assertTrue(d.verified)
        self.assertEqual(d.contributions["sweep"], 2)
        self.assertEqual(d.contributions["fresh_ob"], 2)

    def test_the_same_note_weighs_differently_by_mode(self):
        cont = decompose(_row("CONTINUATION", ("sweep",), score=1))
        retr = decompose(_row("RETRACEMENT", ("sweep",), score=2))
        self.assertTrue(cont.verified and retr.verified)
        self.assertEqual(cont.contributions["sweep"], 1)
        self.assertEqual(retr.contributions["sweep"], 2)

    def test_the_rr_band_comes_from_the_levels_not_the_note_text(self):
        """`f"rr_{rr:.1f}"` rounds: 2.98 prints as rr_3.0 but scores +1, not +2."""
        row = _row("RETRACEMENT", ("rr_3.0",), score=1,
                   entry=100.0, sl=99.0, tp=102.98, rr=2.98)
        d = decompose(row)
        self.assertTrue(d.verified, d.contributions)
        self.assertEqual(d.contributions["rr_band"], 1)

    def test_a_true_three_r_target_scores_two(self):
        row = _row("RETRACEMENT", ("rr_3.0",), score=2,
                   entry=100.0, sl=99.0, tp=103.0, rr=3.0)
        self.assertEqual(decompose(row).contributions["rr_band"], 2)

    def test_a_band_note_below_two_scores_nothing(self):
        row = _row("RETRACEMENT", ("rr_1.8",), score=0,
                   entry=100.0, sl=99.0, tp=101.8, rr=1.8)
        d = decompose(row)
        self.assertTrue(d.verified)
        self.assertEqual(d.contributions["rr_band"], 0)

    def test_the_choppy_penalty_is_reversed_before_anything_else(self):
        row = _row("RETRACEMENT", ("sweep", "choppy_penalty"), score=0)
        d = decompose(row)
        self.assertTrue(d.choppy)
        self.assertEqual(d.raw_score, 2)
        self.assertNotIn("choppy_penalty", d.contributions)
        self.assertTrue(d.verified)

    def test_an_unknown_note_fails_verification_rather_than_being_ignored(self):
        d = decompose(_row("RETRACEMENT", ("not_a_real_note",), score=0))
        self.assertFalse(d.verified)
        self.assertEqual(d.unknown_notes, ("not_a_real_note",))

    def test_an_early_exit_row_is_flagged(self):
        d = decompose(_row("RETRACEMENT", (), score=0, reason="bad_rr"))
        self.assertTrue(d.early_exit)

    def test_a_zero_level_skips_the_rr_engine_as_the_scorer_does(self):
        """`if entry and sl and tp` — a 0.0 level means no RR term at all."""
        row = _row("RETRACEMENT", ("sweep",), score=2, tp=0.0, rr=None)
        self.assertTrue(decompose(row).verified)


class DecisionTierTests(unittest.TestCase):

    def test_continuation_thresholds(self):
        for score, tier in ((14, "A+"), (11, "VALID"), (8, "WATCH"), (7, "SKIP")):
            self.assertEqual(decide("CONTINUATION", score, displacement=False),
                             tier)

    def test_retracement_a_plus_needs_displacement(self):
        self.assertEqual(decide("RETRACEMENT", 15, displacement=True), "A+")
        self.assertEqual(decide("RETRACEMENT", 15, displacement=False), "VALID")

    def test_watch_only_has_one_threshold(self):
        self.assertEqual(decide("WATCH_ONLY", 12, displacement=False), "WATCH")
        self.assertEqual(decide("WATCH_ONLY", 11, displacement=False), "SKIP")

    def test_a_shift_lowers_every_threshold_together(self):
        """How a constant term's removal is compensated."""
        self.assertEqual(decide("CONTINUATION", 12, displacement=False), "VALID")
        self.assertEqual(
            decide("CONTINUATION", 12, displacement=False, shift=2), "A+")


class CounterfactualCensusTests(unittest.TestCase):

    def _ledger(self, directory: Path, rows):
        path = directory / "signal_ledger.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def _census(self, rows, counterfactual):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._ledger(directory, rows)
            return census(directory, counterfactual)

    def test_removing_a_note_that_never_fires_changes_nothing(self):
        rows = [_row("RETRACEMENT", ("sweep", "fresh_ob"), score=4)] * 5
        report = self._census(rows, Counterfactual("dead", removed=NEVER_FIRES))
        self.assertTrue(report.score_neutral)
        self.assertTrue(report.tier_neutral)
        self.assertEqual(report.unverified, 0)
        self.assertEqual(set(report.note_fired.values()), {0})

    def test_removing_a_note_that_always_fires_shifts_every_score(self):
        rows = [_row("RETRACEMENT",
                     ("weekly_aligned", "sweep", "fresh_ob"), score=6)] * 5
        report = self._census(rows,
                              Counterfactual("const", removed=("weekly_aligned",)))
        self.assertFalse(report.score_neutral)
        self.assertEqual(report.score_deltas, {-2: 5})

    def test_a_compensating_shift_restores_the_tiers(self):
        rows = [_row("CONTINUATION",
                     ("weekly_aligned", "displacement", "sweep", "fvg"),
                     score=2 + 4 + 1 + 1)] * 5
        report = self._census(rows,
                              Counterfactual("const", removed=("weekly_aligned",)))
        self.assertEqual(report.compensating_shift, 2)
        self.assertEqual(report.changes_by_shift[2], 0)

    def test_reweighting_flips_a_penalty_into_a_bonus(self):
        rows = [_row("RETRACEMENT",
                     ("sweep", "momentum_weakening"), score=1)] * 4
        report = self._census(rows, Counterfactual(
            "sign", reweighted={"momentum_weakening": 1}))
        self.assertEqual(report.score_deltas, {2: 4})
        self.assertEqual(report.note_fired["momentum_weakening"], 4)

    def test_an_early_exit_row_is_counted_but_not_rescored(self):
        rows = [_row("RETRACEMENT", ("choppy_penalty",), score=-2,
                     reason="bad_rr")]
        report = self._census(rows, Counterfactual("x", removed=("sweep",)))
        self.assertEqual(report.early_exit, 1)
        self.assertEqual(report.score_deltas, {})

    def test_an_unscored_row_is_skipped_entirely(self):
        rows = [_row("RETRACEMENT", (), score=0)]
        report = self._census(rows, Counterfactual("x", removed=("sweep",)))
        self.assertEqual(report.scored, 0)

    def test_a_bad_weight_table_would_be_caught(self):
        """The verification identity is the table's only guarantee."""
        rows = [_row("RETRACEMENT", ("sweep",), score=99)]
        report = self._census(rows, Counterfactual("x"))
        self.assertEqual(report.unverified, 1)

    def test_the_three_component_classes_are_disjoint(self):
        self.assertEqual(set(NEVER_FIRES) & set(ALWAYS_FIRES), set())
        self.assertEqual(set(NEVER_FIRES) & set(UNINFORMATIVE), set())
        self.assertEqual(set(ALWAYS_FIRES) & set(UNINFORMATIVE), set())

    def test_a_missing_ledger_is_an_error(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                census(Path(tmp), Counterfactual("x"))


# ===========================================================================
# boundary
# ===========================================================================

class BoundaryTests(unittest.TestCase):

    def _imports(self, filename):
        import ast

        path = (Path(__file__).resolve().parents[2] / "backtest"
                / "experiments" / filename)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
        return found

    def test_partition_and_rescore_import_no_strategy_code(self):
        """A1-A3 are arithmetic over the ledger, so `v2` is not needed at all."""
        for filename in ("partition.py", "rescore.py", "phase_a.py"):
            for module in self._imports(filename):
                self.assertFalse(
                    module == "v2" or module.startswith("v2."),
                    f"{filename} imports {module}")

    def test_nothing_imports_the_replay_package(self):
        for filename in ("partition.py", "rescore.py", "phase_a.py"):
            for module in self._imports(filename):
                self.assertFalse(module.startswith("backtest.replay"),
                                 f"{filename} imports {module}")


class StudyDeterminismTests(unittest.TestCase):
    """Output must not depend on the order records arrive in."""

    def _rows(self):
        from backtest.experiments.phase_a import _Row

        rows = []
        for index in range(400):
            boundary = TRAIN_START + index * 6 * 24 * H1
            rows.append(_Row(
                boundary=boundary,
                exit_key=(boundary + H1, index, 1, f"S{index % 7}USDT"),
                realized_r=(2.0 if index % 3 == 0 else -1.0),
                direction=("SHORT" if index % 2 else "LONG"),
                entry_mode=("CONTINUATION" if index % 4 else "RETRACEMENT"),
                score=8 + (index % 9),
                labels={"fvg": index % 2 == 0, "tp_rr": 1.5 + (index % 5) * 0.5,
                        "volume_spike": index % 5 == 0,
                        "ob_candidate_count": 1 + index % 8},
            ))
        return rows

    def test_a_gate_is_order_independent(self):
        from backtest.experiments.phase_a import _gate

        rows = self._rows()
        first = _gate(rows, lambda r: r.labels["fvg"], name="g", question="q")
        second = _gate(list(reversed(rows)), lambda r: r.labels["fvg"],
                       name="g", question="q")
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_the_volume_matched_control_removes_the_same_count(self):
        from backtest.experiments.phase_a import _gate

        rows = self._rows()
        result = _gate(rows, lambda r: r.labels["fvg"], name="g", question="q")
        self.assertEqual(result["removed"]["n"],
                         result["volume_matched_control"]["removed_n"])
        self.assertEqual(result["kept"]["n"],
                         result["volume_matched_control"]["n"])

    def test_a_gate_reports_three_sign_tests_and_names_the_deciding_one(self):
        from backtest.experiments.phase_a import _gate

        result = _gate(self._rows(), lambda r: r.labels["fvg"], name="g",
                       question="q")
        for key in ("train_sign_test_absolute", "train_sign_test_vs_control",
                    "train_sign_test_vs_baseline"):
            self.assertIn(key, result)
        self.assertIn("vs_control", result["which_sign_test_decides"])

    def test_writing_studies_is_byte_deterministic(self):
        from backtest.experiments.phase_a import Study, write_phase_a

        studies = {"x": Study("x", "X", "r1-core-cleanup", {"b": 2, "a": 1})}
        with TemporaryDirectory() as tmp:
            first = write_phase_a(studies, Path(tmp) / "a")[0].read_bytes()
            second = write_phase_a(studies, Path(tmp) / "b")[0].read_bytes()
            self.assertEqual(first, second)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()