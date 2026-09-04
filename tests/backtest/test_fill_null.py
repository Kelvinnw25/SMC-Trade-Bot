"""D0 — the authoritative gate for the fill-based null. BENCHMARK_FIX_SPEC.md §D0.

D0 is authoritative; D1 and D2 are consistency checks. Everything here runs on
synthetic fixtures or analytic truth, except D0-S6, which is a population claim
about the official run and can only be checked against it.

The fixture values are chosen to be exactly representable in binary floating
point, so several assertions are exact rather than approximate. That is
deliberate: a tolerance would hide the drift these tests exist to catch.
"""
from __future__ import annotations

import random
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backtest.analytics.studies import fill_null as fn

_ROOT = Path(__file__).resolve().parents[2]


def _official_run() -> Optional[Path]:
    """The official replay directory, or None when the corpus is absent.

    `data/runs/official` is the bundle; the run itself is the single
    `runs/replay-*` beneath it, and its digest is not hardcoded here so the
    test keeps working across re-runs.
    """
    runs = _ROOT / "data" / "runs" / "official" / "runs"
    if not runs.is_dir():
        return None
    candidates = [d for d in sorted(runs.iterdir())
                  if (d / "outcomes.jsonl").exists()]
    return candidates[0] if len(candidates) == 1 else None


_OFFICIAL = _official_run()


@dataclass(frozen=True)
class Row:
    """The fields the study reads off an `OutcomeRecord`."""

    direction: str
    entry_price: float
    sl: float
    tp: float
    fill_price: Optional[float]
    structural_rr: Optional[float] = None
    realized_r: Optional[float] = None
    exit_reason: str = "TP"
    entry_mode: str = "CONTINUATION"


# ---------------------------------------------------------------------------
# D0-S1 — the ordering predicate, not magnitude
# ---------------------------------------------------------------------------

class OrderingPredicate(unittest.TestCase):
    """`geometry()` decides on ordering. `abs()` cannot: a SHORT gapped through
    its stop divides 17 by 1 and returns a perfectly plausible 17.0."""

    #: (label, direction, entry, sl, tp, fill, value a naive abs() would yield)
    CASES = (
        ("GAP-SL-S", "SHORT", 100.0, 104.0, 88.0, 105.0, 17.0),
        ("GAP-TP-S", "SHORT", 100.0, 104.0, 88.0, 87.0, 1.0 / 17.0),
        ("GAP-SL-L", "LONG", 100.0, 98.0, 106.0, 97.0, 9.0),
        ("GAP-TP-L", "LONG", 100.0, 98.0, 106.0, 107.0, 1.0 / 9.0),
        ("AT-SL-S", "SHORT", 100.0, 104.0, 88.0, 104.0, None),
        ("AT-TP-S", "SHORT", 100.0, 104.0, 88.0, 88.0, 0.0),
    )

    def test_non_interior_fills_are_ineligible(self):
        for label, direction, entry, sl, tp, fill, _ in self.CASES:
            with self.subTest(label):
                self.assertIsNone(
                    fn.geometry(direction, fill, sl, tp, entry),
                    f"{label}: a fill at/beyond a barrier must be ineligible")

    def test_naive_abs_would_have_returned_a_plausible_number(self):
        """Documents why the predicate is ordering-based, and fails loudly if
        someone reintroduces the magnitude form."""
        for label, _d, _e, sl, tp, fill, naive in self.CASES:
            if naive in (None, 0.0):
                continue
            with self.subTest(label):
                self.assertAlmostEqual(
                    abs(tp - fill) / abs(fill - sl), naive, places=9)

    def test_interior_fills_are_eligible(self):
        self.assertIsNotNone(fn.geometry("LONG", 101.0, 98.0, 106.0, 100.0))
        self.assertIsNotNone(fn.geometry("SHORT", 97.0, 104.0, 88.0, 100.0))

    def test_missing_or_non_finite_input_is_ineligible(self):
        self.assertIsNone(fn.geometry("LONG", None, 98.0, 106.0, 100.0))
        self.assertIsNone(fn.geometry("LONG", float("nan"), 98.0, 106.0, 100.0))
        self.assertIsNone(fn.geometry("LONG", float("inf"), 98.0, 106.0, 100.0))
        self.assertIsNone(fn.geometry("BOTH", 101.0, 98.0, 106.0, 100.0))


