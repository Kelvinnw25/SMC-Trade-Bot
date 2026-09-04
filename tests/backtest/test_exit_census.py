"""V-1 … V-12 for the Exit Feasibility Census.

V-4 and V-12 are the highest-priority tests: V-4 pins the arithmetic of the only
output that can settle a rule class, and V-12 pins the D1 bracket that keeps that
output honest about an unobservable.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from backtest.analytics.studies import exit_census as ec
from backtest.analytics.studies import fill_null as fn

_ROOT = Path(__file__).resolve().parents[2]


def _official_run() -> Optional[Path]:
    runs = _ROOT / "data" / "runs" / "official" / "runs"
    if not runs.is_dir():
        return None
    found = [d for d in sorted(runs.iterdir()) if (d / "outcomes.jsonl").exists()]
    return found[0] if len(found) == 1 else None


_OFFICIAL = _official_run()


@dataclass(frozen=True)
class Rec:
    """The record fields the census reads."""

    direction: str = "LONG"
    entry_price: float = 100.0
    sl: float = 98.0
    tp: float = 106.0
    fill_price: Optional[float] = 100.0
    structural_rr: Optional[float] = 3.0
    realized_r: Optional[float] = -1.0
    exit_reason: str = "SL"
    entry_mode: str = "CONTINUATION"
    mfe_r: Optional[float] = 0.0
    mae_r: Optional[float] = -1.0
    holding_candles: Optional[int] = 5
    symbol: str = "AAA/USDT"
    boundary: int = 1_700_000_000
    cycle_index: Optional[int] = None
    universe_rank: Optional[int] = None


class Row:
    """Stands in for `LabelledTrade` — record plus ledger labels."""

    def __init__(self, record: Rec, atr_last: float = 2.0):
        self.record = record
        self.labels = {"atr_last": atr_last}


def stopped(mfe: float, *, holding: int = 5, fill: float = 100.0) -> Row:
    """Exits at its stop, so `realized_r` is exactly −1.0."""
    return Row(Rec(fill_price=fill, realized_r=-1.0, exit_reason="SL",
                   mfe_r=mfe, holding_candles=holding))


def targeted(mfe: float, *, holding: int = 5, fill: float = 100.0) -> Row:
    """Exits at its target, so `realized_r` is exactly `effective_rr`."""
    eff = abs(106.0 - fill) / abs(fill - 98.0)
    return Row(Rec(fill_price=fill, realized_r=eff, exit_reason="TP",
                   mfe_r=mfe, holding_candles=holding))


# ---------------------------------------------------------------------------
# V-1 — threshold monotonicity
# ---------------------------------------------------------------------------

class ThresholdMonotonicity(unittest.TestCase):

    def test_probabilities_are_non_increasing_in_x(self):
        rng = np.random.default_rng(4)
        rows = [stopped(float(v)) for v in rng.gamma(2.0, 0.8, 400)]
        table = ec.threshold_table(ec.series_of("c", rows))
        ps = [r["p_unrestricted"] for r in table]
        self.assertEqual(ps, sorted(ps, reverse=True))
        self.assertEqual([r["threshold_r"] for r in table], list(ec.THRESHOLDS))


# ---------------------------------------------------------------------------
# V-2 — frozen §B3 population
# ---------------------------------------------------------------------------

class PopulationIdentity(unittest.TestCase):

    @unittest.skipUnless(_OFFICIAL is not None, "official run not present")
    def test_cell_counts_match_frozen_b3(self):
        from backtest.analytics.studies.common import labelled_trades

        trades = labelled_trades(_OFFICIAL)
        expected = {"CONTINUATION×SHORT": 6592, "CONTINUATION×LONG": 11913,
                    "RETRACEMENT×SHORT": 4502, "RETRACEMENT×LONG": 6846}
        for cell, mode, direction in ec.CELLS:
            rows = [t for t in trades if t.record.entry_mode == mode
                    and t.record.direction == direction]
            with self.subTest(cell):
                self.assertEqual(ec.series_of(cell, rows).n, expected[cell])


# ---------------------------------------------------------------------------
# V-3 — exit-class partition (tautology removed, closure asserted)
# ---------------------------------------------------------------------------

class ExitClassPartition(unittest.TestCase):

    def test_classes_exhaust_the_engine_vocabulary(self):
        """The real risk is not that a TP row is miscounted — the filter forbids
        it — but that a reason belongs to neither class and vanishes from both."""
        self.assertEqual(ec.SL_CLASS | ec.TP_CLASS, fn.BARRIER_REASONS)
        self.assertEqual(ec.SL_CLASS & ec.TP_CLASS, set())

    def test_censored_reasons_cannot_reach_the_guard(self):
        """`fn.partition` removes censored rows before the vocabulary check runs,
        so the guard can only ever fire on a *barrier* reason the two classes fail
        to cover. Documented so the guard's scope is not mistaken."""
        censored = Row(Rec(exit_reason="HTF_KILL", realized_r=-0.3, mfe_r=1.0))
        self.assertEqual(ec.series_of("c", [censored, stopped(1.5)]).n, 1)

    def test_guard_fires_when_a_class_set_is_incomplete(self):
        original = ec.SL_CLASS
        try:
            ec.SL_CLASS = frozenset({"GAP_SL"})     # drops plain "SL"
            with self.assertRaises(fn.FillNullError):
                ec.series_of("c", [stopped(1.5)])
        finally:
            ec.SL_CLASS = original

    def test_no_targeted_row_counts_as_stopped(self):
        s = ec.series_of("c", [stopped(1.5), targeted(3.0)])
        self.assertFalse(bool((s.is_sl & s.is_tp).any()))
        self.assertTrue(bool((s.is_sl | s.is_tp).all()))


