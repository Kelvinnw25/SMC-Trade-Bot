"""M10 — the Metrics & Baseline Engine.

Every number here is hand-computed in the test, not asserted against whatever the
engine happened to produce. That is the only way a metrics test is worth anything:
a test that records current behaviour would pass just as happily on a wrong formula.

Coverage follows the milestone's task list — simple, mixed, all-WIN, all-LOSS,
all-NOT_FILLED, empty and large datasets; segmentation, expectancy, profit factor
and holding-time correctness; byte-identical repeats — plus the parts §12 makes
normative that the task list does not name: the bootstrap's fixed parameters, the
`inferential` gate, the multiple-comparison caution, the mandatory data-health block
and C-5.7.2's comparison refusal.
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from backtest.dataset import (
    DatasetError, HealthSummary, LABEL_FIELDS, LabeledDataset, canonical_key,
    read_outcomes,
)
from backtest.metrics import (
    BOOTSTRAP_SEED, DIMENSIONS_BY_NAME, HEADLINE, METRICS_BY_NAME,
    MIN_SEGMENT_N, N_RESAMPLES, BaselineError, ComparisonKey, Dimension,
    IncomparableBaseline, ReportInputs, bootstrap_mean, build_equity_curve,
    build_report, c98_order_key, catalogue, compare, compute_metrics, freeze,
    group_by, load_baseline, segment_all,
)
from backtest.outcome.record import OutcomeRecord

HOUR = 3600
T0 = 1699999200


def rec(**kw) -> OutcomeRecord:
    """One outcome record. Defaults describe a filled, winning LONG retracement;
    every test overrides only what it is about."""
    base = dict(
        run_id="replay-test", source="replay",
        thesis_key="BTC/USDT|LONG|RETRACEMENT", symbol="BTC/USDT",
        direction="LONG", entry_mode="RETRACEMENT",
        boundary=T0, h1_last_ts=T0 - HOUR, cycle_index=0, universe_rank=1,
        alert_tier="VALID", shadow_kind=None, in_burn_in=False,
        entry_price=100.0, sl=95.0, tp=115.0, structural_rr=3.0,
        tp_source="EQH_cluster", entry_source="retracement_ob", score=12,
        filled=True, fill_kind="LIMIT_TOUCH", fill_T=T0, fill_price=100.0,
        candles_to_fill=1,
        state="WIN", cancel_reason=None, exit_reason="TP", exit_T=T0 + 3 * HOUR,
        exit_price=115.0, realized_r=3.0, realized_r_net=3.0, holding_candles=3,
        mae_r=-0.3, mfe_r=3.0, excursion_degraded=False,
        contract_version="1.1", strategy_version="v2.0",
        ledger_schema_version=2, outcome_schema_version=1,
        outcome_config_hash="cfg0000000000000", data_snapshot_id="snap000000000000",
        env={"python": "3.13.5"}, signal_id=None,
    )
    base.update(kw)
    return OutcomeRecord(**base)


def win(r: float = 3.0, **kw) -> OutcomeRecord:
    return rec(state="WIN", exit_reason="TP", realized_r=r, realized_r_net=r, **kw)


def loss(r: float = -1.0, **kw) -> OutcomeRecord:
    return rec(state="LOSS", exit_reason="SL", realized_r=r, realized_r_net=r,
               exit_price=95.0, **kw)


def not_filled(reason: str = "DECAY", **kw) -> OutcomeRecord:
    return rec(state="NOT_FILLED", cancel_reason=reason, exit_reason=None,
               filled=False, fill_kind=None, fill_T=None, fill_price=None,
               candles_to_fill=None, exit_T=None, exit_price=None,
               realized_r=None, realized_r_net=None, holding_candles=None,
               mae_r=None, mfe_r=None, **kw)


def spread(records) -> list[OutcomeRecord]:
    """Give each record a distinct boundary and exit so C-9.8 ordering is total
    and the equity path is unambiguous."""
    import dataclasses
    out = []
    for index, record in enumerate(records):
        exit_T = None if record.exit_T is None else T0 + (index + 1) * HOUR
        out.append(dataclasses.replace(
            record, boundary=T0 + index * HOUR,
            h1_last_ts=T0 + index * HOUR - HOUR, exit_T=exit_T,
            cycle_index=index))
    return out


# ==========================================================================
# The catalogue is the contract (Task 5)
# ==========================================================================

class TestCatalogue(unittest.TestCase):

    def test_every_emitted_metric_is_documented(self):
        """A number in the report with no definition is not evidence."""
        metrics = compute_metrics(spread([win(), loss()]))
        undocumented = sorted(set(metrics.values) - set(METRICS_BY_NAME))
        self.assertEqual(undocumented, [], f"undocumented: {undocumented}")

    def test_every_documented_metric_is_emitted(self):
        metrics = compute_metrics(spread([win(), loss(), not_filled()]))
        missing = sorted(set(METRICS_BY_NAME) - set(metrics.values))
        self.assertEqual(missing, [], f"documented but never emitted: {missing}")

    def test_every_definition_states_its_population_and_edge_cases(self):
        for entry in catalogue():
            self.assertIn(entry["population"],
                          {"all", "evaluated", "resolved", "scored", "filled",
                           "fill_decided"}, entry["name"])
            self.assertTrue(entry["formula"], entry["name"])
            self.assertTrue(entry["definition"], entry["name"])
            self.assertTrue(entry["zero_trades"], entry["name"])

    def test_the_headline_set_is_documented(self):
        for name in HEADLINE:
            self.assertIn(name, METRICS_BY_NAME, name)


# ==========================================================================
# Populations and exclusions
# ==========================================================================

class TestPopulations(unittest.TestCase):

    def test_shadows_burn_in_and_invalid_levels_leave_the_headline(self):
        records = spread([
            win(), loss(),
            win(shadow_kind="UNALERTED_INSERT"),
            win(in_burn_in=True),
            rec(state="INVALID_LEVELS", filled=False, realized_r=None,
                realized_r_net=None, exit_reason=None, exit_T=None,
                holding_candles=None, mae_r=None, mfe_r=None),
        ])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["all_records"], 5)
        self.assertEqual(metrics.counts["total_trades"], 2)
        self.assertEqual(metrics.counts["shadow_trades"], 1)
        self.assertEqual(metrics.counts["burn_in_trades"], 1)
        self.assertEqual(metrics.counts["invalid_levels_trades"], 1)
        self.assertEqual(metrics.values["expectancy"], 1.0)   # (3 + -1) / 2

    def test_open_trades_are_reported_and_excluded_from_expectancy(self):
        """C-4.10: a trade still live when the data ended has no finished story."""
        records = spread([
            win(2.0),
            rec(state="ACTIVE", exit_reason=None, exit_T=None, exit_price=None,
                realized_r=None, realized_r_net=None, holding_candles=None),
            rec(state="PENDING", filled=False, fill_kind=None, fill_T=None,
                fill_price=None, candles_to_fill=None, exit_reason=None,
                exit_T=None, exit_price=None, realized_r=None,
                realized_r_net=None, holding_candles=None),
        ])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["total_trades"], 3)
        self.assertEqual(metrics.counts["completed_trades"], 1)
        self.assertEqual(metrics.counts["pending_trades"], 2)
        self.assertEqual(metrics.values["expectancy"], 2.0)

    def test_a_pending_trade_is_not_a_missed_fill(self):
        """The fill question is unsettled while a trade is still PENDING, so it
        leaves the fill-rate denominator rather than counting as a miss."""
        records = spread([
            win(), not_filled(),
            rec(state="PENDING", filled=False, fill_kind=None, fill_T=None,
                fill_price=None, candles_to_fill=None, exit_reason=None,
                exit_T=None, exit_price=None, realized_r=None,
                realized_r_net=None, holding_candles=None),
        ])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["fill_decided_trades"], 2)
        self.assertEqual(metrics.values["fill_rate"], 0.5)


# ==========================================================================
# Arithmetic, hand-computed (Task 6)
# ==========================================================================

class TestArithmetic(unittest.TestCase):

    def test_a_simple_dataset(self):
        """Two wins at +2R, one loss at −1R."""
        metrics = compute_metrics(spread([win(2.0), win(2.0), loss(-1.0)]))
        self.assertEqual(metrics.counts["scored_trades"], 3)
        self.assertAlmostEqual(metrics.values["expectancy"], (2 + 2 - 1) / 3)
        self.assertAlmostEqual(metrics.values["median_r"], 2.0)
        self.assertAlmostEqual(metrics.values["win_rate"], 2 / 3)
        self.assertAlmostEqual(metrics.values["loss_rate"], 1 / 3)
        self.assertAlmostEqual(metrics.values["gross_profit"], 4.0)
        self.assertAlmostEqual(metrics.values["gross_loss"], 1.0)
        self.assertAlmostEqual(metrics.values["profit_factor"], 4.0)
        self.assertAlmostEqual(metrics.values["avg_win"], 2.0)
        self.assertAlmostEqual(metrics.values["avg_loss"], -1.0)
        self.assertAlmostEqual(metrics.values["payoff_ratio"], 2.0)
        self.assertEqual(metrics.values["largest_win"], 2.0)
        self.assertEqual(metrics.values["largest_loss"], -1.0)

    def test_a_mixed_dataset(self):
        """Every terminal state at once, with the denominators kept apart."""
        records = spread([
            win(3.0), win(1.5), loss(-1.0), loss(-1.8),
            not_filled("KILL"), not_filled("TTL"),
            rec(state="EXPIRED", exit_reason="MAX_HOLDING", realized_r=0.4,
                realized_r_net=0.4, holding_candles=168),
            rec(state="INVALIDATED_EXIT", exit_reason="HTF_KILL", realized_r=-0.5,
                realized_r_net=-0.5, holding_candles=6),
        ])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["total_trades"], 8)
        self.assertEqual(metrics.counts["completed_trades"], 8)
        self.assertEqual(metrics.counts["scored_trades"], 6)

        # scored-population rates
        self.assertAlmostEqual(metrics.values["win_rate"], 2 / 6)
        self.assertAlmostEqual(metrics.values["loss_rate"], 2 / 6)
        # resolved-population rates
        self.assertAlmostEqual(metrics.values["not_filled_rate"], 2 / 8)
        self.assertAlmostEqual(metrics.values["expired_rate"], 1 / 8)
        self.assertAlmostEqual(metrics.values["invalidated_rate"], 1 / 8)
        self.assertAlmostEqual(metrics.values["win_rate_of_completed"], 2 / 8)
        self.assertAlmostEqual(metrics.values["kill_exit_rate"], 1 / 8)
        self.assertAlmostEqual(metrics.values["kill_exit_mean_r"], -0.5)
        self.assertAlmostEqual(metrics.values["expiry_mean_r"], 0.4)

        total = 3.0 + 1.5 - 1.0 - 1.8 + 0.4 - 0.5
        self.assertAlmostEqual(metrics.values["expectancy"], total / 6)
        # per signal: the two cancellations enter as 0R over 8 resolved trades
        self.assertAlmostEqual(metrics.values["expectancy_per_signal"], total / 8)
        self.assertAlmostEqual(metrics.values["gross_profit"], 3.0 + 1.5 + 0.4)
        self.assertAlmostEqual(metrics.values["gross_loss"], 1.0 + 1.8 + 0.5)
        self.assertAlmostEqual(metrics.values["profit_factor"], 4.9 / 3.3)

    def test_all_win(self):
        metrics = compute_metrics(spread([win(1.0), win(2.0), win(3.0)]))
        self.assertEqual(metrics.values["win_rate"], 1.0)
        self.assertEqual(metrics.values["loss_rate"], 0.0)
        self.assertEqual(metrics.values["gross_loss"], 0.0)
        self.assertIsNone(metrics.values["profit_factor"])
        self.assertTrue(metrics.flags["profit_factor_undefined"])
        self.assertIsNone(metrics.values["avg_loss"])
        self.assertIsNone(metrics.values["payoff_ratio"])
        self.assertEqual(metrics.values["max_drawdown_r"], 0.0)
        self.assertEqual(metrics.counts["max_consecutive_losses"], 0)

    def test_all_loss(self):
        metrics = compute_metrics(spread([loss(-1.0), loss(-1.0), loss(-1.2)]))
        self.assertEqual(metrics.values["loss_rate"], 1.0)
        self.assertEqual(metrics.values["win_rate"], 0.0)
        self.assertEqual(metrics.values["gross_profit"], 0.0)
        self.assertEqual(metrics.values["profit_factor"], 0.0)
        self.assertFalse(metrics.flags["profit_factor_undefined"])
        self.assertIsNone(metrics.values["avg_win"])
        self.assertAlmostEqual(metrics.values["expectancy"], -3.2 / 3)
        self.assertAlmostEqual(metrics.values["max_drawdown_r"], 3.2)
        self.assertEqual(metrics.counts["max_consecutive_losses"], 3)

    def test_all_not_filled(self):
        """No R exists, so every R metric is None — never 0.0."""
        metrics = compute_metrics(spread([not_filled(), not_filled("TTL")]))
        self.assertEqual(metrics.counts["completed_trades"], 2)
        self.assertEqual(metrics.counts["scored_trades"], 0)
        self.assertEqual(metrics.values["not_filled_rate"], 1.0)
        self.assertEqual(metrics.values["fill_rate"], 0.0)
        for name in ("expectancy", "median_r", "win_rate", "loss_rate",
                     "avg_win", "avg_loss", "profit_factor", "payoff_ratio",
                     "largest_win", "largest_loss", "avg_holding_candles",
                     "max_drawdown_r"):
            self.assertIsNone(metrics.values[name], name)
        self.assertEqual(metrics.values["gross_profit"], 0.0)
        self.assertEqual(metrics.distributions["cancel_reason"],
                         {"DECAY": 1, "TTL": 1})

    def test_an_empty_dataset(self):
        metrics = compute_metrics([])
        self.assertEqual(metrics.counts["total_trades"], 0)
        self.assertEqual(metrics.n, 0)
        for name, definition in METRICS_BY_NAME.items():
            value = metrics.values[name]
            if definition.kind == "count":
                self.assertEqual(value, 0.0, name)
            elif definition.kind == "sum" and name.startswith("gross"):
                self.assertEqual(value, 0.0, name)
            else:
                self.assertIsNone(value, name)
        self.assertFalse(metrics.flags["profit_factor_undefined"])
        self.assertFalse(metrics.inferential)

    def test_no_metric_is_ever_nan_or_infinite(self):
        """`canonical_json` is built with `allow_nan=False` (C-9.7), so a NaN would
        make the report unserializable."""
        for records in ([], spread([win()]), spread([loss()]),
                        spread([not_filled()]),
                        spread([win(1.0), loss(-1.0), not_filled()])):
            metrics = compute_metrics(records)
            for name, value in metrics.values.items():
                if value is None:
                    continue
                self.assertTrue(math.isfinite(value), f"{name}={value}")

    def test_holding_time(self):
        records = spread([
            win(1.0, holding_candles=2), win(1.0, holding_candles=4),
            loss(-1.0, holding_candles=9),
            not_filled(),                       # contributes no holding time
        ])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["avg_holding_candles_n"], 3)
        self.assertAlmostEqual(metrics.values["avg_holding_candles"], 5.0)
        self.assertAlmostEqual(metrics.values["median_holding_candles"], 4.0)

    def test_time_to_fill(self):
        records = spread([win(candles_to_fill=0), win(candles_to_fill=2),
                          not_filled()])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["time_to_fill_n"], 2)
        self.assertAlmostEqual(metrics.values["avg_time_to_fill"], 1.0)

    def test_the_immediate_fill_share(self):
        records = spread([win(fill_kind="IMMEDIATE"), win(fill_kind="LIMIT_TOUCH"),
                          win(fill_kind="MARKET_WALK_ORIGIN")])
        metrics = compute_metrics(records)
        self.assertAlmostEqual(metrics.values["immediate_fill_share"], 1 / 3)

    def test_the_breakeven_bucket_overlaps_rather_than_partitions(self):
        records = spread([win(0.05), loss(-0.02), win(3.0)])
        metrics = compute_metrics(records)
        self.assertAlmostEqual(metrics.values["breakeven_rate"], 2 / 3)
        # ... and those two trades are still WIN and LOSS by state.
        self.assertAlmostEqual(metrics.values["win_rate"], 2 / 3)
        self.assertAlmostEqual(metrics.values["loss_rate"], 1 / 3)

    def test_the_structural_versus_realized_gap(self):
        records = spread([win(1.0, structural_rr=3.0),
                          loss(-1.0, structural_rr=2.0)])
        metrics = compute_metrics(records)
        self.assertAlmostEqual(metrics.values["avg_structural_rr"], 2.5)
        self.assertAlmostEqual(metrics.values["expectancy"], 0.0)
        self.assertAlmostEqual(metrics.values["structural_vs_realized_gap"], 2.5)

    def test_net_metrics_track_the_cost_model(self):
        records = spread([
            rec(state="WIN", exit_reason="TP", realized_r=2.0,
                realized_r_net=1.9),
            rec(state="LOSS", exit_reason="SL", realized_r=-1.0,
                realized_r_net=-1.1),
        ])
        metrics = compute_metrics(records)
        self.assertAlmostEqual(metrics.values["expectancy"], 0.5)
        self.assertAlmostEqual(metrics.values["expectancy_net"], 0.4)
        self.assertAlmostEqual(metrics.values["profit_factor_net"], 1.9 / 1.1)

    def test_the_r_histogram_uses_fixed_bins(self):
        records = spread([win(0.25), win(1.2), loss(-1.0), loss(-2.5)])
        histogram = compute_metrics(records).distributions["r_histogram"]
        self.assertEqual(histogram["[0,0.5)"], 1)
        self.assertEqual(histogram["[1,1.5)"], 1)
        self.assertEqual(histogram["[-1,-0.5)"], 1)
        self.assertEqual(histogram["[-5,-2)"], 1)

    def test_a_large_dataset_stays_consistent(self):
        records = spread([win(1.0) if index % 3 else loss(-1.0)
                          for index in range(600)])
        metrics = compute_metrics(records)
        self.assertEqual(metrics.counts["scored_trades"], 600)
        self.assertEqual(metrics.counts["win_count"], 400)
        self.assertEqual(metrics.counts["loss_count"], 200)
        self.assertAlmostEqual(metrics.values["expectancy"], 200 / 600)
        self.assertAlmostEqual(metrics.values["profit_factor"], 2.0)
        self.assertTrue(metrics.inferential)


# ==========================================================================
# The equity path (C-9.8)
# ==========================================================================

class TestEquity(unittest.TestCase):

    def test_the_order_is_by_exit_not_by_instantiation(self):
        early_open_late_exit = win(1.0, boundary=T0, exit_T=T0 + 100 * HOUR)
        late_open_early_exit = loss(-1.0, boundary=T0 + 50 * HOUR,
                                    exit_T=T0 + 51 * HOUR)
        curve = build_equity_curve([early_open_late_exit, late_open_early_exit])
        self.assertEqual(curve.points, (-1.0, 0.0))

    def test_the_c98_tie_break_is_total_and_deterministic(self):
        left = win(1.0, exit_T=T0, cycle_index=1, universe_rank=2, symbol="A/USDT")
        right = loss(-1.0, exit_T=T0, cycle_index=1, universe_rank=2,
                     symbol="B/USDT")
        self.assertLess(c98_order_key(left), c98_order_key(right))
        self.assertEqual(build_equity_curve([right, left]).points, (1.0, 0.0))

    def test_the_running_peak_starts_at_zero(self):
        """C-9.8 starts the sum at 0, so a first-trade loss is already a drawdown.
        Anchoring the peak at the first point would hide it."""
        curve = build_equity_curve(spread([loss(-2.0), win(1.0)]))
        self.assertEqual(curve.points, (-2.0, -1.0))
        self.assertAlmostEqual(curve.max_drawdown_r, 2.0)

    def test_drawdown_measures_peak_to_trough(self):
        curve = build_equity_curve(spread([
            win(3.0), loss(-1.0), loss(-1.0), win(0.5), loss(-1.0)]))
        # path: 3.0, 2.0, 1.0, 1.5, 0.5  -> peak 3.0, trough 0.5
        self.assertEqual(curve.points, (3.0, 2.0, 1.0, 1.5, 0.5))
        self.assertAlmostEqual(curve.max_drawdown_r, 2.5)
        self.assertAlmostEqual(curve.final_r, 0.5)

    def test_unfilled_and_open_trades_contribute_nothing(self):
        curve = build_equity_curve(spread([
            win(1.0), not_filled(),
            rec(state="ACTIVE", realized_r=None, realized_r_net=None,
                exit_T=None, exit_reason=None, holding_candles=None),
        ]))
        self.assertEqual(curve.trades, 1)
        self.assertEqual(curve.points, (1.0,))

    def test_streaks_go_by_sign_not_by_state(self):
        """An EXPIRED trade that closed below its fill was a loss as the operator
        experienced it."""
        curve = build_equity_curve(spread([
            loss(-1.0), rec(state="EXPIRED", exit_reason="MAX_HOLDING",
                            realized_r=-0.3, realized_r_net=-0.3),
            loss(-1.0), win(1.0)]))
        self.assertEqual(curve.max_consecutive_losses, 3)
        self.assertEqual(curve.max_consecutive_wins, 1)

    def test_a_zero_r_trade_breaks_both_streaks(self):
        curve = build_equity_curve(spread([loss(-1.0), win(0.0), loss(-1.0)]))
        self.assertEqual(curve.max_consecutive_losses, 1)

    def test_an_empty_curve_is_none_not_zero(self):
        curve = build_equity_curve([])
        self.assertEqual(curve.trades, 0)
        self.assertIsNone(curve.max_drawdown_r)
        self.assertIsNone(curve.final_r)


# ==========================================================================
# The bootstrap (§12, normative)
# ==========================================================================

class TestBootstrap(unittest.TestCase):

    def test_the_normative_parameters_are_what_the_contract_fixes(self):
        self.assertEqual(N_RESAMPLES, 10_000)
        self.assertEqual(BOOTSTRAP_SEED, 20260727)
        self.assertEqual(MIN_SEGMENT_N, 30)

    def test_scipy_is_never_imported(self):
        """§12: scipy is not available and MUST NOT be introduced."""
        from tests.backtest.test_historical_adapter import imported_modules
        for path in (Path("backtest") / "metrics").glob("*.py"):
            for module in imported_modules(path):
                self.assertNotEqual(module.split(".")[0], "scipy",
                                    f"{path.name} imports {module}")

    def test_the_interval_is_reproducible_from_the_seed(self):
        values = [float(index % 7) - 3.0 for index in range(80)]
        first = bootstrap_mean(values)
        second = bootstrap_mean(values)
        self.assertEqual(first.low, second.low)
        self.assertEqual(first.high, second.high)

    def test_the_point_estimate_is_exact_not_resampled(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(bootstrap_mean(values).point, 2.5)

    def test_the_interval_brackets_the_point(self):
        values = [float(index % 5) - 2.0 for index in range(100)]
        interval = bootstrap_mean(values)
        self.assertLessEqual(interval.low, interval.point)
        self.assertLessEqual(interval.point, interval.high)

    def test_below_min_segment_n_the_interval_is_flagged(self):
        self.assertFalse(bootstrap_mean([1.0] * 29).inferential)
        self.assertTrue(bootstrap_mean([1.0] * 30).inferential)

    def test_a_single_value_collapses_and_is_never_inferential(self):
        interval = bootstrap_mean([2.0])
        self.assertEqual((interval.low, interval.point, interval.high),
                         (2.0, 2.0, 2.0))
        self.assertFalse(interval.inferential)

    def test_an_empty_population_yields_no_interval(self):
        interval = bootstrap_mean([])
        self.assertIsNone(interval.point)
        self.assertIsNone(interval.low)
        self.assertEqual(interval.n, 0)
        self.assertFalse(interval.inferential)

    def test_a_segments_interval_does_not_depend_on_what_preceded_it(self):
        """A fresh generator per interval — otherwise a report's numbers would
        depend on how many segments were computed before them."""
        values = [float(index % 3) - 1.0 for index in range(50)]
        alone = bootstrap_mean(values)
        for _ in range(5):
            bootstrap_mean([9.0] * 40)
        again = bootstrap_mean(values)
        self.assertEqual(alone.low, again.low)
        self.assertEqual(alone.high, again.high)

    def test_the_engine_attaches_intervals_to_expectancy_and_win_rate(self):
        metrics = compute_metrics(spread(
            [win(1.0) if index % 2 else loss(-1.0) for index in range(40)]))
        self.assertIn("expectancy", metrics.intervals)
        self.assertIn("win_rate", metrics.intervals)
        self.assertTrue(metrics.intervals["expectancy"].inferential)
        self.assertAlmostEqual(metrics.intervals["win_rate"].point, 0.5)


# ==========================================================================
# Segmentation (Task 3)
# ==========================================================================

class TestSegmentation(unittest.TestCase):

    def test_grouping_by_an_outcome_dimension(self):
        records = spread([
            win(2.0, direction="LONG"), loss(-1.0, direction="LONG"),
            win(4.0, direction="SHORT"),
        ])
        result = group_by(DIMENSIONS_BY_NAME["direction"], records)
        self.assertEqual(sorted(result.segments), ["LONG", "SHORT"])
        self.assertAlmostEqual(result.segments["LONG"].values["expectancy"], 0.5)
        self.assertAlmostEqual(result.segments["SHORT"].values["expectancy"], 4.0)
        self.assertEqual(result.excluded, 0)

    def test_segments_of_a_partitioning_dimension_sum_to_the_population(self):
        records = spread([win(1.0), loss(-1.0), win(2.0, direction="SHORT"),
                          not_filled()])
        total = compute_metrics(records).counts["total_trades"]
        result = group_by(DIMENSIONS_BY_NAME["direction"], records)
        self.assertTrue(result.dimension.partitions)
        self.assertEqual(
            sum(s.counts["total_trades"] for s in result.segments.values()),
            total)

    def test_grouping_by_a_label_dimension(self):
        records = spread([win(2.0), loss(-1.0)])
        labels = {
            canonical_key(r.run_id, r.symbol, r.h1_last_ts):
                {"regime": "expansion_up" if index == 0 else "compression"}
            for index, r in enumerate(records)
        }
        result = group_by(DIMENSIONS_BY_NAME["regime"], records, labels=labels)
        self.assertEqual(sorted(result.segments), ["compression", "expansion_up"])
        self.assertAlmostEqual(
            result.segments["expansion_up"].values["expectancy"], 2.0)

    def test_a_label_dimension_without_labels_is_unavailable_not_empty(self):
        result = group_by(DIMENSIONS_BY_NAME["regime"], spread([win()]))
        self.assertFalse(result.available)
        self.assertEqual(result.excluded, 1)
        self.assertEqual(result.segments, {})

    def test_a_missing_flag_is_excluded_rather_than_bucketed_as_false(self):
        """"the confirmation was false" and "we do not know" are different facts."""
        records = spread([win(1.0), win(2.0)])
        keys = [canonical_key(r.run_id, r.symbol, r.h1_last_ts) for r in records]
        labels = {keys[0]: {"sweep": False}, keys[1]: {"sweep": None}}
        result = group_by(DIMENSIONS_BY_NAME["sweep"], records, labels=labels)
        self.assertEqual(sorted(result.segments), ["false"])
        self.assertEqual(result.excluded, 1)

    def test_a_multi_valued_dimension_does_not_partition(self):
        records = spread([win(2.0), loss(-1.0)])
        keys = [canonical_key(r.run_id, r.symbol, r.h1_last_ts) for r in records]
        labels = {
            keys[0]: {"notes": ["sweep_ok", "fvg_ok"]},
            keys[1]: {"notes": ["sweep_ok"]},
        }
        result = group_by(DIMENSIONS_BY_NAME["notes_token"], records,
                          labels=labels)
        self.assertFalse(result.dimension.partitions)
        self.assertEqual(result.segments["sweep_ok"].counts["total_trades"], 2)
        self.assertEqual(result.segments["fvg_ok"].counts["total_trades"], 1)
        self.assertGreater(
            sum(s.counts["total_trades"] for s in result.segments.values()),
            len(records))

    def test_buckets_have_fixed_edges(self):
        records = spread([win(1.0, structural_rr=1.2),
                          win(1.0, structural_rr=2.7),
                          win(1.0, structural_rr=9.0)])
        result = group_by(DIMENSIONS_BY_NAME["rr_bucket"], records)
        self.assertEqual(set(result.segments),
                         {"[1,1.5)", "[2.5,3)", ">=6"})

    def test_htf_alignment_reads_the_biases_against_the_direction(self):
        long_aligned = win(1.0, direction="LONG")
        long_opposed = loss(-1.0, direction="LONG", symbol="ETH/USDT")
        short_aligned = win(1.0, direction="SHORT", symbol="SOL/USDT")
        records = spread([long_aligned, long_opposed, short_aligned])
        keys = [canonical_key(r.run_id, r.symbol, r.h1_last_ts) for r in records]
        labels = {
            keys[0]: {"d1_bias": "BULLISH", "h4_bias": "BULLISH"},
            keys[1]: {"d1_bias": "BEARISH", "h4_bias": "BEARISH"},
            keys[2]: {"d1_bias": "BEARISH", "h4_bias": "CHOPPY"},
        }
        result = group_by(DIMENSIONS_BY_NAME["htf_alignment"], records,
                          labels=labels)
        self.assertEqual(sorted(result.segments),
                         ["ALIGNED", "OPPOSED", "PARTIAL"])

    def test_the_regime_class_is_a_reporting_grouping_over_the_real_labels(self):
        records = spread([win(1.0), win(1.0), win(1.0)])
        keys = [canonical_key(r.run_id, r.symbol, r.h1_last_ts) for r in records]
        labels = {
            keys[0]: {"regime": "expansion_up"},
            keys[1]: {"regime": "rotational"},
            keys[2]: {"regime": "compression"},
        }
        classes = group_by(DIMENSIONS_BY_NAME["regime_class"], records,
                           labels=labels)
        self.assertEqual(sorted(classes.segments),
                         ["CHOPPY", "RANGING", "TRENDING"])
        # the raw label stays available and unmapped
        raw = group_by(DIMENSIONS_BY_NAME["regime"], records, labels=labels)
        self.assertEqual(sorted(raw.segments),
                         ["compression", "expansion_up", "rotational"])

    def test_the_window_truncation_flag(self):
        records = spread([win(1.0), win(1.0)])
        keys = [canonical_key(r.run_id, r.symbol, r.h1_last_ts) for r in records]
        labels = {
            keys[0]: {"d1_rows": 119, "h1_rows": 499},
            keys[1]: {"d1_rows": 40, "h1_rows": 499},
        }
        result = group_by(DIMENSIONS_BY_NAME["window_truncated"], records,
                          labels=labels)
        self.assertEqual(result.segments["false"].counts["total_trades"], 1)
        self.assertEqual(result.segments["true"].counts["total_trades"], 1)

    def test_a_new_dimension_needs_no_report_change(self):
        """Task 3: reusable grouping, no hardcoded report logic."""
        dimension = Dimension(
            "entry_price_parity",
            lambda record, _labels: "even" if int(record.entry_price) % 2 == 0
            else "odd",
            description="a test dimension")
        records = spread([win(1.0, entry_price=100.0), win(1.0, entry_price=101.0)])
        result = group_by(dimension, records)
        self.assertEqual(sorted(result.segments), ["even", "odd"])

    def test_segment_all_covers_every_registered_dimension(self):
        records = spread([win(1.0), loss(-1.0)])
        segmentation = segment_all(records, with_intervals=False)
        self.assertEqual(set(segmentation.dimensions), set(DIMENSIONS_BY_NAME))

    def test_the_multiple_comparison_caution_fires_above_twenty_segments(self):
        records = spread([win(1.0, symbol=f"S{index}/USDT")
                          for index in range(25)])
        segmentation = segment_all(
            records, dimensions=[DIMENSIONS_BY_NAME["symbol"]],
            with_intervals=False)
        self.assertEqual(segmentation.total_segments, 25)
        self.assertTrue(segmentation.multiple_comparison_caution)
        self.assertIsNotNone(
            segmentation.to_dict()["multiple_comparison_note"])

    def test_small_segments_are_reported_but_flagged(self):
        records = spread([win(1.0)] * 5)
        result = group_by(DIMENSIONS_BY_NAME["direction"], records)
        self.assertEqual(len(result.segments), 1)
        self.assertFalse(result.segments["LONG"].inferential)
        self.assertEqual(result.inferential_segments, ())


# ==========================================================================
# The report (Task 4)
# ==========================================================================

def sample_records() -> list[OutcomeRecord]:
    """A population with both directions, both modes, shadows and burn-in."""
    records = []
    for index in range(40):
        if index % 4 == 0:
            records.append(loss(-1.0, direction="SHORT",
                                entry_mode="CONTINUATION"))
        elif index % 4 == 1:
            records.append(win(2.0))
        elif index % 4 == 2:
            records.append(not_filled("TTL"))
        else:
            records.append(win(1.5, direction="SHORT", alert_tier="A+"))
    records.append(win(5.0, shadow_kind="UNALERTED_INSERT"))
    records.append(win(1.0, shadow_kind="CAPPED"))
    records.append(loss(-1.0, in_burn_in=True))
    return spread(records)


def sample_inputs() -> ReportInputs:
    return ReportInputs(
        run_id="replay-test", window_start=T0, window_end=T0 + 42 * HOUR,
        burn_in_h1=0, universe_mode="volume", top_coins_limit=30,
        label_fields=LABEL_FIELDS)


class TestReport(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def report(self, **kw):
        params = dict(inputs=sample_inputs(), segment_intervals=False)
        params.update(kw)
        return build_report(sample_records(), **params)

    def test_the_report_carries_identity_lineage_and_the_catalogue(self):
        payload = self.report().to_dict()
        self.assertEqual(payload["schema"], "baseline_report")
        self.assertEqual(payload["lineage"]["data_snapshot_ids"],
                         ["snap000000000000"])
        self.assertEqual(payload["lineage"]["sources"], ["replay"])
        self.assertEqual(payload["lineage"]["strategy_versions"], ["v2.0"])
        self.assertEqual(payload["inputs"]["burn_in_h1"], 0)
        self.assertEqual(payload["inputs"]["universe_mode"], "volume")
        self.assertEqual(payload["bootstrap_seed"], BOOTSTRAP_SEED)
        self.assertEqual(len(payload["catalogue"]), len(METRICS_BY_NAME))

    def test_shadow_classes_are_reported_separately_and_never_merged(self):
        report = self.report()
        self.assertEqual(sorted(report.shadow_metrics),
                         ["CAPPED", "UNALERTED_INSERT"])
        self.assertEqual(report.global_metrics.counts["shadow_trades"], 2)
        # the +5R shadow is nowhere near the headline expectancy
        self.assertAlmostEqual(
            report.shadow_metrics["UNALERTED_INSERT"].values["expectancy"], 5.0)
        self.assertLess(report.global_metrics.values["expectancy"], 2.0)

    def test_a_missing_health_block_is_called_out(self):
        """§12 makes the data-health block mandatory."""
        markdown = self.report().to_markdown()
        self.assertIn("No data-health block was supplied", markdown)

    def test_a_supplied_health_block_is_rendered(self):
        health = HealthSummary(cycles=48, universe_total=48, ledger_rows=48,
                               decisions=48).to_dict()
        markdown = self.report(health=health).to_markdown()
        self.assertIn("universe_coverage", markdown)
        self.assertNotIn("No data-health block was supplied", markdown)

    def test_an_unclean_run_is_flagged(self):
        health = HealthSummary(cycles=2, swallowed_before_ledger=3).to_dict()
        self.assertIn("not clean", self.report(health=health).to_markdown())

    def test_the_report_declares_mixed_lineage(self):
        records = spread([win(1.0), win(1.0, strategy_version="v2.1")])
        report = build_report(records, inputs=sample_inputs(),
                              segment_intervals=False)
        self.assertTrue(report.lineage["mixed_strategy_version"])
        self.assertIn("mixed strategy_version", report.to_markdown())

    def test_all_three_artifacts_are_written(self):
        paths = self.report().write(self.out / "baseline")
        self.assertEqual(sorted(paths), ["metrics", "report", "segments"])
        for path in paths.values():
            self.assertTrue(path.exists())
        self.assertIn("# Baseline Report",
                      paths["report"].read_text(encoding="utf-8"))

    def test_writing_over_an_existing_report_is_refused(self):
        report = self.report()
        report.write(self.out / "b")
        with self.assertRaises(FileExistsError):
            report.write(self.out / "b")
        report.write(self.out / "b", overwrite=True)

    def test_the_csv_has_one_row_per_segment_plus_a_global_row(self):
        report = self.report()
        lines = report.to_segments_csv().strip().split("\n")
        expected = 1 + 1 + report.segmentation.total_segments
        self.assertEqual(len(lines), expected)
        self.assertTrue(lines[1].startswith("__global__,all,"))
        self.assertNotIn("\r", report.to_segments_csv())

    def test_the_csv_leaves_missing_values_blank_not_zero(self):
        report = build_report(spread([not_filled()]), inputs=sample_inputs(),
                              segment_intervals=False)
        row = report.to_segments_csv().strip().split("\n")[1].split(",")
        header = report.to_segments_csv().split("\n")[0].split(",")
        self.assertEqual(row[header.index("expectancy")], "")

    def test_the_json_is_serialized_strictly(self):
        """`allow_nan=False` is the real guarantee: a non-finite value raises at
        serialization time rather than producing JSON no strict parser accepts.
        Asserted by round-tripping, not by grepping for the words — the catalogue
        text legitimately mentions both."""
        payload = self.report().to_json()
        json.loads(payload)
        self.assertEqual(
            json.dumps(json.loads(payload), allow_nan=False,
                       sort_keys=True, indent=2) + "\n", payload)

    def test_a_zero_trade_report_still_renders(self):
        report = build_report([], inputs=sample_inputs(),
                              segment_intervals=False)
        self.assertEqual(report.global_metrics.counts["total_trades"], 0)
        self.assertIn("# Baseline Report", report.to_markdown())
        json.loads(report.to_json())

    def test_a_not_inferential_population_is_warned_about(self):
        report = build_report(spread([win(1.0)] * 3), inputs=sample_inputs(),
                              segment_intervals=False)
        self.assertIn("Not inferential", report.to_markdown())


# ==========================================================================
# Determinism (Task 4 / Task 6)
# ==========================================================================

class TestDeterminism(unittest.TestCase):

    def test_two_reports_over_the_same_records_are_byte_identical(self):
        left = build_report(sample_records(), inputs=sample_inputs())
        right = build_report(sample_records(), inputs=sample_inputs())
        self.assertEqual(left.to_json(), right.to_json())
        self.assertEqual(left.to_markdown(), right.to_markdown())
        self.assertEqual(left.to_segments_csv(), right.to_segments_csv())
        self.assertEqual(left.metrics_hash, right.metrics_hash)

    def test_record_order_does_not_change_the_numbers(self):
        records = sample_records()
        left = build_report(records, inputs=sample_inputs(),
                            segment_intervals=False)
        right = build_report(list(reversed(records)), inputs=sample_inputs(),
                             segment_intervals=False)
        self.assertEqual(left.to_json(), right.to_json())

    def test_generated_at_is_the_only_non_reproducible_key(self):
        base = build_report(sample_records(), inputs=sample_inputs(),
                            segment_intervals=False).to_dict()
        stamped = build_report(
            sample_records(),
            inputs=ReportInputs(**{**sample_inputs().__dict__,
                                   "generated_at": "2026-07-30T00:00:00Z"}),
            segment_intervals=False).to_dict()
        self.assertEqual(stamped.pop("generated_at"), "2026-07-30T00:00:00Z")
        self.assertEqual(base, stamped)

    def test_the_metrics_hash_covers_the_numbers_and_not_the_lineage(self):
        records = sample_records()
        left = build_report(records, inputs=sample_inputs(),
                            segment_intervals=False)
        right = build_report(
            records,
            inputs=ReportInputs(**{**sample_inputs().__dict__,
                                   "notes": ("a note",)}),
            segment_intervals=False)
        self.assertEqual(left.metrics_hash, right.metrics_hash)

    def test_a_changed_number_changes_the_hash(self):
        left = build_report(sample_records(), inputs=sample_inputs(),
                            segment_intervals=False)
        right = build_report(sample_records() + spread([win(9.0)]),
                             inputs=sample_inputs(), segment_intervals=False)
        self.assertNotEqual(left.metrics_hash, right.metrics_hash)

    def test_the_written_files_are_byte_identical_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = build_report(sample_records(), inputs=sample_inputs()).write(
                root / "a")
            b = build_report(sample_records(), inputs=sample_inputs()).write(
                root / "b")
            for name in a:
                self.assertEqual(a[name].read_bytes(), b[name].read_bytes(),
                                 name)


# ==========================================================================
# Baseline freeze and comparison (Task 8)
# ==========================================================================

class TestBaseline(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def big_report(self, **kw):
        """A population above `min_segment_n`, so a freeze is not refused."""
        records = spread([win(2.0) if index % 3 else loss(-1.0)
                          for index in range(60)])
        params = dict(inputs=sample_inputs(), segment_intervals=False)
        params.update(kw)
        return build_report(records, **params)

    def test_freezing_writes_the_bundle_and_the_lock(self):
        baseline = freeze(self.big_report(), self.out / "baseline",
                          label="phase-8-baseline")
        for name in ("metrics.json", "report.md", "segments.csv",
                     "baseline.json"):
            self.assertTrue((self.out / "baseline" / name).exists(), name)
        self.assertTrue(baseline.baseline_id.startswith("baseline-"))
        self.assertEqual(baseline.label, "phase-8-baseline")
        self.assertEqual(baseline.strategy_versions, ("v2.0",))

    def test_the_lock_round_trips(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        again = load_baseline(self.out / "b")
        self.assertEqual(again.baseline_id, baseline.baseline_id)
        self.assertEqual(again.key, baseline.key)
        self.assertEqual(again.metrics_hash, baseline.metrics_hash)
        self.assertEqual(again.headline, baseline.headline)

    def test_the_lock_records_the_formula_of_every_headline_metric(self):
        freeze(self.big_report(), self.out / "b", label="x")
        payload = json.loads(
            (self.out / "b" / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(payload["metric_definitions"]),
                         sorted(n for n in HEADLINE if n in METRICS_BY_NAME))

    def test_verification_catches_an_edited_baseline(self):
        report = self.big_report()
        baseline = freeze(report, self.out / "b", label="x")
        baseline.verify(report)                       # unchanged: passes
        drifted = build_report(
            spread([win(2.0)] * 60), inputs=sample_inputs(),
            segment_intervals=False)
        with self.assertRaises(BaselineError):
            baseline.verify(drifted)

    def test_an_undeclared_comparison_key_refuses_the_freeze(self):
        """C-5.7.2 names burn_in_h1 and universe_mode, and no OutcomeRecord
        carries either."""
        report = self.big_report(inputs=ReportInputs(run_id="r"))
        with self.assertRaises(BaselineError) as ctx:
            freeze(report, self.out / "b", label="x")
        self.assertIn("burn_in_h1", str(ctx.exception))
        freeze(report, self.out / "b", label="x", allow_undeclared=True)

    def test_a_population_below_min_segment_n_refuses_the_freeze(self):
        report = build_report(spread([win(1.0)] * 5), inputs=sample_inputs(),
                             segment_intervals=False)
        with self.assertRaises(BaselineError) as ctx:
            freeze(report, self.out / "b", label="x")
        self.assertIn("min_segment_n", str(ctx.exception))
        freeze(report, self.out / "b", label="x", allow_not_inferential=True)

    def test_a_mixed_population_cannot_be_frozen(self):
        records = spread([win(1.0)] * 30
                         + [win(1.0, data_snapshot_id="other")] * 30)
        report = build_report(records, inputs=sample_inputs(),
                              segment_intervals=False)
        with self.assertRaises(BaselineError) as ctx:
            freeze(report, self.out / "b", label="x")
        self.assertIn("data_snapshot_id", str(ctx.exception))

    def test_refreezing_over_a_lock_is_refused(self):
        freeze(self.big_report(), self.out / "b", label="x")
        with self.assertRaises(Exception):
            freeze(self.big_report(), self.out / "b", label="x")

    def test_comparing_an_identical_candidate_shows_no_movement(self):
        report = self.big_report()
        baseline = freeze(report, self.out / "b", label="x")
        result = compare(baseline, report)
        self.assertTrue(result.unchanged)
        self.assertEqual(result.candidate_metrics_hash, baseline.metrics_hash)
        self.assertEqual(result.strategy_versions, ("v2.0", "v2.0"))

    def test_comparing_a_changed_candidate_reports_deltas(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = build_report(
            spread([win(3.0) if index % 3 else loss(-1.0)
                    for index in range(60)]),
            inputs=sample_inputs(), segment_intervals=False)
        result = compare(baseline, candidate)
        self.assertFalse(result.unchanged)
        deltas = {delta.name: delta for delta in result.deltas}
        self.assertGreater(deltas["expectancy"].delta, 0)
        self.assertAlmostEqual(deltas["win_rate"].delta, 0.0)
        json.loads(result.to_json())

    def test_a_different_snapshot_refuses_the_comparison(self):
        """C-5.7.2 / C-16.6.4: the engine MUST refuse."""
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = build_report(
            spread([win(2.0, data_snapshot_id="different0000000")
                    if index % 3 else loss(-1.0, data_snapshot_id="different0000000")
                    for index in range(60)]),
            inputs=sample_inputs(), segment_intervals=False)
        with self.assertRaises(IncomparableBaseline) as ctx:
            compare(baseline, candidate)
        self.assertIn("data_snapshot_id", str(ctx.exception))

    def test_a_different_window_refuses_the_comparison(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = self.big_report(
            inputs=ReportInputs(**{**sample_inputs().__dict__,
                                   "window_end": T0 + 999 * HOUR}))
        with self.assertRaises(IncomparableBaseline) as ctx:
            compare(baseline, candidate)
        self.assertIn("window_end", str(ctx.exception))

    def test_a_different_burn_in_refuses_the_comparison(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = self.big_report(
            inputs=ReportInputs(**{**sample_inputs().__dict__,
                                   "burn_in_h1": 336}))
        with self.assertRaises(IncomparableBaseline):
            compare(baseline, candidate)

    def test_a_different_universe_mode_refuses_the_comparison(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = self.big_report(
            inputs=ReportInputs(**{**sample_inputs().__dict__,
                                   "universe_mode": "static"}))
        with self.assertRaises(IncomparableBaseline):
            compare(baseline, candidate)

    def test_a_new_strategy_version_is_comparable_because_that_is_the_point(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = build_report(
            spread([win(2.0, strategy_version="v2.1") if index % 3
                    else loss(-1.0, strategy_version="v2.1")
                    for index in range(60)]),
            inputs=sample_inputs(), segment_intervals=False)
        result = compare(baseline, candidate)
        self.assertEqual(result.strategy_versions, ("v2.0", "v2.1"))

    def test_a_not_inferential_candidate_is_compared_but_noted(self):
        baseline = freeze(self.big_report(), self.out / "b", label="x")
        candidate = build_report(spread([win(2.0)] * 4), inputs=sample_inputs(),
                                 segment_intervals=False)
        result = compare(baseline, candidate)
        self.assertFalse(result.candidate_inferential)
        self.assertTrue(any("min_segment_n" in note for note in result.notes))

    def test_the_comparison_key_digest_is_stable(self):
        left = ComparisonKey.from_report(self.big_report())
        right = ComparisonKey.from_report(self.big_report())
        self.assertEqual(left.digest, right.digest)


# ==========================================================================
# The labeled dataset (§11 step 4)
# ==========================================================================

class TestLabeledDataset(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.run_dir = Path(self._tmp.name) / "run"
        self.run_dir.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def write_outcomes(self, records):
        path = self.run_dir / "outcomes.jsonl"
        path.write_text(
            "".join(json.dumps(r.to_dict(), sort_keys=True) + "\n"
                    for r in records), encoding="utf-8")

    def write_ledger(self, rows):
        path = self.run_dir / "signal_ledger.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8")

    def test_outcomes_round_trip_through_the_reader(self):
        records = spread([win(2.0), loss(-1.0), not_filled()])
        self.write_outcomes(records)
        self.assertEqual(read_outcomes(self.run_dir / "outcomes.jsonl"),
                         tuple(records))

    def test_an_unknown_outcome_field_is_refused_not_dropped(self):
        self.write_outcomes(spread([win()]))
        path = self.run_dir / "outcomes.jsonl"
        row = json.loads(path.read_text(encoding="utf-8").strip())
        row["a_new_field"] = 1
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(DatasetError) as ctx:
            read_outcomes(path)
        self.assertIn("unknown field", str(ctx.exception))

    def test_the_join_attaches_ledger_context(self):
        records = spread([win(2.0), loss(-1.0)])
        self.write_outcomes(records)
        self.write_ledger([
            {"run_id": r.run_id, "symbol": r.symbol,
             "h1_last_ts": float(r.h1_last_ts), "regime": "expansion_up",
             "sweep": True, "notes": ["a"], "decision": "VALID"}
            for r in records])
        dataset = LabeledDataset.from_run(self.run_dir)
        self.assertEqual(len(dataset), 2)
        self.assertEqual(dataset.joined, 2)
        self.assertEqual(dataset.unjoined, 0)
        self.assertEqual(dataset.join_rate, 1.0)
        self.assertEqual(dataset.labels_for(records[0])["regime"],
                         "expansion_up")

    def test_only_the_declared_label_fields_are_carried(self):
        records = spread([win()])
        self.write_outcomes(records)
        self.write_ledger([{
            "run_id": records[0].run_id, "symbol": records[0].symbol,
            "h1_last_ts": float(records[0].h1_last_ts),
            "regime": "rotational", "a_field_nobody_chose": 42,
        }])
        labels = LabeledDataset.from_run(self.run_dir).labels_for(records[0])
        self.assertIn("regime", labels)
        self.assertNotIn("a_field_nobody_chose", labels)
        self.assertEqual(set(labels), set(LABEL_FIELDS))

    def test_an_unjoined_outcome_is_counted_not_hidden(self):
        records = spread([win(), loss()])
        self.write_outcomes(records)
        self.write_ledger([{
            "run_id": records[0].run_id, "symbol": records[0].symbol,
            "h1_last_ts": float(records[0].h1_last_ts), "regime": "neutral"}])
        dataset = LabeledDataset.from_run(self.run_dir)
        self.assertEqual(dataset.joined, 1)
        self.assertEqual(dataset.unjoined, 1)
        self.assertEqual(dataset.labels_for(records[1]), {})

    def test_a_duplicated_canonical_row_is_refused(self):
        records = spread([win()])
        self.write_outcomes(records)
        row = {"run_id": records[0].run_id, "symbol": records[0].symbol,
               "h1_last_ts": float(records[0].h1_last_ts), "regime": "neutral"}
        self.write_ledger([row, row])
        with self.assertRaises(DatasetError) as ctx:
            LabeledDataset.from_run(self.run_dir)
        self.assertIn("canonical row", str(ctx.exception))

    def test_an_outcomes_only_dataset_is_allowed(self):
        self.write_outcomes(spread([win()]))
        with self.assertRaises(DatasetError):
            LabeledDataset.from_run(self.run_dir)
        dataset = LabeledDataset.from_run(self.run_dir, require_ledger=False)
        self.assertEqual(dataset.labels, {})

    def test_health_is_summed_across_cycles(self):
        (self.run_dir / "run_health.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in [
                {"processed": True, "universe_size": 3, "ledger_rows": 3,
                 "decisions": 3, "guard_skips": 0,
                 "swallowed_before_ledger": 0, "swallowed_after_ledger": 0,
                 "universe_error": None, "ttl_inferred": 1, "ttl_log_lines": 1},
                {"processed": True, "universe_size": 3, "ledger_rows": 2,
                 "decisions": 2, "guard_skips": 1,
                 "swallowed_before_ledger": 0, "swallowed_after_ledger": 0,
                 "universe_error": None, "ttl_inferred": 0, "ttl_log_lines": 0},
            ]), encoding="utf-8")
        health = HealthSummary.from_health_file(
            self.run_dir / "run_health.jsonl")
        self.assertEqual(health.cycles, 2)
        self.assertEqual(health.universe_total, 6)
        self.assertEqual(health.ledger_rows, 5)
        self.assertEqual(health.guard_skips, 1)
        self.assertAlmostEqual(health.coverage, 5 / 6)
        self.assertTrue(health.clean)

    def test_an_unclean_run_is_detected(self):
        (self.run_dir / "run_health.jsonl").write_text(
            json.dumps({"processed": True, "universe_size": 1,
                        "swallowed_after_ledger": 2}) + "\n", encoding="utf-8")
        health = HealthSummary.from_health_file(
            self.run_dir / "run_health.jsonl")
        self.assertFalse(health.clean)

    def test_a_missing_health_file_is_an_empty_summary(self):
        health = HealthSummary.from_health_file(self.run_dir / "nope.jsonl")
        self.assertEqual(health.cycles, 0)
        self.assertIsNone(health.coverage)

    def test_the_dataset_feeds_the_report_end_to_end(self):
        records = spread([win(2.0), loss(-1.0), not_filled()])
        self.write_outcomes(records)
        self.write_ledger([
            {"run_id": r.run_id, "symbol": r.symbol,
             "h1_last_ts": float(r.h1_last_ts), "regime": "expansion_up",
             "session_active": True, "sweep": True, "d1_bias": "BULLISH",
             "h4_bias": "BULLISH", "d1_rows": 119, "h1_rows": 499}
            for r in records])
        dataset = LabeledDataset.from_run(self.run_dir)
        report = build_report(
            dataset.records, labels=dataset.labels,
            health=dataset.health.to_dict(), dataset=dataset.to_dict(),
            inputs=sample_inputs(), segment_intervals=False)
        dimensions = report.segmentation.dimensions
        self.assertEqual(sorted(dimensions["regime"].segments), ["expansion_up"])
        self.assertEqual(sorted(dimensions["htf_alignment"].segments),
                         ["ALIGNED"])
        self.assertEqual(sorted(dimensions["sweep"].segments), ["true"])
        self.assertNotIn("regime", report.segmentation.unavailable)


# ==========================================================================
# Future compatibility (Task 9) and boundaries (Task 10)
# ==========================================================================

class TestBoundaries(unittest.TestCase):

    METRICS_DIR = Path("backtest") / "metrics"

    def test_live_sourced_outcomes_need_no_code_change(self):
        """Task 9: no replay-specific assumption."""
        records = spread([
            win(2.0, source="live", run_id="live-2026-07-30T00:00:00Z-123",
                data_snapshot_id="", cycle_index=None, universe_rank=None),
            loss(-1.0, source="live", run_id="live-2026-07-30T00:00:00Z-123",
                 data_snapshot_id="", cycle_index=None, universe_rank=None),
        ])
        metrics = compute_metrics(records)
        self.assertAlmostEqual(metrics.values["expectancy"], 0.5)
        report = build_report(records, inputs=sample_inputs(),
                              segment_intervals=False)
        self.assertEqual(report.lineage["sources"], ["live"])
        self.assertEqual(sorted(
            report.segmentation.dimensions["source"].segments), ["live"])

    def test_a_mixed_source_population_is_declared(self):
        """C-16.6.2: any aggregate mixing sources must say so."""
        records = spread([win(1.0, source="replay"), win(1.0, source="live")])
        report = build_report(records, inputs=sample_inputs(),
                              segment_intervals=False)
        self.assertTrue(report.lineage["mixed_source"])
        self.assertTrue(report.global_metrics.flags["mixed_sources"])

    def test_metrics_imports_nothing_from_replay_or_capture(self):
        from tests.backtest.test_historical_adapter import imported_modules
        for path in self.METRICS_DIR.glob("*.py"):
            for module in imported_modules(path):
                for forbidden in ("backtest.replay", "backtest.capture_schema",
                                  "backtest.acquire", "backtest.canonical",
                                  "v2.analysis", "v2.lifecycle", "v2.app",
                                  "v2.infra"):
                    self.assertFalse(module.startswith(forbidden),
                                     f"{path.name}: {module}")

    def test_metrics_depends_only_on_outcomes_and_the_repository_helper(self):
        """§10: `metrics -> data + v2.domain`."""
        from tests.backtest.test_historical_adapter import imported_modules
        allowed = {
            "__future__", "dataclasses", "typing", "json", "csv", "io", "math",
            "hashlib", "pathlib", "statistics", "datetime", "numpy",
            "backtest.data.repository",
            "backtest.outcome.record", "backtest.outcome.trade",
            "backtest.metrics.bootstrap", "backtest.metrics.core",
            "backtest.metrics.definitions", "backtest.metrics.equity",
            "backtest.metrics.report", "backtest.metrics.segments",
            "backtest.metrics.baseline",
        }
        for path in self.METRICS_DIR.glob("*.py"):
            for module in imported_modules(path):
                self.assertIn(module, allowed, f"{path.name}: {module}")

    def test_metrics_opens_no_file_for_reading(self):
        """It consumes records, it does not go looking for data. Writing the
        report is the only filesystem contact."""
        for path in self.METRICS_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for banned in ("read_text", "read_bytes", 'open(', "iterdir",
                           "glob("):
                if path.name == "baseline.py" and banned == "read_text":
                    continue          # load_baseline reads its own lock file
                self.assertNotIn(banned, source, f"{path.name}: {banned}")

    def test_metrics_reads_no_clock(self):
        """A wall-clock read would break byte-reproducibility. `generated_at` is an
        input the caller supplies, never a reading this engine takes."""
        from tests.backtest.test_historical_adapter import imported_modules
        for path in self.METRICS_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("datetime.now", source, path.name)
            self.assertNotIn("utcnow", source, path.name)
            self.assertNotIn("time.time", source, path.name)
            for module in imported_modules(path):
                self.assertNotIn(module, ("time", "random"), path.name)

    def test_the_only_randomness_is_the_seeded_generator(self):
        """§12 fixes the seed, so the one stochastic step is reproducible. Asserted
        on the AST, not on prose — every `np.random` use must be
        `default_rng(...)`, and nothing may touch the legacy global state."""
        import ast

        for path in self.METRICS_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                chain = []
                current: ast.expr = node
                while isinstance(current, ast.Attribute):
                    chain.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    chain.append(current.id)
                dotted = ".".join(reversed(chain))
                if "random" in dotted:
                    # `ast.walk` also yields the inner `np.random` of the full
                    # chain, so a prefix of the allowed name is allowed too.
                    self.assertIn(dotted,
                                  ("np.random", "np.random.default_rng"),
                                  f"{path.name}: {dotted}")

    def test_metrics_never_writes_to_a_replay_or_outcome_artifact(self):
        for path in self.METRICS_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for artifact in ("outcomes.jsonl", "signal_ledger.jsonl",
                             "lifecycle_events.jsonl", "run_health.jsonl",
                             "active_pois.json", "alerts.jsonl"):
                self.assertNotIn(artifact, source, f"{path.name}: {artifact}")

    def test_production_still_imports_nothing_from_backtest(self):
        from tests.backtest.test_historical_adapter import imported_modules
        for path in Path("v2").rglob("*.py"):
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest"),
                                 f"{path} imports {module}")


# ==========================================================================
# M11 — N2 closed: the report reads its comparison key from the data
# ==========================================================================

class TestLineageFromRecords(unittest.TestCase):
    """C-5.7.2's two unenforceable preconditions become data-enforced."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def report(self, records, **kw):
        inputs = ReportInputs(
            run_id="replay-test", window_start=T0, window_end=T0 + 42 * HOUR,
            label_fields=LABEL_FIELDS, **kw)
        return build_report(records, inputs=inputs, n_resamples=64)

    def stamped(self, n=40, **kw):
        return [win(2.0, burn_in_h1=336, universe_mode="volume", **kw)
                for _ in range(n)]

    def test_the_key_is_taken_from_the_records(self):
        report = self.report(self.stamped())
        self.assertEqual(report.lineage["burn_in_h1"], 336)
        self.assertEqual(report.lineage["universe_mode"], "volume")

    def test_the_caller_need_not_supply_what_the_data_declares(self):
        """The point of N2: no hand-typed comparison key."""
        key = ComparisonKey.from_report(self.report(self.stamped()))
        self.assertEqual(key.undeclared, ())
        self.assertEqual(key.burn_in_h1, 336)

    def test_a_caller_contradicting_the_data_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            self.report(self.stamped(), burn_in_h1=48)
        self.assertIn("C-5.7.2", str(caught.exception))

    def test_a_caller_agreeing_with_the_data_is_accepted(self):
        report = self.report(self.stamped(), burn_in_h1=336,
                             universe_mode="volume")
        self.assertEqual(report.lineage["burn_in_h1"], 336)

    def test_records_without_the_field_still_fall_back_to_the_caller(self):
        """Schema-1 outcomes remain reportable."""
        report = self.report([win(2.0) for _ in range(40)],
                             burn_in_h1=336, universe_mode="volume")
        self.assertEqual(report.lineage["burn_in_h1"], 336)
        self.assertFalse(report.lineage["mixed_burn_in_h1"])

    def test_a_mixed_population_declares_nothing_and_says_so(self):
        records = (self.stamped(20)
                   + [win(2.0, burn_in_h1=48, universe_mode="static")
                      for _ in range(20)])
        report = self.report(records)
        self.assertIsNone(report.lineage["burn_in_h1"])
        self.assertTrue(report.lineage["mixed_burn_in_h1"])
        self.assertTrue(report.lineage["mixed_universe_mode"])

    def test_a_mixed_population_cannot_be_frozen(self):
        records = (self.stamped(20)
                   + [win(2.0, burn_in_h1=48, universe_mode="static")
                      for _ in range(20)])
        with self.assertRaises(BaselineError):
            freeze(self.report(records), self.out / "b", label="mixed")

    def test_the_stamped_key_freezes_without_a_hand_typed_input(self):
        baseline = freeze(self.report(self.stamped()), self.out / "b", label="n2")
        self.assertEqual(baseline.key.burn_in_h1, 336)
        self.assertEqual(baseline.key.universe_mode, "volume")

    def test_two_burn_ins_are_refused_by_comparison(self):
        first = freeze(self.report(self.stamped()), self.out / "a", label="n2")
        other = self.report([win(2.0, burn_in_h1=48, universe_mode="volume")
                             for _ in range(40)])
        with self.assertRaises(IncomparableBaseline):
            compare(first, other)

    def test_the_lineage_does_not_enter_the_metrics_hash(self):
        """Metadata is not a measurement: the numbers are untouched."""
        bare = self.report([win(2.0) for _ in range(40)],
                           burn_in_h1=336, universe_mode="volume")
        self.assertEqual(self.report(self.stamped()).metrics_hash,
                         bare.metrics_hash)


if __name__ == "__main__":
    unittest.main()