# ---------------------------------------------------------------------------
# D0-S2 — asymmetric LONG/SHORT fixture
# ---------------------------------------------------------------------------

LONG_A = Row("LONG", 100.0, 98.0, 106.0, 101.0, structural_rr=3.0)
SHORT_A = Row("SHORT", 100.0, 104.0, 88.0, 97.0, structural_rr=3.0)


class DirectionAsymmetry(unittest.TestCase):
    """Same `structural_rr`, both filled adversely, different `fill_null`.

    A SHORT branch copy-pasted from LONG passes a mirrored fixture. It does not
    pass this one.
    """

    def test_long_a(self):
        self.assertEqual(LONG_A.structural_rr, 3.0)
        self.assertAlmostEqual(fn.effective_rr(LONG_A), 5.0 / 3.0, places=12)
        self.assertAlmostEqual(fn.fill_null(LONG_A), 3.0 / 8.0, places=12)
        self.assertAlmostEqual(fn.adverse_slippage_r(LONG_A), 0.50, places=12)

    def test_short_a(self):
        self.assertEqual(SHORT_A.structural_rr, 3.0)
        self.assertAlmostEqual(fn.effective_rr(SHORT_A), 9.0 / 7.0, places=12)
        self.assertAlmostEqual(fn.fill_null(SHORT_A), 7.0 / 16.0, places=12)
        self.assertAlmostEqual(fn.adverse_slippage_r(SHORT_A), 0.75, places=12)

    def test_the_two_directions_do_not_agree(self):
        self.assertNotEqual(fn.fill_null(LONG_A), fn.fill_null(SHORT_A))
        self.assertNotEqual(fn.effective_rr(LONG_A), fn.effective_rr(SHORT_A))

    def test_both_fills_are_adverse(self):
        self.assertGreater(fn.adverse_slippage_r(LONG_A), 0)
        self.assertGreater(fn.adverse_slippage_r(SHORT_A), 0)

    def test_a_favourable_fill_is_signed_negative(self):
        improved_long = Row("LONG", 100.0, 98.0, 106.0, 99.0)
        improved_short = Row("SHORT", 100.0, 104.0, 88.0, 101.0)
        self.assertAlmostEqual(fn.adverse_slippage_r(improved_long), -0.5, 12)
        self.assertAlmostEqual(fn.adverse_slippage_r(improved_short), -0.25, 12)


# ---------------------------------------------------------------------------
# D0-S3 — LIMIT_TOUCH invariance, SHORT, exact
# ---------------------------------------------------------------------------

class LimitTouchInvariance(unittest.TestCase):
    """Where the fill is the planned entry, the correction must be inert."""

    def test_short_touch_is_exactly_invariant(self):
        row = Row("SHORT", 100.0, 104.0, 88.0, 100.0, structural_rr=3.0)
        self.assertEqual(fn.effective_rr(row), 3.0)
        self.assertEqual(fn.effective_rr(row), row.structural_rr)
        self.assertEqual(fn.fill_null(row), 0.25)
        self.assertEqual(fn.fill_null(row), fn.null_bound(row))
        self.assertEqual(fn.adverse_slippage_r(row), 0.0)

    def test_long_touch_is_exactly_invariant(self):
        row = Row("LONG", 100.0, 98.0, 106.0, 100.0, structural_rr=3.0)
        self.assertEqual(fn.effective_rr(row), 3.0)
        self.assertEqual(fn.fill_null(row), 0.25)
        self.assertEqual(fn.fill_null(row), fn.null_bound(row))