# ---------------------------------------------------------------------------
# V-4 — exact partial arithmetic  (highest priority)
# ---------------------------------------------------------------------------

class PartialArithmetic(unittest.TestCase):

    def test_stopped_row_gains_f_times_x_plus_one(self):
        row = stopped(mfe=2.0)
        led = {(r["x"], r["f"]): r
               for r in ec.partial_ledger(ec.series_of("c", [row]))}
        for (x, f), r in led.items():
            expected = f * (x + 1.0) if 2.0 >= x else 0.0
            with self.subTest(x=x, f=f):
                self.assertAlmostEqual(r["delta_e_upper"], expected, places=12)

    def test_targeted_row_costs_f_times_x_minus_effective_rr(self):
        row = targeted(mfe=3.0)                     # effective_rr = 6/2 = 3.0
        led = {(r["x"], r["f"]): r
               for r in ec.partial_ledger(ec.series_of("c", [row]))}
        for (x, f), r in led.items():
            with self.subTest(x=x, f=f):
                self.assertAlmostEqual(r["delta_e_upper"], f * (x - 3.0),
                                       places=12)
                self.assertLess(r["delta_e_upper"], 0.0)

    def test_row_below_threshold_contributes_nothing(self):
        led = ec.partial_ledger(ec.series_of("c", [stopped(mfe=0.2)]))
        for r in led:
            with self.subTest(x=r["x"], f=r["f"]):
                self.assertEqual(r["delta_e_upper"], 0.0)
                self.assertEqual(r["n_reached"], 0)

    def test_gain_and_cost_terms_sum_to_delta_upper(self):
        rows = [stopped(1.5), stopped(2.5), targeted(3.0), stopped(0.1)]
        for r in ec.partial_ledger(ec.series_of("c", rows)):
            with self.subTest(x=r["x"], f=r["f"]):
                self.assertAlmostEqual(r["gain_term"] + r["cost_term"],
                                       r["delta_e_upper"], places=12)


# ---------------------------------------------------------------------------
# V-12 — the D1 bracket
# ---------------------------------------------------------------------------

class Bracket(unittest.TestCase):

    def test_lower_never_exceeds_upper(self):
        rng = np.random.default_rng(7)
        rows = [stopped(float(v), holding=int(h)) for v, h in
                zip(rng.gamma(2.0, 0.8, 300), rng.integers(1, 40, 300))]
        rows += [targeted(3.0, holding=int(h)) for h in rng.integers(1, 40, 120)]
        for r in ec.partial_ledger(ec.series_of("c", rows)):
            with self.subTest(x=r["x"], f=r["f"]):
                self.assertLessEqual(r["delta_e_lower"], r["delta_e_upper"])

    def test_bracket_collapses_when_nothing_is_ambiguous(self):
        rows = [stopped(1.5, holding=5), targeted(3.0, holding=9)]
        for r in ec.partial_ledger(ec.series_of("c", rows)):
            with self.subTest(x=r["x"], f=r["f"]):
                self.assertEqual(r["delta_e_lower"], r["delta_e_upper"])

    def test_ambiguous_gain_is_withheld_but_cost_is_still_charged(self):
        gainer = stopped(2.0, holding=1)            # ambiguous, positive
        coster = targeted(3.0, holding=1)           # ambiguous, negative
        g = next(r for r in ec.partial_ledger(ec.series_of("c", [gainer]))
                 if r["x"] == 1.0 and r["f"] == 0.5)
        c = next(r for r in ec.partial_ledger(ec.series_of("c", [coster]))
                 if r["x"] == 1.0 and r["f"] == 0.5)
        self.assertGreater(g["delta_e_upper"], 0.0)
        self.assertEqual(g["delta_e_lower"], 0.0)               # withheld
        self.assertLess(c["delta_e_upper"], 0.0)
        self.assertEqual(c["delta_e_lower"], c["delta_e_upper"])  # still charged


