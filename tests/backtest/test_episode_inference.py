"""D-1 … D-13 for EPISODE_INFERENCE_CONT_SHORT.

D-2 is the highest-priority test here: if block intervals do not widen on a
series with injected block structure, the whole experiment is inert and every
other test passes anyway.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from backtest.analytics.studies import episode_inference as ei
from backtest.analytics.studies import fill_null as fn

_ROOT = Path(__file__).resolve().parents[2]
HOUR = 3600


def _official_run() -> Optional[Path]:
    runs = _ROOT / "data" / "runs" / "official" / "runs"
    if not runs.is_dir():
        return None
    found = [d for d in sorted(runs.iterdir()) if (d / "outcomes.jsonl").exists()]
    return found[0] if len(found) == 1 else None


_OFFICIAL = _official_run()


@dataclass(frozen=True)
class Row:
    """The fields both modules read off an `OutcomeRecord`."""

    boundary: int
    direction: str = "LONG"
    entry_price: float = 100.0
    sl: float = 98.0
    tp: float = 106.0
    fill_price: Optional[float] = 101.0
    structural_rr: Optional[float] = 3.0
    realized_r: Optional[float] = 1.0
    exit_reason: str = "TP"
    entry_mode: str = "CONTINUATION"
    symbol: str = "AAA/USDT"
    cycle_index: Optional[int] = None
    universe_rank: Optional[int] = None


def book(wins: list[bool], *, start: int = 1_700_000_000,
         step: int = HOUR) -> list[Row]:
    """A book with `fill_null` = 0.375 on every row, so d = +0.625 / -0.375."""
    return [Row(boundary=start + i * step,
                realized_r=(1.0 if won else -1.0),
                exit_reason=("TP" if won else "SL"))
            for i, won in enumerate(wins)]


def evenly_spaced(n: int, rng: np.random.Generator, p: float = 0.375,
                  step: int = HOUR):
    return book([bool(x) for x in (rng.random(n) < p)], step=step)


# ---------------------------------------------------------------------------
# D-1 — degenerate blocking agrees with IID resampling
# ---------------------------------------------------------------------------

class DegenerateBlocking(unittest.TestCase):

    def test_one_trade_blocks_match_iid_bootstrap(self):
        rng = np.random.default_rng(11)
        s = ei.series_of("c", evenly_spaced(500, rng))
        block = HOUR                                  # exactly one trade per block
        got = ei.circular_block_ci(s.boundary, s.d, block, n_resamples=3000)

        boot = np.random.default_rng(11)
        draws = [s.d[boot.integers(0, s.n, s.n)].mean() for _ in range(3000)]
        low, high = np.percentile(draws, (2.5, 97.5))

        self.assertAlmostEqual(got["ci_low"], float(low), delta=0.02)
        self.assertAlmostEqual(got["ci_high"], float(high), delta=0.02)
        self.assertEqual(got["k_blocks"], 499)      # span = (n-1)*step + 1


# ---------------------------------------------------------------------------
# D-2 — block structure must widen the interval  (highest priority)
# ---------------------------------------------------------------------------

class BlockStructureWidens(unittest.TestCase):

    WINS = 0.375
    RUN = 50

    def _clustered(self, n=1200, seed=5):
        """Same marginal win rate, but arriving in runs of alternating regime.

        Regime assignment is *random* rather than a fixed alternation: a
        deterministic cycle aliases against any block length equal to its period,
        collapsing between-block variance to zero and making the test pass or fail
        on arithmetic coincidence rather than on behaviour.
        """
        rng = np.random.default_rng(seed)
        wins: list[bool] = []
        while len(wins) < n:
            p = 0.70 if rng.random() < 0.5 else 0.05
            wins += [bool(x) for x in (rng.random(self.RUN) < p)]
        return book(wins[:n])

    def _iid(self, n=1200):
        return evenly_spaced(n, np.random.default_rng(5), self.WINS)

    def test_clustered_series_gives_wider_interval_than_iid(self):
        block = self.RUN * HOUR
        clustered = ei.series_of("c", self._clustered())
        iid = ei.series_of("c", self._iid())
        wide = ei.circular_block_ci(clustered.boundary, clustered.d, block,
                                    n_resamples=4000)
        tight = ei.circular_block_ci(iid.boundary, iid.d, block,
                                     n_resamples=4000)
        self.assertGreater(wide["ci_high"] - wide["ci_low"],
                           2.0 * (tight["ci_high"] - tight["ci_low"]),
                           "block resampling must react to block structure; if "
                           "it does not, the entire experiment is inert")

    def test_sub_dependence_blocks_understate_the_variance(self):
        """Blocks shorter than the dependence length treat near-neighbours as
        exchangeable and miss the regime-level variance the CI must carry."""
        s = ei.series_of("c", self._clustered())
        short = ei.circular_block_ci(s.boundary, s.d, 2 * HOUR, n_resamples=3000)
        matched = ei.circular_block_ci(s.boundary, s.d, self.RUN * HOUR,
                                       n_resamples=3000)
        self.assertLess(short["ci_high"] - short["ci_low"],
                        matched["ci_high"] - matched["ci_low"])


# ---------------------------------------------------------------------------
# D-3 — exact sign test
# ---------------------------------------------------------------------------

class SignTest(unittest.TestCase):

    @staticmethod
    def _panel(deltas):
        return [{"month": f"m{i}", "delta": d, "qualifying": True, "n": 50}
                for i, d in enumerate(deltas)]

    def test_matches_closed_form(self):
        # 8 of 10 positive: P(X>=8) = (45 + 10 + 1)/1024 = 0.0547 — just misses.
        got = ei.sign_test(self._panel([1.0] * 8 + [-1.0] * 2))
        self.assertEqual(got["positive"], 8)
        self.assertAlmostEqual(got["p_value"], 56 / 1024, places=12)
        self.assertFalse(got["rejects"])
        # 9 of 10: P(X>=9) = 11/1024 = 0.0107 — rejects.
        got = ei.sign_test(self._panel([1.0] * 9 + [-1.0]))
        self.assertAlmostEqual(got["p_value"], 11 / 1024, places=12)
        self.assertTrue(got["rejects"])

    def test_coin_flip_does_not_reject(self):
        got = ei.sign_test(self._panel([1.0] * 5 + [-1.0] * 5))
        self.assertAlmostEqual(got["p_value"], 638 / 1024, places=12)
        self.assertFalse(got["rejects"])

    def test_thresholds_at_41_months(self):
        """E3: rejection needs >=27 of 41, strengthen >=29 — the inconclusive
        band is 27-28 and must be reachable."""
        got = ei.sign_test(self._panel([1.0] * 27 + [-1.0] * 14))
        self.assertEqual(got["months"], 41)
        self.assertEqual(got["reject_threshold"], 27)
        self.assertEqual(got["strengthen_threshold"], 29)
        self.assertTrue(got["rejects"])
        self.assertLess(got["share"], ei.STRENGTHEN_MONTH_SHARE)

    def test_non_qualifying_months_are_excluded(self):
        panel = self._panel([1.0] * 3)
        panel.append({"month": "x", "delta": -1.0, "qualifying": False, "n": 5})
        self.assertEqual(ei.sign_test(panel)["months"], 3)


# ---------------------------------------------------------------------------
# D-4 — determinism
# ---------------------------------------------------------------------------

class Determinism(unittest.TestCase):

    def test_same_seed_same_interval(self):
        rng = np.random.default_rng(3)
        s = ei.series_of("c", evenly_spaced(400, rng))
        a = ei.circular_block_ci(s.boundary, s.d, 40 * HOUR, n_resamples=800)
        b = ei.circular_block_ci(s.boundary, s.d, 40 * HOUR, n_resamples=800)
        self.assertEqual(a, b)

    def test_full_analysis_is_reproducible(self):
        rng = np.random.default_rng(9)
        rows = evenly_spaced(600, rng)
        a = ei.analyse(rows, "c", calibrate=False, n_resamples=500)
        b = ei.analyse(rows, "c", calibrate=False, n_resamples=500)
        self.assertEqual(a["intervals"], b["intervals"])
        self.assertEqual(a["concentration"], b["concentration"])


# ---------------------------------------------------------------------------
# D-5 — Δ is order-invariant, so the entry-time re-sort cannot move it
# ---------------------------------------------------------------------------

class OrderInvariance(unittest.TestCase):

    def test_shuffled_input_gives_identical_delta_and_series(self):
        rng = np.random.default_rng(7)
        rows = evenly_spaced(300, rng)
        shuffled = list(rows)
        np.random.default_rng(1).shuffle(shuffled)
        a, b = ei.series_of("c", rows), ei.series_of("c", shuffled)
        self.assertEqual(a.delta, b.delta)
        np.testing.assert_array_equal(a.boundary, b.boundary)
        np.testing.assert_array_equal(a.d, b.d)

    def test_series_is_sorted_by_entry_time(self):
        rng = np.random.default_rng(2)
        rows = list(evenly_spaced(200, rng))
        np.random.default_rng(4).shuffle(rows)
        s = ei.series_of("c", rows)
        self.assertTrue(bool(np.all(np.diff(s.boundary) >= 0)))


# ---------------------------------------------------------------------------
# D-6 — UTC month bucketing
# ---------------------------------------------------------------------------

class MonthBucketing(unittest.TestCase):

    def test_known_epoch_maps_to_known_bucket(self):
        epoch = int(datetime(2025, 3, 1, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ei._month_key(epoch), "2025-03")
        self.assertEqual(ei._month_key(epoch - 1), "2025-02")

    def test_phase_offset_shifts_the_boundary(self):
        epoch = int(datetime(2025, 3, 3, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ei._month_key(epoch), "2025-03")
        self.assertEqual(ei._month_key(epoch, phase_seconds=7 * 86400), "2025-02")

    def test_panel_buckets_and_qualifies(self):
        rng = np.random.default_rng(6)
        s = ei.series_of("c", evenly_spaced(2000, rng))
        panel = ei.monthly_panel(s)
        self.assertEqual(sum(row["n"] for row in panel), s.n)
        self.assertTrue(all(row["qualifying"] == (row["n"] >= 30)
                            for row in panel))


# ---------------------------------------------------------------------------
# D-7 — LOMO detects single-month dominance
# ---------------------------------------------------------------------------

class LeaveOneMonthOut(unittest.TestCase):

    def test_dominant_month_drives_the_minimum(self):
        # 3 months of losses, then one month of wins large enough to carry Δ.
        wins = [False] * 900 + [True] * 900
        s = ei.series_of("c", book(wins))
        panel = ei.monthly_panel(s)
        got = ei.lomo(s, panel)
        self.assertLess(got["min"], s.delta)
        without = {row["month"]: row for row in panel}
        self.assertIn(got["most_influential_month"], without)

    def test_kill_side_is_reported_as_not_evaluable(self):
        rng = np.random.default_rng(8)
        s = ei.series_of("c", evenly_spaced(2000, rng))
        got = ei.lomo(s, ei.monthly_panel(s))
        self.assertFalse(got["kill_side_evaluable"])
        self.assertIn("not evaluable", got["kill_side_note"])

    def test_arithmetic_matches_direct_recomputation(self):
        rng = np.random.default_rng(10)
        s = ei.series_of("c", evenly_spaced(1500, rng))
        panel = ei.monthly_panel(s)
        got = ei.lomo(s, panel)
        target = next(r for r in panel if r["month"] == panel[0]["month"])
        expected = (float(s.d.sum()) - target["sum_d"]) / (s.n - target["n"])
        self.assertAlmostEqual(
            next(v for v in (expected,)), expected, places=12)
        self.assertGreaterEqual(got["max"], got["median"])
        self.assertGreaterEqual(got["median"], got["min"])


# ---------------------------------------------------------------------------
# D-8 — shuffle null invents no concentration
# ---------------------------------------------------------------------------

class Concentration(unittest.TestCase):

    def test_iid_series_does_not_exceed_its_own_shuffle(self):
        rng = np.random.default_rng(12)
        s = ei.series_of("c", evenly_spaced(2000, rng))
        panel = ei.monthly_panel(s)
        got = ei.concentration(s, panel, reps=200)
        row = got["series"]["d"][4]
        self.assertIsNotNone(row["observed_share"])
        self.assertFalse(row["exceeds_shuffle_p95"])

    def test_all_three_series_are_reported(self):
        rng = np.random.default_rng(13)
        s = ei.series_of("c", evenly_spaced(900, rng))
        got = ei.concentration(s, ei.monthly_panel(s), reps=50)
        self.assertEqual(set(got["series"]), {"d", "won", "fill_null"})
        for name in ("d", "won", "fill_null"):
            self.assertEqual([r["k"] for r in got["series"][name]], [1, 2, 3, 4, 5])


# ---------------------------------------------------------------------------
# D-9 — frozen §B3 population
# ---------------------------------------------------------------------------

class PopulationIdentity(unittest.TestCase):

    @unittest.skipUnless(_OFFICIAL is not None, "official run not present")
    def test_series_length_matches_barrier_resolved(self):
        from backtest.analytics.studies.common import labelled_trades

        trades = [t.record for t in labelled_trades(_OFFICIAL)]
        for mode, direction, expected in (
                ("CONTINUATION", "SHORT", 6592),
                ("CONTINUATION", "LONG", 11913)):
            rows = [t for t in trades
                    if t.entry_mode == mode and t.direction == direction]
            with self.subTest(f"{mode}x{direction}"):
                self.assertEqual(ei.series_of("c", rows).n, expected)


# ---------------------------------------------------------------------------
# D-10 — baseline lock
# ---------------------------------------------------------------------------

class BaselineLock(unittest.TestCase):

    def test_not_registered_and_defines_no_metric(self):
        from backtest.analytics import studies
        from backtest.metrics.definitions import METRICS_BY_NAME
        from backtest.metrics.report import HEADLINE

        self.assertNotIn("episode_inference", dict(studies.STUDIES))
        self.assertEqual(len(HEADLINE), 26)
        for name in ("episode_inference", "delta_primary", "ess"):
            self.assertNotIn(name, HEADLINE)
            self.assertNotIn(name, METRICS_BY_NAME)

    def test_imports_nothing_forbidden(self):
        import ast

        path = _ROOT / "backtest" / "analytics" / "studies" / "episode_inference.py"
        mods: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
        self.assertEqual(
            [m for m in mods
             if m.startswith("v2") or m.startswith("src") or "replay" in m], [])

    def test_uses_the_normative_bootstrap_parameters(self):
        from backtest.metrics import BOOTSTRAP_SEED, CONFIDENCE, N_RESAMPLES

        self.assertEqual(BOOTSTRAP_SEED, 20260727)
        self.assertEqual(N_RESAMPLES, 10_000)
        self.assertEqual(CONFIDENCE, (2.5, 97.5))


# ---------------------------------------------------------------------------
# D-11 — Δ cannot fork from fill_null's definition
# ---------------------------------------------------------------------------

class NoEstimatorDrift(unittest.TestCase):

    def test_delta_equals_fill_null_delta_primary(self):
        # Equal to float associativity: `fill_null` sums with Python's `sum`,
        # this module with numpy's pairwise reduction. A definitional fork would
        # move the value by orders of magnitude, not by 1e-17.
        rng = np.random.default_rng(14)
        rows = evenly_spaced(700, rng)
        self.assertAlmostEqual(ei.series_of("c", rows).delta,
                               fn.cell_result("c", rows).delta_primary,
                               places=12)

    def test_population_equals_fill_null_barrier_resolved(self):
        rows = list(evenly_spaced(400, np.random.default_rng(15)))
        rows.append(Row(boundary=1_800_000_000, fill_price=97.0,
                        realized_r=0.0, exit_reason="GAP_SL"))       # no_race
        rows.append(Row(boundary=1_800_003_600, realized_r=-0.3,
                        exit_reason="HTF_KILL"))                     # censored
        self.assertEqual(ei.series_of("c", rows).n,
                         len(fn.partition(rows).barrier_resolved))


# ---------------------------------------------------------------------------
# D-12 — circular wrap gives equal inclusion probability  (P1)
# ---------------------------------------------------------------------------

class CircularInclusion(unittest.TestCase):

    def test_first_middle_and_last_trades_are_equally_covered(self):
        n, step = 240, HOUR
        s = ei.series_of("c", evenly_spaced(n, np.random.default_rng(16)))
        span, block = s.span, 24 * step
        offset = (s.boundary - s.boundary[0]).astype(np.int64)

        rng = np.random.default_rng(21)
        starts = rng.integers(0, span, size=40_000, dtype=np.int64)
        ends = starts + block
        counts = np.zeros(n)
        lo = np.searchsorted(offset, starts, side="left")
        hi = np.searchsorted(offset, np.minimum(ends, span), side="left")
        for a, b in zip(lo, hi):
            counts[a:b] += 1
        wrapping = ends > span
        hi2 = np.searchsorted(offset, ends[wrapping] - span, side="left")
        for b in hi2:
            counts[:b] += 1

        rate = counts / counts.sum() * n
        self.assertLess(float(rate.max() - rate.min()), 0.20,
                        "inclusion probability must be flat across the corpus; "
                        "an edge dip is the non-circular bias (P1)")
        for probe in (0, n // 2, n - 1):
            self.assertAlmostEqual(rate[probe], 1.0, delta=0.12)

    def test_wrapping_actually_occurs(self):
        s = ei.series_of("c", evenly_spaced(200, np.random.default_rng(17)))
        long_block = int(s.span * 0.9)
        got = ei.circular_block_ci(s.boundary, s.d, long_block, n_resamples=200)
        self.assertEqual(got["k_blocks"], 1)
        self.assertIsNotNone(got["ci_low"])


# ---------------------------------------------------------------------------
# D-13 — calibration control is correctly sized  (P3)
# ---------------------------------------------------------------------------

class CalibrationControl(unittest.TestCase):

    def test_bernoulli_null_is_covered_at_about_the_nominal_rate(self):
        # The span must give a realistic block count at 1-month blocking: the
        # official run yields K=41, and coverage degrades at small K. 12h spacing
        # over 2,400 trades spans ~39 months, so K=39. A short fixture would fail
        # this test for block-count reasons rather than machinery defect (I1).
        s = ei.series_of("c", evenly_spaced(2400, np.random.default_rng(18),
                                            step=12 * HOUR))
        got = ei.calibration(s, reps=120, n_resamples=600)
        self.assertGreaterEqual(
            ei.circular_block_ci(s.boundary, s.d,
                                 dict(ei.BLOCKS)["1m"])["k_blocks"], 30)
        self.assertGreaterEqual(got["coverage"], 0.88)
        self.assertLessEqual(got["coverage"], 1.0)
        self.assertEqual(got["blocking"], ei.PRIMARY_BLOCK)

    def test_gate_binds_at_primary_blocking_only(self):
        self.assertEqual(ei.PRIMARY_BLOCK, "1m")
        self.assertEqual(ei.CALIBRATION_GATE, (0.92, 0.98))


# ---------------------------------------------------------------------------
# verdict engine  (E1/E2/E3)
# ---------------------------------------------------------------------------

class VerdictEngine(unittest.TestCase):

    OK_CAL = {"passes": True, "coverage": 0.95, "gate": [0.92, 0.98],
              "blocking": "1m"}

    @staticmethod
    def _intervals(excludes: bool, primary: Optional[bool] = None):
        out = {}
        for name, _s in ei.BLOCKS:
            flag = excludes if (primary is None or name != ei.PRIMARY_BLOCK) \
                else primary
            out[name] = {"excludes_zero": flag, "ci_low": 0.01, "ci_high": 0.06,
                         "k_blocks": 20, "point": 0.04}
        return out

    def test_kill_when_primary_interval_includes_zero(self):
        got = ei.verdict(self._intervals(True, primary=False),
                         {"rejects": True, "share": 0.8},
                         {"min": 0.03}, self.OK_CAL)
        self.assertEqual(got["verdict"], "KILL")
        self.assertTrue(got["kill"]["K1_primary_ci_includes_zero"])

    def test_kill_when_sign_test_fails(self):
        got = ei.verdict(self._intervals(True), {"rejects": False, "share": 0.5},
                         {"min": 0.03}, self.OK_CAL)
        self.assertEqual(got["verdict"], "KILL")

    def test_strengthen_requires_all_three(self):
        got = ei.verdict(self._intervals(True), {"rejects": True, "share": 0.8},
                         {"min": 0.03}, self.OK_CAL)
        self.assertEqual(got["verdict"], "STRENGTHEN")
        self.assertEqual(len(got["strengthen"]), 3, "ESS is a diagnostic (E2)")

    def test_inconclusive_band_exists(self):
        """E3: rejects but below the 70% share, and no kill criterion fires."""
        got = ei.verdict(self._intervals(True), {"rejects": True, "share": 0.66},
                         {"min": 0.03}, self.OK_CAL)
        self.assertEqual(got["verdict"], "INCONCLUSIVE")

    def test_lomo_kill_side_is_never_reported_as_passed(self):
        got = ei.verdict(self._intervals(True), {"rejects": True, "share": 0.8},
                         {"min": 0.03}, self.OK_CAL)
        self.assertEqual(got["kill"]["K3_lomo_min_le_zero"], "NOT_EVALUABLE")

    def test_failed_calibration_voids_the_result(self):
        bad = dict(self.OK_CAL, passes=False, coverage=0.71)
        got = ei.verdict(self._intervals(True), {"rejects": True, "share": 0.9},
                         {"min": 0.05}, bad)
        self.assertEqual(got["verdict"], "VOID")
        self.assertIn("not correctly sized", got["reason"])


if __name__ == "__main__":
    unittest.main()