# ---------------------------------------------------------------------------
# D0-S4 — typed raise
# ---------------------------------------------------------------------------

class TypedRaise(unittest.TestCase):
    """Never nan, never inf, never a bare ZeroDivisionError: an out-of-range
    null that reached a mean would be invisible."""

    def test_fill_null_raises_on_every_ineligible_case(self):
        for label, direction, entry, sl, tp, fill, _ in OrderingPredicate.CASES:
            row = Row(direction, entry, sl, tp, fill)
            with self.subTest(label):
                with self.assertRaises(fn.FillNullError):
                    fn.fill_null(row)
                with self.assertRaises(fn.FillNullError):
                    fn.effective_rr(row)
                with self.assertRaises(fn.FillNullError):
                    fn.adverse_slippage_r(row)

    def test_at_stop_raises_rather_than_dividing_by_zero(self):
        row = Row("SHORT", 100.0, 104.0, 88.0, 104.0)
        with self.assertRaises(fn.FillNullError):
            fn.fill_null(row)

    def test_null_bound_refuses_a_non_positive_structural_rr(self):
        for bad in (None, 0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(bad):
                with self.assertRaises(fn.FillNullError):
                    fn.null_bound(Row("LONG", 100.0, 98.0, 106.0, 101.0, bad))


# ---------------------------------------------------------------------------
# D0-S5 — bound-path routing
# ---------------------------------------------------------------------------

class BoundRouting(unittest.TestCase):
    """§B5 reinstates with `null_bound`; §B6 reinstates with `fill_null`.

    The fixture is deliberately `barrier_eligible` so both quantities are
    computable and demonstrably differ. On a real `no_race` row `fill_null`
    raises, so the substitution would be caught there by D0-S4 — but not in the
    bound path, which is what this covers.
    """

    def test_the_two_nulls_differ_on_the_same_row(self):
        self.assertAlmostEqual(fn.null_bound(LONG_A), 0.25, places=12)
        self.assertAlmostEqual(fn.fill_null(LONG_A), 0.375, places=12)
        self.assertNotEqual(fn.null_bound(LONG_A), fn.fill_null(LONG_A))

    def test_no_race_bound_uses_null_bound(self):
        # One resolved winner priced at fill_null 0.375, one no_race row whose
        # structural_rr is 3.0 -> null_bound 0.25.
        resolved = Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, realized_r=1.0)
        gapped = Row("LONG", 100.0, 98.0, 106.0, 97.0, 3.0, realized_r=0.0,
                     exit_reason="GAP_SL")
        result = fn.cell_result("c", [resolved, gapped])

        self.assertEqual(result.n_barrier_resolved, 1)
        self.assertEqual(result.no_race_bounds.reinstated_n, 1)
        # lower: {win@0.375, loss@0.25} -> 1/2 - 0.625/2
        self.assertAlmostEqual(result.no_race_bounds.lower, 0.1875, places=12)
        # upper: both counted as wins -> 1 - 0.625/2
        self.assertAlmostEqual(result.no_race_bounds.upper, 0.6875, places=12)
        # Had fill_null been used on the reinstated row it would have raised,
        # so assert the arithmetic pins null_bound specifically.
        self.assertNotAlmostEqual(result.no_race_bounds.lower,
                                  0.5 - 0.375, places=6)

    def test_censoring_bound_uses_fill_null(self):
        resolved = Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, realized_r=1.0)
        killed = Row("SHORT", 100.0, 104.0, 88.0, 97.0, 3.0, realized_r=-0.4,
                     exit_reason="HTF_KILL")
        result = fn.cell_result("c", [resolved, killed])

        self.assertEqual(result.censoring_bounds.reinstated_n, 1)
        # The reinstated row is priced at its own fill_null of 7/16, not at
        # null_bound 0.25.
        expected_lower = 0.5 - (0.375 + 7.0 / 16.0) / 2.0
        self.assertAlmostEqual(result.censoring_bounds.lower, expected_lower, 12)
        self.assertAlmostEqual(result.censoring_bounds.upper,
                               1.0 - (0.375 + 7.0 / 16.0) / 2.0, places=12)

    def test_bounds_are_independent_and_never_combined(self):
        """§C5: each bound is computed against the primary on its own."""
        resolved = Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, realized_r=1.0)
        gapped = Row("LONG", 100.0, 98.0, 106.0, 97.0, 3.0, realized_r=0.0,
                     exit_reason="GAP_SL")
        killed = Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, realized_r=-0.4,
                     exit_reason="MAX_HOLDING")
        both = fn.cell_result("c", [resolved, gapped, killed])
        only_gap = fn.cell_result("c", [resolved, gapped])
        only_kill = fn.cell_result("c", [resolved, killed])

        self.assertEqual(both.no_race_bounds, only_gap.no_race_bounds)
        self.assertEqual(both.censoring_bounds, only_kill.censoring_bounds)