# ---------------------------------------------------------------------------
# V-5 — tail immunity of the target-fraction normalisation
# ---------------------------------------------------------------------------

class TailImmunity(unittest.TestCase):

    def test_target_fraction_is_invariant_to_r_unit_collapse(self):
        """Two trades that reached the same fraction of their target, differing
        only in how close the fill sat to the stop."""
        # wide : fill 100.00, r_unit 2.00, effective_rr = 6.00/2.00  = 3.0
        # tight: fill  98.25, r_unit 0.25, effective_rr = 7.75/0.25  = 31.0
        # Both reach one third of their own target, so mfe_r must be 1.0 and
        # 31/3 respectively — a 10x spread in R for the same fractional move.
        wide = ec.series_of("c", [stopped(mfe=1.0, fill=100.0)])
        tight = ec.series_of("c", [stopped(mfe=31.0 / 3.0, fill=98.25)])
        self.assertAlmostEqual(float(wide.effective_rr[0]), 3.0, places=9)
        self.assertAlmostEqual(float(tight.effective_rr[0]), 31.0, places=9)
        self.assertGreater(float(tight.mfe_r[0]) / float(wide.mfe_r[0]), 10.0)
        self.assertAlmostEqual(
            float(wide.mfe_r[0] / wide.effective_rr[0]),
            float(tight.mfe_r[0] / tight.effective_rr[0]), places=9)

    def test_tail_restriction_excludes_the_collapsed_fill(self):
        s = ec.series_of("c", [stopped(mfe=1.0, fill=100.0),
                               stopped(mfe=8.0, fill=98.25)])
        self.assertEqual(list(s.tail_ok), [True, False])
        self.assertEqual(ec.tail_census(s)["n_below_floor"], 1)


# ---------------------------------------------------------------------------
# V-6 / V-7 — robust statistics discipline
# ---------------------------------------------------------------------------

class RobustDiscipline(unittest.TestCase):

    KEYS = {"n", "median", "p25", "p75", "p90", "p95", "trimmed_mean_10pct"}

    def test_no_bare_mean_key_is_ever_emitted(self):
        self.assertEqual(set(ec.robust_summary([1.0, 2.0, 3.0])), self.KEYS)
        flagged = ec.robust_summary([1.0, 2.0, 3.0], include_raw_mean=True)
        self.assertEqual(set(flagged),
                         self.KEYS | {"raw_mean_TAIL_CONTAMINATED"})
        self.assertNotIn("mean", flagged)

    def test_trim_resists_contamination_that_moves_the_raw_mean(self):
        clean = [1.0] * 100
        dirty = clean[:-1] + [10_000.0]
        c = ec.robust_summary(clean, include_raw_mean=True)
        d = ec.robust_summary(dirty, include_raw_mean=True)
        self.assertAlmostEqual(c["trimmed_mean_10pct"], 1.0, places=9)
        self.assertAlmostEqual(d["trimmed_mean_10pct"], 1.0, places=9)
        self.assertAlmostEqual(d["median"], 1.0, places=9)
        self.assertGreater(d["raw_mean_TAIL_CONTAMINATED"], 100.0)

    def test_empty_input_returns_nulls_not_nan(self):
        got = ec.robust_summary([])
        self.assertEqual(got["n"], 0)
        self.assertIsNone(got["median"])

    def test_only_r_units_carry_the_flagged_raw_mean(self):
        e = ec.normalised_excursions(ec.series_of("c", [stopped(1.5)]))
        self.assertIn("raw_mean_TAIL_CONTAMINATED", e["r_units"]["mfe"])
        self.assertNotIn("raw_mean_TAIL_CONTAMINATED",
                         e["target_fraction"]["mfe"])
        self.assertNotIn("raw_mean_TAIL_CONTAMINATED", e["atr_units"]["mfe"])


# ---------------------------------------------------------------------------
# D2 — the geometric reference guard
# ---------------------------------------------------------------------------

class GeometricReference(unittest.TestCase):

    def test_reference_matches_gamblers_ruin(self):
        row = targeted(mfe=3.0)                     # effective_rr = 3.0
        g = ec.give_back(ec.series_of("c", [row]))
        self.assertAlmostEqual(g["geometric_reference"], (3.0 - 1) / (3.0 + 1),
                               places=12)

    def test_rr_le_one_rows_are_excluded_not_negative(self):
        """A target closer than the stop would give a negative probability."""
        # fill 100, sl 98, tp 101.5 -> effective_rr = 1.5/2 = 0.75
        rec = Rec(tp=101.5, fill_price=100.0, realized_r=0.75,
                  exit_reason="TP", mfe_r=1.2, holding_candles=4)
        g = ec.give_back(ec.series_of("c", [Row(rec)]))
        self.assertEqual(g["n_excluded_rr_le_1"], 1)
        self.assertEqual(g["n_degenerate_reached_and_rr_le_1"], 1)
        self.assertIsNone(g["geometric_reference"])
        self.assertIsNone(g["excess"])

    def test_excess_uses_the_same_restricted_set_as_the_reference(self):
        rows = [stopped(1.5), targeted(3.0), stopped(1.2)]
        g = ec.give_back(ec.series_of("c", rows))
        self.assertEqual(g["n_usable_rr_gt_1"], 3)
        self.assertAlmostEqual(g["conditional_restricted"], 2 / 3, places=12)
        self.assertAlmostEqual(
            g["excess"], g["conditional_restricted"] - g["geometric_reference"],
            places=12)


# ---------------------------------------------------------------------------
# D6 — recoverable pool disclosure
# ---------------------------------------------------------------------------

class RecoverablePool(unittest.TestCase):

    def test_pool_is_identical_to_the_gain_set(self):
        rows = [stopped(1.5), targeted(3.0), stopped(0.2), stopped(2.5)]
        pool = ec.recoverable_pool(ec.series_of("c", rows))
        self.assertTrue(all(r["identical_to_gain_set"] for r in pool["rows"]))
        self.assertIn("D6", pool["disclosure"])


# ---------------------------------------------------------------------------
# V-11 — the verdict engine, all four outcomes
# ---------------------------------------------------------------------------

class VerdictEngine(unittest.TestCase):

    @staticmethod
    def _cells(max_lower, share, excess, *, monotone=False):
        ledger = []
        for f in ec.PARTIAL_F:
            for i, x in enumerate(ec.PARTIAL_X):
                value = (max_lower * (i + 1) / len(ec.PARTIAL_X) if monotone
                         else (max_lower if (x, f) == (1.0, 0.5) else
                               max_lower - 0.01))
                ledger.append({"x": x, "f": f, "delta_e_lower": value,
                               "delta_e_upper": value})
        cell = {
            "partial_ledger": ledger,
            "recoverable_pool": {"rows": [
                {"threshold_r": t, "share_recoverable": share,
                 "n_recoverable": 1, "identical_to_gain_set": True}
                for t in ec.THRESHOLDS]},
            "give_back": {"excess": excess},
        }
        return {name: dict(cell) for name, _m, _d in ec.CELLS}

    def test_fee_tier_band_takes_precedence_over_kill(self):
        got = ec.decide(self._cells(0.05, 0.10, 0.00))
        self.assertEqual(got["verdict"], "INCONCLUSIVE_FEE_TIER_BAND")
        self.assertTrue(got["kill"]["K1_delta_le_friction"])

    def test_justify_on_j1_strict(self):
        self.assertEqual(ec.decide(self._cells(0.09, 0.10, 0.0))["verdict"],
                         "JUSTIFY_REWALK")

    def test_exactly_at_friction_does_not_justify(self):
        """D4: J1 is strict, so 0.0683 exactly falls through."""
        got = ec.decide(self._cells(ec.FRICTION_LEG_CONT_SHORT, 0.10, 0.0))
        self.assertFalse(got["justify"]["J1_delta_gt_friction"])
        self.assertTrue(got["kill"]["K1_delta_le_friction"])
        self.assertEqual(got["verdict"], "KILL")

    def test_kill_requires_all_three(self):
        self.assertEqual(ec.decide(self._cells(0.001, 0.10, 0.0))["verdict"],
                         "KILL")
        # pool too large -> K2 fails, and J2 needs excess too, so inconclusive
        self.assertEqual(ec.decide(self._cells(0.001, 0.30, 0.0))["verdict"],
                         "INCONCLUSIVE")

    def test_justify_on_j2_pool_and_excess(self):
        self.assertEqual(ec.decide(self._cells(0.001, 0.30, 0.06))["verdict"],
                         "JUSTIFY_REWALK")

    def test_negative_monotone_surface_does_not_justify(self):
        """D3: monotone increasing but immaterial must not fire J3."""
        got = ec.decide(self._cells(-0.05, 0.10, 0.0, monotone=True))
        self.assertFalse(got["justify"]["J3_monotone_and_material"])
        self.assertEqual(got["verdict"], "KILL")

    def test_monotone_and_material_fires_j3(self):
        got = ec.decide(self._cells(0.019, 0.10, 0.0, monotone=True))
        self.assertFalse(got["fee_tier_band"])       # 0.019 < MATERIAL_R
        got2 = ec.decide(self._cells(0.0683, 0.10, 0.0, monotone=True))
        self.assertTrue(got2["justify"]["J3_monotone_and_material"])
        self.assertEqual(got2["verdict"], "JUSTIFY_REWALK")

    def test_verdict_order_is_declared(self):
        self.assertEqual(ec.BAND, (0.02, ec.FRICTION_LEG_CONT_SHORT))
        self.assertEqual(ec.PRIMARY_CELL, "CONTINUATION×SHORT")