# ---------------------------------------------------------------------------
# D0-S6 — populations, identities, vocabulary closure
# ---------------------------------------------------------------------------

class PopulationPartition(unittest.TestCase):

    def _book(self):
        return [
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, 1.0, "TP"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -1.0, "SL"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, 2.4, "GAP_TP"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -1.2, "GAP_SL"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -1.0,
                "SL_WORST_CASE_M5"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -1.0,
                "SL_WORST_CASE_NO_M5"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -0.3, "HTF_KILL"),
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, 0.2, "MAX_HOLDING"),
            Row("LONG", 100.0, 98.0, 106.0, 97.0, 3.0, 0.0, "GAP_SL"),
        ]

    def test_identities_hold(self):
        pops = fn.partition(self._book())
        pops.check()
        self.assertEqual(len(pops.admitted), 9)
        self.assertEqual(len(pops.no_race), 1)
        self.assertEqual(len(pops.barrier_eligible), 8)
        self.assertEqual(len(pops.barrier_resolved), 6)
        self.assertEqual(len(pops.censored), 2)

    def test_worst_case_stops_are_barrier_resolutions(self):
        """Omitting them drops genuine losses and biases Δ upward — the error
        this suite exists to prevent from recurring."""
        self.assertIn("SL_WORST_CASE_M5", fn.BARRIER_REASONS)
        self.assertIn("SL_WORST_CASE_NO_M5", fn.BARRIER_REASONS)

    def test_barrier_reasons_match_the_engine_vocabulary(self):
        from backtest.outcome.trade import EXIT_REASONS

        classified = fn.BARRIER_REASONS | fn.CENSORED_REASONS
        self.assertTrue(classified <= EXIT_REASONS)
        self.assertEqual(EXIT_REASONS - classified, {"END_OF_DATA"})

    def test_end_of_data_cannot_reach_the_admitted_book(self):
        from backtest.outcome.trade import TERMINAL_STATES

        self.assertNotIn("PENDING", TERMINAL_STATES)
        self.assertNotIn("ACTIVE", TERMINAL_STATES)

    def test_unknown_exit_reason_raises_rather_than_being_dropped(self):
        book = self._book() + [
            Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, 0.0, "NEW_REASON")]
        with self.assertRaises(fn.FillNullError):
            fn.partition(book).check()

    @unittest.skipUnless(_OFFICIAL is not None, "official run not present")
    def test_official_run_censored_rows_are_all_eligible(self):
        """§B6 states this as fact; here it is verified."""
        from backtest.analytics.studies.common import labelled_trades

        trades = [t.record for t in labelled_trades(_OFFICIAL)]
        pops = fn.partition(trades)
        pops.check()
        censored = [t for t in trades
                    if t.exit_reason in fn.CENSORED_REASONS]
        ineligible = [t for t in censored if fn._geometry_of(t) is None]
        self.assertEqual(
            ineligible, [],
            f"{len(ineligible)} censored rows are not barrier_eligible; §B6's "
            f"population is then wrong and its bounds are invalid")
        self.assertEqual(len(pops.censored), len(censored))


# ---------------------------------------------------------------------------
# D0-S7 — output-type exclusivity
# ---------------------------------------------------------------------------

class OutputTypeExclusivity(unittest.TestCase):

    BOOK = [
        Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, 1.0, "TP"),
        Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -1.0, "SL"),
        Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, -0.3, "HTF_KILL"),
        Row("LONG", 100.0, 98.0, 106.0, 97.0, 3.0, 0.0, "GAP_SL"),
    ]

    def test_every_diagnostic_column_carries_the_bound_infix(self):
        row = fn.cell_result("c", self.BOOK).to_row()
        primaries = {"cell", "n_barrier_resolved", "delta_primary",
                     "median_effective_rr", "trimmed_mean_effective_rr"}
        for name in row:
            if name in primaries:
                self.assertNotIn("_bound_", name)
            else:
                self.assertIn("_bound_", name,
                              f"{name} reinstates excluded rows but is not "
                              f"named as a bound")

    def test_as_traded_carries_no_null_column(self):
        row = fn.as_traded_row("c", self.BOOK)
        forbidden = ("delta", "null", "rr", "z_", "p_value", "_bound_")
        for name in row:
            for token in forbidden:
                self.assertNotIn(token, name, f"{name} is not R-space")

    def test_the_two_channels_use_different_populations(self):
        """§B7 keeps censored exits; §B4 does not. Both drop `no_race`."""
        d3 = fn.cell_result("c", self.BOOK)
        d3b = fn.as_traded_row("c", self.BOOK)
        self.assertEqual(d3.n_barrier_resolved, 2)
        self.assertEqual(d3b["n_as_traded"], 3)

    def test_no_race_is_removed_from_both_channels(self):
        without = [r for r in self.BOOK if r.fill_price != 97.0]
        self.assertEqual(fn.cell_result("c", without).delta_primary,
                         fn.cell_result("c", self.BOOK).delta_primary)
        self.assertEqual(fn.as_traded_row("c", without)["n_as_traded"],
                         fn.as_traded_row("c", self.BOOK)["n_as_traded"])

    def test_empty_bound_population_emits_null_not_the_primary(self):
        """§D3: RETRACEMENT has no_race n=0; the column must not read as
        computed."""
        clean = [r for r in self.BOOK if r.fill_price != 97.0]
        result = fn.cell_result("c", clean)
        self.assertIsNone(result.no_race_bounds.lower)
        self.assertIsNone(result.no_race_bounds.upper)
        self.assertEqual(result.no_race_bounds.reinstated_n, 0)
        self.assertIsNotNone(result.delta_primary)
        self.assertEqual(result.to_row()["delta_bound_lower_no_race"], None)


# ---------------------------------------------------------------------------
# D0-S8 — the descoped shadow surface
# ---------------------------------------------------------------------------

class ShadowDescoped(unittest.TestCase):

    def test_shadow_entrypoint_raises(self):
        with self.assertRaises(NotImplementedError):
            fn.shadow_cells([])

    def test_no_other_shadow_surface_exists(self):
        exported = [name for name in fn.__all__ if "shadow" in name.lower()]
        self.assertEqual(exported, [])

    def test_the_loader_really_does_filter_shadows_out(self):
        """The reason the descope is safe, pinned so it cannot rot."""
        import inspect

        from backtest.analytics.studies import common
        source = inspect.getsource(common.labelled_trades)
        self.assertIn("scored", source)


# ---------------------------------------------------------------------------
# D0-S9 — analytic truth and determinism
# ---------------------------------------------------------------------------