# ---------------------------------------------------------------------------
# V-8 / V-9 — determinism and baseline lock
# ---------------------------------------------------------------------------

class Determinism(unittest.TestCase):

    def test_no_rng_is_used_anywhere(self):
        import ast

        path = _ROOT / "backtest" / "analytics" / "studies" / "exit_census.py"
        source = path.read_text(encoding="utf-8")
        for token in ("default_rng", "random.", "RandomState", "shuffle"):
            self.assertNotIn(token, source)
        ast.parse(source)

    def test_repeated_analysis_is_identical(self):
        rows = [stopped(1.5), targeted(3.0), stopped(0.4, holding=1)]
        self.assertEqual(ec.analyse("c", rows), ec.analyse("c", rows))


class BaselineLock(unittest.TestCase):

    def test_not_registered_and_defines_no_metric(self):
        from backtest.analytics import studies
        from backtest.metrics.definitions import METRICS_BY_NAME
        from backtest.metrics.report import HEADLINE

        self.assertNotIn("exit_census", dict(studies.STUDIES))
        self.assertEqual(len(HEADLINE), 26)
        for name in ("exit_census", "delta_e_lower", "mfe_frac"):
            self.assertNotIn(name, HEADLINE)
            self.assertNotIn(name, METRICS_BY_NAME)

    def test_imports_nothing_forbidden(self):
        import ast

        path = _ROOT / "backtest" / "analytics" / "studies" / "exit_census.py"
        mods: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        self.assertEqual(
            [m for m in mods
             if m.startswith("v2") or m.startswith("src") or "replay" in m], [])


# ---------------------------------------------------------------------------
# V-10 — the conservative-exclusion caveat, data-backed
# ---------------------------------------------------------------------------

class CensoredExclusion(unittest.TestCase):

    @unittest.skipUnless(_OFFICIAL is not None, "official run not present")
    def test_censored_rows_are_absent_and_counted(self):
        """`barrier_resolved` drops HTF_KILL and MAX_HOLDING — the trades that ran
        without reaching a barrier, where a partial would plausibly have helped
        most. The census is therefore a lower bound on exit-management upside."""
        from backtest.analytics.studies.common import labelled_trades

        trades = labelled_trades(_OFFICIAL)
        for cell, mode, direction in ec.CELLS:
            rows = [t for t in trades if t.record.entry_mode == mode
                    and t.record.direction == direction]
            pops = fn.partition([t.record for t in rows])
            s = ec.series_of(cell, rows)
            with self.subTest(cell):
                self.assertEqual(s.n, len(pops.barrier_resolved))
                self.assertEqual(s.n + len(pops.censored),
                                 len(pops.barrier_eligible))
                self.assertGreater(len(pops.censored), 0)


if __name__ == "__main__":
    unittest.main()