class AnalyticTruth(unittest.TestCase):
    """Gambler's ruin: a driftless walk absorbed at -a and +b hits +b first
    with probability a/(a+b). With a=1 stop unit and b=RR, that is exactly
    1/(1+RR) — the null. So a book of driftless races must produce Δ ≈ 0.

    Integer steps are used rather than a truncated price walk: a finite horizon
    biases first passage by -0.13 to -0.33 depending on barrier width, which
    would make this test measure the truncation, not the estimator.
    """

    @staticmethod
    def _race(rng: random.Random, down: int, up: int) -> bool:
        position = 0
        while True:
            position += 1 if rng.random() < 0.5 else -1
            if position >= up:
                return True
            if position <= -down:
                return False

    def test_driftless_book_recovers_zero_delta(self):
        rng = random.Random(20260815)
        # (stop units, target units) -> effective_rr 1, 2 and 3.
        shapes = ((2, 2), (2, 4), (2, 6))
        book = []
        for down, up in shapes:
            rr = up / down
            # fill at 100, sl one stop below, tp `rr` stops above.
            sl, tp = 100.0 - 1.0, 100.0 + rr
            for _ in range(70_000):
                won = self._race(rng, down, up)
                book.append(Row("LONG", 100.0, sl, tp, 100.0,
                                structural_rr=rr,
                                realized_r=(rr if won else -1.0),
                                exit_reason=("TP" if won else "SL")))

        result = fn.cell_result("driftless", book)
        self.assertEqual(result.n_barrier_resolved, len(book))
        self.assertIsNotNone(result.delta_primary)
        self.assertLess(
            abs(result.delta_primary), 0.005,
            f"driftless book should price to zero, got "
            f"{result.delta_primary:+.5f}")

    def test_effective_rr_is_reported_robustly_not_as_a_bare_mean(self):
        """§B4: as a fill nears SL the denominator collapses. One such row
        moves the arithmetic mean by orders of magnitude and the median not at
        all."""
        book = [Row("LONG", 100.0, 98.0, 106.0, 101.0, 3.0, 1.0, "TP")
                for _ in range(99)]
        book.append(Row("LONG", 100.0, 98.0, 106.0, 98.000001, 3.0, -1.0, "SL"))
        result = fn.cell_result("tail", book)
        rrs = [fn.effective_rr(r) for r in book]

        self.assertGreater(sum(rrs) / len(rrs), 1000.0)
        self.assertAlmostEqual(result.median_effective_rr, 5.0 / 3.0, places=9)
        self.assertLess(result.trimmed_mean_effective_rr, 2.0)

    def test_output_is_deterministic(self):
        book = OutputTypeExclusivity.BOOK
        self.assertEqual(fn.cell_result("c", book).to_row(),
                         fn.cell_result("c", book).to_row())
        self.assertEqual(fn.as_traded_row("c", book),
                         fn.as_traded_row("c", book))


# ---------------------------------------------------------------------------
# baseline-lock guarantees
# ---------------------------------------------------------------------------

class BaselineLock(unittest.TestCase):
    """The study must be incapable of moving the frozen baseline."""

    def test_study_is_not_registered(self):
        from backtest.analytics import studies

        self.assertNotIn("fill_null_analysis", dict(studies.STUDIES))
        self.assertNotIn(fn.run, [runner for _n, runner in studies.STUDIES])

    def test_study_defines_no_metric(self):
        from backtest.metrics.definitions import METRICS_BY_NAME
        from backtest.metrics.report import HEADLINE

        for name in ("effective_rr", "fill_null", "delta_primary",
                     "fill_null_analysis"):
            self.assertNotIn(name, HEADLINE)
            self.assertNotIn(name, METRICS_BY_NAME)

    def test_headline_membership_is_what_the_spec_records(self):
        from backtest.metrics.report import HEADLINE

        self.assertEqual(len(HEADLINE), 26)
        self.assertIn("structural_vs_realized_gap", HEADLINE)
        self.assertNotIn("avg_structural_rr", HEADLINE)


if __name__ == "__main__":
    unittest.main()
