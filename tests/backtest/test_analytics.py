"""`backtest.analytics` — the Phase 1 research layer, and the registry
dimensions it presents.

The suite is built around one property: **analytics must never be a second
implementation of a metric.** Most of what follows exists to make a duplicated
calculation fail the build rather than survive review — `TestSharedMetrics`
compares every value analytics emits against the object it was copied from, and
the closure tests pin the derived series to the official equity curve.

Fixtures build a small, fully consistent run bundle by running the real Outcome
Engine population filter and the real metrics engine over synthetic outcome
records, so `metrics.json` is genuine output rather than a hand-written stub. A
stub would let analytics agree with a file that the engine would never produce.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from backtest.analytics import (
    ANALYTICS_JSON,
    ANALYTICS_MD,
    AnalyticsError,
    analyse_run,
)
from backtest.dataset import LEDGER_FILENAME, OUTCOMES_FILENAME, SignalPopulation
from backtest.metrics import METRICS_FILENAME, DIMENSIONS_BY_NAME, build_report
from backtest.metrics.report import ReportInputs
from backtest.metrics.segments import SESSIONS
from backtest.outcome.record import OutcomeRecord


def package_modules() -> list[Path]:
    """Every module of the analytics package, so a boundary check cannot be
    escaped by moving code into a new file."""
    return sorted((Path("backtest") / "analytics").glob("*.py"))


def package_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in package_modules())


RUN_ID = "replay-000000000000abcd"
SNAPSHOT = "c8542cb360d13207"
H1 = 3600
#: 2023-01-02T00:00:00Z — a Monday, so weekday/session buckets are predictable.
BASE = 1672617600


def _record(index: int, *, realized_r, state, boundary, symbol="AAAUSDT",
            direction="LONG", entry_mode="CONTINUATION", score=12,
            holding=4, shadow_kind=None, in_burn_in=False,
            exit_offset=None) -> OutcomeRecord:
    """One OutcomeRecord with every field the metrics engine reads."""
    filled = state != "NOT_FILLED"
    return OutcomeRecord(
        run_id=RUN_ID, source="replay",
        thesis_key=f"{symbol}|{direction}|{entry_mode}", symbol=symbol,
        direction=direction, entry_mode=entry_mode, boundary=boundary,
        h1_last_ts=boundary - H1,
        cycle_index=index, universe_rank=1 + (index % 3), alert_tier="A+",
        shadow_kind=shadow_kind, in_burn_in=in_burn_in,
        entry_price=100.0, sl=99.0, tp=102.0, structural_rr=2.0,
        tp_source="SWING_HIGH", entry_source="continuation_micro_pullback",
        score=score, filled=filled,
        fill_kind="LIMIT_TOUCH" if filled else None,
        fill_T=boundary + H1 if filled else None,
        fill_price=100.0 if filled else None,
        candles_to_fill=1 if filled else None,
        state=state, cancel_reason=None if filled else "TTL",
        exit_reason=("TP" if state == "WIN" else
                     "SL" if state == "LOSS" else None),
        exit_T=(boundary + (exit_offset if exit_offset is not None
                            else holding * H1)) if filled else None,
        exit_price=102.0 if state == "WIN" else 99.0,
        realized_r=realized_r,
        realized_r_net=realized_r,
        holding_candles=holding if filled else None,
        mae_r=-0.4, mfe_r=1.2, excursion_degraded=False,
        contract_version="1.1", strategy_version="2.2.0",
        ledger_schema_version=2, outcome_schema_version=2,
        outcome_config_hash="39761eb79ae5432e", data_snapshot_id=SNAPSHOT,
        env={"python": "3.13.5"}, burn_in_h1=0, universe_mode="volume",
    )


def _population() -> list[OutcomeRecord]:
    """A population that spans two months, two years, several sessions, both
    directions and both setups, with a genuine drawdown-and-recovery shape."""
    records: list[OutcomeRecord] = []
    index = 0

    # -- month 1: a losing run then a recovery, so an episode closes ------
    shape = [-1.0, -1.0, 2.0, -1.0, 3.0, -1.0, 1.5, 2.5]
    for offset, value in enumerate(shape):
        index += 1
        records.append(_record(
            index, realized_r=value, state="WIN" if value > 0 else "LOSS",
            boundary=BASE + offset * 6 * H1,
            direction="LONG" if offset % 2 else "SHORT",
            entry_mode="CONTINUATION" if offset % 3 else "RETRACEMENT",
            score=10 + (offset % 5), holding=2 + offset))

    # -- month 2 (and a year rollover): an open, unrecovered drawdown -----
    later = BASE + 340 * 24 * H1          # ≈ 2023-12-08, then into 2024
    for offset, value in enumerate([-1.0, -1.0, -1.0, 0.5]):
        index += 1
        records.append(_record(
            index, realized_r=value, state="WIN" if value > 0 else "LOSS",
            boundary=later + offset * 30 * 24 * H1,
            direction="SHORT", entry_mode="RETRACEMENT", score=14,
            holding=6, symbol="BBBUSDT"))

    # -- excluded populations, so the filters are exercised ---------------
    index += 1
    records.append(_record(index, realized_r=None, state="NOT_FILLED",
                           boundary=BASE + 9 * 6 * H1))
    index += 1
    records.append(_record(index, realized_r=9.0, state="WIN",
                           boundary=BASE + 10 * 6 * H1, shadow_kind="CAPPED"))
    index += 1
    records.append(_record(index, realized_r=-9.0, state="LOSS",
                           boundary=BASE + 11 * 6 * H1, in_burn_in=True))
    return records


def _ledger_row(record: OutcomeRecord, decision: str) -> dict:
    return {
        "schema_version": 2, "strategy_version": "2.2.0",
        "recorded_at": float(record.boundary + 1), "symbol": record.symbol,
        "h1_last_ts": float(record.boundary - H1), "decision": decision,
        "score": record.score, "entry_mode": record.entry_mode,
        "regime": "expansion_up", "session_active": True, "live_price": 100.0,
        "direction": record.direction, "entry_ready": True,
        "source": "replay", "run_id": RUN_ID, "cycle_index": record.cycle_index,
        "universe_rank": record.universe_rank, "contract_version": "1.1",
        "data_snapshot_id": SNAPSHOT,
    }


def build_bundle(root: Path) -> Path:
    """A run directory holding `outcomes.jsonl`, `signal_ledger.jsonl` and a
    real `metrics.json` produced by the metrics engine."""
    run_dir = root / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    records = _population()

    (run_dir / OUTCOMES_FILENAME).write_text(
        "".join(json.dumps(r.to_dict(), sort_keys=True) + "\n"
                for r in records), encoding="utf-8")

    # The ledger carries every analysed row, including decisions that never
    # became a trade — that asymmetry is the whole point of SignalPopulation.
    lines = [json.dumps(_ledger_row(r, "A+"), sort_keys=True) + "\n"
             for r in records]
    for extra, decision in enumerate(("SKIP", "SKIP", "WATCH", "VALID")):
        row = _ledger_row(records[0], decision)
        row["cycle_index"] = 900 + extra
        row["h1_last_ts"] = float(BASE + (900 + extra) * H1)
        row["entry_mode"] = "NONE" if decision == "SKIP" else "RETRACEMENT"
        row["score"] = 3 if decision == "SKIP" else 11
        lines.append(json.dumps(row, sort_keys=True) + "\n")
    (run_dir / LEDGER_FILENAME).write_text("".join(lines), encoding="utf-8")

    report = build_report(
        records, labels={}, health={"clean": True},
        dataset={"join_rate": 1.0},
        inputs=ReportInputs(run_id=RUN_ID, window_start=BASE,
                            window_end=records[-1].boundary,
                            burn_in_h1=0, universe_mode="volume"))
    report.write(run_dir)
    return run_dir


class AnalyticsCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls._root = Path(tempfile.mkdtemp(prefix="analytics-shared-"))
        cls._template = build_bundle(cls._root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._root, ignore_errors=True)

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="analytics-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run_dir = self.root / "runs" / RUN_ID
        shutil.copytree(self._template, self.run_dir)

    def analyse(self):
        return analyse_run(self.run_dir)

    def metrics(self) -> dict:
        return json.loads(
            (self.run_dir / METRICS_FILENAME).read_text(encoding="utf-8"))

    def run_cli(self, *argv: str) -> int:
        from backtest.cli import main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(["analytics", *argv])


# ==========================================================================
# Registry dimensions
# ==========================================================================

class TestRegistryDimensions(unittest.TestCase):
    """The four dimensions are first-class registry entries, so every metric is
    automatically available across them."""

    def test_all_four_are_registered(self):
        for name in ("month", "year", "quarter", "session"):
            self.assertIn(name, DIMENSIONS_BY_NAME, name)

    def test_they_partition_the_population(self):
        for name in ("month", "year", "quarter", "session"):
            self.assertTrue(DIMENSIONS_BY_NAME[name].partitions, name)

    def test_they_read_the_outcome_not_a_label(self):
        """Calendar and session come from `boundary`, which every record has —
        so they are never 'unavailable' the way a label dimension can be."""
        for name in ("month", "year", "quarter", "session"):
            self.assertEqual(DIMENSIONS_BY_NAME[name].source, "outcome", name)

    def test_keys_are_chronological_under_lexical_sort(self):
        stamps = [1672531200, 1690000000, 1700000000, 1790000000]
        for name, expected in (("month", True), ("quarter", True),
                               ("year", True)):
            keys = [DIMENSIONS_BY_NAME[name].keys_for(
                _record(1, realized_r=1.0, state="WIN", boundary=s), {})[0]
                for s in stamps]
            self.assertEqual(keys, sorted(keys), f"{name}: {keys}")

    def test_month_quarter_year_formats(self):
        # 2023-03-15T12:00:00Z
        record = _record(1, realized_r=1.0, state="WIN", boundary=1678881600)
        self.assertEqual(
            DIMENSIONS_BY_NAME["month"].keys_for(record, {}), ("2023-03",))
        self.assertEqual(
            DIMENSIONS_BY_NAME["quarter"].keys_for(record, {}), ("2023-Q1",))
        self.assertEqual(
            DIMENSIONS_BY_NAME["year"].keys_for(record, {}), ("2023",))

    def test_quarter_boundaries(self):
        for month, quarter in ((1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3),
                               (10, 4), (12, 4)):
            stamp = int(datetime(2024, month, 1, tzinfo=timezone.utc).timestamp())
            record = _record(1, realized_r=1.0, state="WIN", boundary=stamp)
            self.assertEqual(
                DIMENSIONS_BY_NAME["quarter"].keys_for(record, {}),
                (f"2024-Q{quarter}",), f"month {month}")

    def test_year_rollover(self):
        for stamp, expected in ((1704067199, "2023"), (1704067200, "2024")):
            record = _record(1, realized_r=1.0, state="WIN", boundary=stamp)
            self.assertEqual(
                DIMENSIONS_BY_NAME["year"].keys_for(record, {}), (expected,))

    def test_session_table_is_exhaustive_and_disjoint(self):
        covered: list[int] = []
        for _name, start, end in SESSIONS:
            covered.extend(range(start, end))
        self.assertEqual(sorted(covered), list(range(24)))
        self.assertEqual(len(covered), len(set(covered)))

    def test_every_hour_maps_to_exactly_one_session(self):
        midnight = 1672531200      # 2023-01-01T00:00:00Z
        seen = set()
        for hour in range(24):
            record = _record(1, realized_r=1.0, state="WIN",
                             boundary=midnight + hour * H1)
            keys = DIMENSIONS_BY_NAME["session"].keys_for(record, {})
            self.assertEqual(len(keys), 1, f"hour {hour}")
            seen.add(keys[0])
        self.assertEqual(seen, {name for name, _s, _e in SESSIONS})

    def test_session_edges_land_where_declared(self):
        midnight = 1672531200
        for hour, expected in ((0, "ASIA"), (6, "ASIA"), (7, "LONDON"),
                               (11, "LONDON"), (12, "LONDON_NY_OVERLAP"),
                               (15, "LONDON_NY_OVERLAP"), (16, "NEWYORK"),
                               (20, "NEWYORK"), (21, "OFF"), (23, "OFF")):
            record = _record(1, realized_r=1.0, state="WIN",
                             boundary=midnight + hour * H1)
            self.assertEqual(
                DIMENSIONS_BY_NAME["session"].keys_for(record, {}),
                (expected,), f"hour {hour}")

    def test_they_bucket_on_instantiation_not_exit(self):
        """The registry commits to `boundary`; a trade that exits in the next
        month still belongs to the month it was taken in."""
        record = _record(1, realized_r=1.0, state="WIN",
                         boundary=1677628800,       # 2023-03-01T00:00Z
                         exit_offset=40 * 24 * H1)  # exits in April
        self.assertEqual(
            DIMENSIONS_BY_NAME["month"].keys_for(record, {}), ("2023-03",))


# ==========================================================================
# The no-duplication guarantee
# ==========================================================================

class TestSharedMetrics(AnalyticsCase):

    #: (analytics block path, dimension name)
    BLOCKS = (
        (("calendar", "monthly"), "month"),
        (("calendar", "yearly"), "year"),
        (("calendar", "quarterly"), "quarter"),
        (("sessions", "by_session"), "session"),
        (("sessions", "by_hour"), "hour_of_day"),
        (("sessions", "by_weekday"), "weekday"),
        (("strategy", "by_setup"), "entry_mode"),
        (("strategy", "by_direction"), "direction"),
        (("strategy", "by_tier"), "alert_tier"),
        (("strategy", "by_score_band"), "score_band"),
    )

    def test_every_shared_value_is_identical_to_metrics_json(self):
        report = self.analyse().to_dict()
        metrics = self.metrics()
        source = metrics["segmentation"]["dimensions"]
        compared = 0
        for path, dimension in self.BLOCKS:
            block = report[path[0]][path[1]]
            self.assertEqual(block["source"], "official", path)
            self.assertEqual(block["dimension"], dimension)
            for row in block["segments"]:
                segment = source[dimension]["segments"][row["key"]]
                for field in ("win_rate", "expectancy", "profit_factor",
                              "payoff_ratio", "avg_holding_candles"):
                    self.assertEqual(segment["values"].get(field), row[field],
                                     f"{dimension}/{row['key']}/{field}")
                self.assertEqual(
                    segment["counts"].get("scored_trades", 0), row["n"])
                compared += 1
        self.assertGreater(compared, 10, "the comparison covered nothing")

    def test_avg_r_is_expectancy_and_not_a_second_computation(self):
        """`avg R` is what the engine already calls `expectancy`. Emitting it
        under a friendlier name is fine; computing it again is not."""
        report = self.analyse().to_dict()
        for path, _dimension in self.BLOCKS:
            for row in report[path[0]][path[1]]["segments"]:
                self.assertEqual(row["avg_r"], row["expectancy"])

    def test_official_equity_is_copied_verbatim(self):
        report = self.analyse().to_dict()
        official = report["equity"]["official"]
        source = self.metrics()["global"]["equity"]
        for field in ("final_r", "max_drawdown_r", "max_runup_r",
                      "max_consecutive_wins", "max_consecutive_losses",
                      "trades"):
            self.assertEqual(official[field], source[field], field)
        self.assertEqual(official["total_r"], source["final_r"])

    def test_the_assembly_module_defines_no_metric_formula(self):
        """`core.py` is the copier: it reads official values and re-emits them,
        so it must not contain a metric formula at all.

        `series.py` is exempt by design and by necessity — a *rolling* profit
        factor is a formula applied to a window the engine does not produce, so
        it cannot be copied from anywhere. The exemption is not taken on trust:
        `test_rolling_at_full_window_matches_the_official_metrics` below proves
        the windowed implementation agrees with the engine when the window is
        the whole population.
        """
        source = (Path("backtest") / "analytics" / "core.py").read_text(
            encoding="utf-8")
        for forbidden in ("def _expectancy", "def _win_rate",
                          "def _profit_factor", "def _payoff",
                          "def _max_drawdown", "def _streak",
                          "gross_profit /", "avg_win /"):
            self.assertNotIn(forbidden, source)

    def test_rolling_at_full_window_matches_the_official_metrics(self):
        """The one place analytics recomputes a formula, checked against the
        engine rather than trusted.

        With the window set to the whole population a rolling statistic must
        reproduce the global metric exactly. If it does not, the windowed
        implementation and the engine disagree about what the quantity means,
        which is precisely the drift this suite exists to prevent.
        """
        from backtest.analytics.series import rolling_statistics
        from backtest.metrics import Populations, c98_order_key
        from backtest.dataset import read_outcomes

        records = read_outcomes(self.run_dir / OUTCOMES_FILENAME)
        scored = sorted(
            [r for r in Populations.of(records).scored
             if r.realized_r is not None], key=c98_order_key)
        rolling = rolling_statistics(scored, windows=(len(scored),), stride=1)
        point = rolling["windows"][str(len(scored))]["series"][-1]

        official = self.metrics()["global"]["values"]
        self.assertAlmostEqual(point["expectancy"], official["expectancy"],
                               places=9)
        self.assertAlmostEqual(point["win_rate"], official["win_rate"],
                               places=9)
        self.assertAlmostEqual(point["profit_factor"],
                               official["profit_factor"], places=9)


# ==========================================================================
# Closure — derived series must reconcile with official values
# ==========================================================================

class TestClosure(AnalyticsCase):

    def test_monthly_net_r_sums_to_final_r(self):
        report = self.analyse().to_dict()
        final_r = report["equity"]["official"]["final_r"]
        monthly = report["calendar"]["equity_calendar"]["monthly"]
        self.assertAlmostEqual(sum(row["net_r"] for row in monthly), final_r,
                               places=9)
        self.assertAlmostEqual(
            report["calendar"]["equity_calendar"]["net_r_total"], final_r,
            places=9)

    def test_yearly_net_r_sums_to_final_r(self):
        report = self.analyse().to_dict()
        yearly = report["calendar"]["equity_calendar"]["yearly"]
        self.assertAlmostEqual(sum(row["net_r"] for row in yearly),
                               report["equity"]["official"]["final_r"],
                               places=9)

    def test_setup_direction_closes_on_count_and_r(self):
        report = self.analyse().to_dict()
        cells = report["strategy"]["by_setup_direction"]["cells"]
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        self.assertEqual(sum(cell["n"] for cell in cells), scored)
        self.assertAlmostEqual(sum(cell["net_r"] for cell in cells),
                               report["equity"]["official"]["final_r"],
                               places=9)

    def test_session_totals_close_against_the_scored_population(self):
        report = self.analyse().to_dict()
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        for block in ("by_session", "by_hour", "by_weekday"):
            segments = report["sessions"][block]["segments"]
            self.assertEqual(sum(row["n"] for row in segments), scored, block)

    def test_calendar_dimensions_close_against_the_scored_population(self):
        report = self.analyse().to_dict()
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        for block in ("monthly", "yearly", "quarterly"):
            segments = report["calendar"][block]["segments"]
            self.assertEqual(sum(row["n"] for row in segments), scored, block)

    def test_deepest_episode_equals_the_official_max_drawdown(self):
        report = self.analyse().to_dict()
        self.assertAlmostEqual(
            report["equity"]["drawdown_episodes"]["deepest_r"],
            report["equity"]["official"]["max_drawdown_r"], places=6)


# ==========================================================================
# Derived blocks
# ==========================================================================

class TestDerivedBlocks(AnalyticsCase):

    def test_drawdown_episodes_record_recovery(self):
        episodes = self.analyse().to_dict()["equity"]["drawdown_episodes"]
        self.assertGreater(episodes["episodes_total"], 0)
        for episode in episodes["episodes"]:
            self.assertLessEqual(episode["trough_r"], episode["peak_r"])
            self.assertGreaterEqual(episode["depth_r"], 0.0)
            if episode["recovered"]:
                self.assertIsNotNone(episode["end_exit_T"])
                self.assertIsNotNone(episode["recovery_seconds"])
            else:
                self.assertIsNone(episode["end_exit_T"])
                self.assertIsNone(episode["recovery_seconds"])

    def test_an_unrecovered_tail_is_reported_as_such(self):
        report = self.analyse().to_dict()
        self.assertGreaterEqual(
            report["equity"]["recovery"]["unrecovered_episodes"], 1)

    def test_consistency_counts_positive_months(self):
        consistency = self.analyse().to_dict()["calendar"]["consistency"]
        monthly = self.analyse().to_dict()["calendar"]["equity_calendar"]["monthly"]
        self.assertEqual(consistency["months_total"], len(monthly))
        self.assertEqual(consistency["months_positive"],
                         sum(1 for row in monthly if row["net_r"] > 0))
        self.assertEqual(consistency["source"], "derived")

    def test_the_funnel_reads_both_artifacts(self):
        funnel = self.analyse().to_dict()["signal_funnel"]
        stages = {stage["stage"]: stage for stage in funnel["stages"]}
        population = SignalPopulation.from_run(self.run_dir)
        self.assertEqual(stages["analysed"]["n"], population.ledger_rows)
        self.assertEqual(stages["scored"]["n"],
                         self.metrics()["global"]["counts"]["scored_trades"])
        self.assertIn("SKIP", funnel["by_decision"])
        self.assertIn("WATCH", funnel["by_decision"])

    def test_signal_population_counts_decisions_the_metrics_engine_cannot_see(self):
        population = SignalPopulation.from_run(self.run_dir)
        self.assertEqual(population.by_decision.get("SKIP"), 2)
        self.assertEqual(population.by_decision.get("WATCH"), 1)
        self.assertEqual(population.by_decision.get("VALID"), 1)
        self.assertEqual(population.actionable,
                         population.by_decision.get("VALID", 0)
                         + population.by_decision.get("A+", 0))

    def test_signal_population_refuses_a_bundle_with_no_ledger(self):
        from backtest.dataset import DatasetError

        (self.run_dir / LEDGER_FILENAME).unlink()
        with self.assertRaises(DatasetError):
            SignalPopulation.from_run(self.run_dir)

    def test_every_block_declares_its_provenance(self):
        report = self.analyse().to_dict()
        checked = 0
        for group in ("populations", "sessions", "strategy", "signal_funnel",
                      "calendar", "equity", "identity", "caveats"):
            block = report[group]
            if "source" in block:
                self.assertIn(block["source"], ("official", "derived"), group)
                checked += 1
                continue
            for name, inner in block.items():
                if isinstance(inner, dict) and "source" in inner:
                    self.assertIn(inner["source"], ("official", "derived"),
                                  f"{group}.{name}")
                    checked += 1
        self.assertGreater(checked, 12)


# ==========================================================================
# WATCH_ONLY
# ==========================================================================

class TestWatchOnly(AnalyticsCase):

    def test_watch_only_is_reported_as_absent_not_omitted(self):
        absent = self.analyse().to_dict()["strategy"]["by_setup"]["modes_absent"]
        self.assertIn("WATCH_ONLY", absent)
        entry = absent["WATCH_ONLY"]
        self.assertEqual(entry["n"], 0)
        self.assertEqual(entry["ledger_rows"], 0)
        self.assertTrue(entry["unreachable_in_bundle"])

    def test_a_mode_in_the_ledger_but_not_in_trades_is_distinguished(self):
        """`NONE` appears in the ledger and never becomes a trade. That is a
        different fact from `WATCH_ONLY`, which the strategy never emitted at
        all, and the two must not collapse into one 'n=0'."""
        absent = self.analyse().to_dict()["strategy"]["by_setup"]["modes_absent"]
        self.assertIn("NONE", absent)
        self.assertGreater(absent["NONE"]["ledger_rows"], 0)
        self.assertFalse(absent["NONE"]["unreachable_in_bundle"])
        self.assertTrue(absent["WATCH_ONLY"]["unreachable_in_bundle"])

    def test_a_mode_that_does_trade_is_not_listed_as_absent(self):
        absent = self.analyse().to_dict()["strategy"]["by_setup"]["modes_absent"]
        self.assertNotIn("CONTINUATION", absent)


# ==========================================================================
# Preconditions
# ==========================================================================

class TestPreconditions(AnalyticsCase):

    def test_missing_metrics_json_names_the_command_to_run(self):
        (self.run_dir / METRICS_FILENAME).unlink()
        with self.assertRaises(AnalyticsError) as caught:
            self.analyse()
        message = str(caught.exception)
        self.assertIn(METRICS_FILENAME, message)
        self.assertIn("backtest.cli metrics", message)

    def test_missing_outcomes_names_the_command_to_run(self):
        (self.run_dir / OUTCOMES_FILENAME).unlink()
        with self.assertRaises(AnalyticsError) as caught:
            self.analyse()
        message = str(caught.exception)
        self.assertIn(OUTCOMES_FILENAME, message)
        self.assertIn("backtest.cli outcome", message)

    def test_missing_ledger_is_an_analytics_error(self):
        (self.run_dir / LEDGER_FILENAME).unlink()
        with self.assertRaises(AnalyticsError):
            self.analyse()

    def test_a_directory_that_is_not_a_run_is_refused(self):
        empty = self.root / "nothing"
        empty.mkdir()
        with self.assertRaises(AnalyticsError):
            analyse_run(empty)

    def test_analytics_never_runs_replay_metrics_or_outcome(self):
        """The three expensive stages must not be invoked as a side effect."""
        source = package_source()
        for forbidden in ("build_report(", "ReplayDriver", "OutcomeEngine",
                          "build_trade_intents", "open_corpus"):
            self.assertNotIn(forbidden, source)


# ==========================================================================
# Determinism and non-mutation
# ==========================================================================

class TestDeterminismAndIsolation(AnalyticsCase):

    def test_repeated_runs_are_identical_except_generated_at(self):
        first = self.analyse().to_dict()
        second = self.analyse().to_dict()
        first["identity"].pop("generated_at")
        second["identity"].pop("generated_at")
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_analytics_does_not_mutate_the_bundle(self):
        before = {path.name: path.read_bytes()
                  for path in self.run_dir.iterdir() if path.is_file()}
        self.run_cli("--run", str(self.run_dir))
        after = {path.name: path.read_bytes()
                 for path in self.run_dir.iterdir() if path.is_file()}
        self.assertEqual(sorted(before), sorted(after),
                         "analytics added or removed a bundle file")
        for name, blob in before.items():
            self.assertEqual(after[name], blob, name)

    def test_it_does_not_overwrite_the_metrics_artifacts(self):
        for name in (METRICS_FILENAME, "report.md", "segments.csv"):
            path = self.run_dir / name
            if not path.exists():
                continue
            before = path.read_bytes()
            self.run_cli("--run", str(self.run_dir))
            self.assertEqual(path.read_bytes(), before, name)

    def test_output_lands_in_its_own_directory(self):
        self.run_cli("--run", str(self.run_dir))
        out = self.run_dir / "analytics"
        self.assertTrue((out / ANALYTICS_JSON).exists())
        self.assertTrue((out / ANALYTICS_MD).exists())

    def test_out_overrides_the_destination(self):
        elsewhere = self.root / "somewhere"
        self.run_cli("--run", str(self.run_dir), "--out", str(elsewhere))
        self.assertTrue((elsewhere / ANALYTICS_JSON).exists())
        self.assertFalse((self.run_dir / "analytics").exists())

    def test_the_command_exits_zero(self):
        self.assertEqual(self.run_cli("--run", str(self.run_dir)), 0)

    def test_the_markdown_renders_every_required_section(self):
        self.run_cli("--run", str(self.run_dir))
        text = (self.run_dir / "analytics" / ANALYTICS_MD).read_text(
            encoding="utf-8")
        for heading in ("# Phase 1 Analytics", "## A · Equity",
                        "## B · Calendar", "## C · Sessions",
                        "## D · Strategy breakdown", "## Signal funnel",
                        "## Caveats"):
            self.assertIn(heading, text)

    def test_the_report_carries_the_bundle_identity(self):
        report = self.analyse().to_dict()
        self.assertEqual(report["identity"]["run_id"], RUN_ID)
        self.assertEqual(report["identity"]["metrics_hash"],
                         self.metrics()["metrics_hash"])
        self.assertEqual(report["identity"]["data_snapshot_id"], SNAPSHOT)


# ==========================================================================
# Architecture
# ==========================================================================

class TestAnalyticsBoundaries(unittest.TestCase):

    def test_it_never_imports_the_replay_package(self):
        from tests.backtest.test_historical_adapter import imported_modules

        for path in package_modules():
            for module in imported_modules(path):
                self.assertFalse(module.startswith("backtest.replay"),
                                 f"{path} imports {module}")

    def test_it_names_no_v2_module(self):
        """`v2` still loads transitively — `backtest/outcome/__init__.py` imports
        the engine — so the enforceable rule is that no strategy module is named
        here. Same rule `backtest/audit.py` follows."""
        from tests.backtest.test_historical_adapter import imported_modules

        for path in package_modules():
            for module in imported_modules(path):
                self.assertFalse(module.startswith("v2"),
                                 f"{path} imports {module}")

    def test_it_defines_no_strategy_logic(self):
        source = package_source()
        for forbidden in ("def score", "def reconcile", "def analyze",
                          "miss_count"):
            self.assertNotIn(forbidden, source)

    def test_the_ledger_reader_stays_in_dataset(self):
        """`backtest.dataset` is documented as the only place in the evaluation
        stack that opens the signal ledger. Analytics must not become a second.

        Checked structurally rather than by grepping the text: the module names
        `signal_ledger.jsonl` in prose and in a provenance label, and neither is
        file access.
        """
        import ast

        imported: set[str] = set()
        opens = 0
        nodes = [n for path in package_modules()
                 for n in ast.walk(ast.parse(
                     path.read_text(encoding="utf-8")))]
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backtest.dataset"):
                    imported.update(alias.name for alias in node.names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    opens += 1
        self.assertNotIn("LEDGER_FILENAME", imported)
        self.assertEqual(opens, 0, "analytics opened a file handle directly")


# ==========================================================================
# Phase 2 — statistical primitives
# ==========================================================================

class TestStatistics(unittest.TestCase):

    def test_the_normal_quantile_is_accurate(self):
        from backtest.analytics.stats import _normal_quantile

        for p, expected in ((0.975, 1.959964), (0.995, 2.575829),
                            (0.5, 0.0), (0.025, -1.959964)):
            self.assertAlmostEqual(_normal_quantile(p), expected, places=5)

    def test_the_critical_value_follows_the_engine_confidence_band(self):
        from backtest.analytics.stats import _z_for

        self.assertAlmostEqual(_z_for((2.5, 97.5)), 1.959964, places=5)

    def test_a_real_difference_is_detected(self):
        from backtest.analytics.stats import compare_means

        left = [1.0] * 200 + [1.2] * 200
        right = [0.0] * 200 + [0.2] * 200
        result = compare_means("t", "q", "L", "R", left, right)
        self.assertAlmostEqual(result.effect, 1.0, places=6)
        self.assertTrue(result.welch_ci[0] > 0)
        self.assertTrue(result.bootstrap_ci[0] > 0)
        self.assertTrue(result.methods_agree)
        self.assertLess(result.p_value, 1e-6)

    def test_no_difference_is_not_detected(self):
        from backtest.analytics.stats import compare_means

        sample = [float(v % 7) - 3.0 for v in range(600)]
        result = compare_means("t", "q", "L", "R", sample, list(sample))
        self.assertAlmostEqual(result.effect, 0.0, places=9)
        self.assertLessEqual(result.welch_ci[0], 0.0)
        self.assertGreaterEqual(result.welch_ci[1], 0.0)
        self.assertGreater(result.p_value, 0.5)

    def test_a_small_sample_is_not_inferential(self):
        from backtest.analytics.stats import compare_means

        result = compare_means("t", "q", "L", "R", [1.0, 2.0], [0.0, 1.0])
        self.assertFalse(result.inferential)
        self.assertIsNone(result.significant)

    def test_the_bootstrap_is_seeded_and_reproducible(self):
        from backtest.analytics.stats import compare_means

        left = [float((v * 37) % 11) - 5 for v in range(500)]
        right = [float((v * 17) % 11) - 5.5 for v in range(500)]
        first = compare_means("t", "q", "L", "R", left, right)
        second = compare_means("t", "q", "L", "R", left, right)
        self.assertEqual(first.bootstrap_ci, second.bootstrap_ci)

    def test_holm_is_monotone_and_bounded(self):
        from backtest.analytics.stats import holm_bonferroni

        raw = [0.001, 0.01, 0.04, 0.2, 0.9]
        adjusted = holm_bonferroni(raw)
        self.assertEqual(len(adjusted), len(raw))
        for value, original in zip(adjusted, raw):
            self.assertGreaterEqual(value, original)
            self.assertLessEqual(value, 1.0)
        self.assertEqual(adjusted, sorted(adjusted))

    def test_holm_ignores_uncomputable_tests(self):
        from backtest.analytics.stats import holm_bonferroni

        adjusted = holm_bonferroni([0.01, None, 0.02])
        self.assertIsNone(adjusted[1])
        # Family size 2, not 3: a test that never ran must not penalise one
        # that did.
        self.assertAlmostEqual(adjusted[0], 0.02, places=9)

    def test_cohens_d_has_the_expected_sign_and_scale(self):
        from backtest.analytics.stats import cohens_d

        left = [1.0, 1.0, 1.0, 1.0, 2.0]
        right = [0.0, 0.0, 0.0, 0.0, 1.0]
        self.assertGreater(cohens_d(left, right), 0)
        self.assertLess(cohens_d(right, left), 0)
        self.assertIsNone(cohens_d([1.0], [0.0]))


# ==========================================================================
# Phase 2 — hypotheses
# ==========================================================================

class TestHypotheses(AnalyticsCase):

    def test_the_family_is_declared_in_code_not_derived_from_data(self):
        from backtest.analytics.research import HYPOTHESES

        names = [h.name for h in HYPOTHESES]
        self.assertEqual(len(names), len(set(names)), "duplicate hypothesis")
        research = self.analyse().to_dict()["research"]
        self.assertEqual(research["method"]["family_size"], len(HYPOTHESES))
        self.assertTrue(research["method"]["declared_in_advance"])
        self.assertEqual([r["name"] for r in research["results"]], names)

    def test_every_result_carries_the_required_evidence(self):
        for result in self.analyse().to_dict()["research"]["results"]:
            for metric in ("expectancy", "win_rate"):
                block = result[metric]
                for field in ("left", "right", "effect", "p_value",
                              "p_adjusted", "inferential", "interpretation"):
                    self.assertIn(field, block, f"{result['name']}/{metric}")
                self.assertIn("n", block["left"])
                self.assertIn("n", block["right"])
                self.assertTrue(block["interpretation"])

    def test_significance_requires_correction_and_agreement(self):
        for result in self.analyse().to_dict()["research"]["results"]:
            block = result["expectancy"]
            if block["significant"]:
                self.assertLess(block["p_adjusted"], 0.05)
                self.assertTrue(block["methods_agree"])
                self.assertTrue(block["inferential"])

    def test_an_uninferential_comparison_never_claims_significance(self):
        for result in self.analyse().to_dict()["research"]["results"]:
            for metric in ("expectancy", "win_rate"):
                block = result[metric]
                if not block["inferential"]:
                    self.assertIn(block["significant"], (None, False))

    def test_group_sizes_never_exceed_the_population(self):
        report = self.analyse().to_dict()
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        for result in report["research"]["results"]:
            block = result["expectancy"]
            self.assertLessEqual(block["left"]["n"] + block["right"]["n"],
                                 scored, result["name"])

    def test_the_session_predicate_matches_the_registry(self):
        """`research` reproduces the session bucketing from a timestamp. It must
        agree with the dimension, or two parts of one report would disagree."""
        from backtest.analytics.research import _session

        midnight = 1672531200
        for hour in range(24):
            record = _record(1, realized_r=1.0, state="WIN",
                             boundary=midnight + hour * H1)
            self.assertEqual(
                _session(record),
                DIMENSIONS_BY_NAME["session"].keys_for(record, {})[0],
                f"hour {hour}")


# ==========================================================================
# Phase 2 — aggregation correctness
# ==========================================================================

class TestAggregation(AnalyticsCase):

    def test_r_distribution_bands_cover_every_trade_once(self):
        report = self.analyse().to_dict()
        block = report["distributions"]["r_multiple"]
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        self.assertEqual(sum(band["n"] for band in block["bands"]), scored)
        self.assertAlmostEqual(sum(band["sum_r"] for band in block["bands"]),
                               report["equity"]["official"]["final_r"],
                               places=9)
        self.assertEqual(block["summary"]["n"], scored)

    def test_duration_bands_cover_every_filled_trade_once(self):
        block = self.analyse().to_dict()["distributions"]["duration"]
        self.assertEqual(sum(band["n"] for band in block["bands"]),
                         block["summary"]["n"])

    def test_streak_lengths_sum_to_the_population(self):
        report = self.analyse().to_dict()
        streaks = report["distributions"]["streaks"]
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        total = 0
        for side in ("wins", "losses"):
            block = streaks[side]
            self.assertGreaterEqual(block["longest"], 0)
            total += (block["mean_length"] or 0) * block["runs"]
        self.assertAlmostEqual(total, scored, places=6)

    def test_the_longest_streaks_match_the_official_maxima(self):
        report = self.analyse().to_dict()
        streaks = report["distributions"]["streaks"]
        official = report["equity"]["official"]
        self.assertEqual(streaks["wins"]["longest"],
                         official["max_consecutive_wins"])
        self.assertEqual(streaks["losses"]["longest"],
                         official["max_consecutive_losses"])

    def test_outcome_groups_sum_to_their_source_dimension(self):
        report = self.analyse().to_dict()
        outcomes = report["outcomes"]
        by_exit = {row["key"]: row["n"] for row in outcomes["by_exit_reason"]}
        by_cancel = {row["key"]: row["n"]
                     for row in outcomes["by_cancel_reason"]}
        for group in outcomes["grouped"]:
            source = by_exit if group["dimension"] == "exit_reason" else by_cancel
            expected = sum(source.get(key, 0) for key in group["members"])
            self.assertEqual(group["n"], expected, group["outcome"])

    def test_partial_refresh_is_declared_unavailable_not_invented(self):
        outcomes = self.analyse().to_dict()["outcomes"]
        self.assertIn("PARTIAL_REFRESH", outcomes["unavailable"])
        labels = {group["outcome"] for group in outcomes["grouped"]}
        self.assertNotIn("PARTIAL_REFRESH", labels)

    def test_contributors_net_r_equals_final_r(self):
        report = self.analyse().to_dict()
        self.assertAlmostEqual(report["symbols"]["contributors"]["net_r"],
                               report["equity"]["official"]["final_r"],
                               places=6)

    def test_the_leaderboard_respects_the_sample_floor(self):
        from backtest.analytics.series import LEADERBOARD_MIN_N

        board = self.analyse().to_dict()["symbols"]["leaderboard"]
        for key in ("best_by_expectancy_qualified",
                    "worst_by_expectancy_qualified"):
            for row in board[key]:
                self.assertGreaterEqual(row["n"], LEADERBOARD_MIN_N, key)

    def test_drawdown_summary_agrees_with_the_episode_table(self):
        episodes = self.analyse().to_dict()["equity"]["drawdown_episodes"]
        summary = episodes["summary"]
        self.assertEqual(summary["episodes_total"], len(episodes["all"]))
        if episodes["all"]:
            self.assertAlmostEqual(
                summary["depth_r"]["max"], episodes["deepest_r"], places=9)

    def test_rolling_windows_are_ordered_and_end_at_the_last_trade(self):
        report = self.analyse().to_dict()
        scored = self.metrics()["global"]["counts"]["scored_trades"]
        for block in report["rolling"]["windows"].values():
            series = block.get("series")
            if not series:
                continue
            indices = [point["index"] for point in series]
            self.assertEqual(indices, sorted(indices))
            self.assertEqual(indices[-1], scored)
            self.assertGreaterEqual(indices[0], block["window"])


# ==========================================================================
# Phase 2 — CSV exports
# ==========================================================================

class TestCsvExports(AnalyticsCase):

    def charts(self) -> Path:
        return self.run_dir / "analytics" / "charts"

    def test_every_declared_csv_is_written(self):
        from backtest.analytics import EXPORTS

        self.run_cli("--run", str(self.run_dir))
        for name in EXPORTS:
            path = self.charts() / name
            self.assertTrue(path.exists(), name)
            self.assertTrue(path.read_text(encoding="utf-8").strip(), name)

    def test_no_images_are_produced(self):
        self.run_cli("--run", str(self.run_dir))
        for path in (self.run_dir / "analytics").rglob("*"):
            if path.is_file():
                self.assertIn(path.suffix, (".json", ".md", ".csv"),
                              f"unexpected artifact {path.name}")

    def test_every_csv_carries_its_provenance(self):
        self.run_cli("--run", str(self.run_dir))
        metrics_hash = self.metrics()["metrics_hash"]
        for path in self.charts().glob("*.csv"):
            head = path.read_text(encoding="utf-8").splitlines()[:2]
            self.assertTrue(head[0].startswith("# run_id="), path.name)
            self.assertEqual(head[1], f"# metrics_hash={metrics_hash}",
                             path.name)

    def test_csvs_are_byte_identical_across_runs(self):
        self.run_cli("--run", str(self.run_dir))
        first = {p.name: p.read_bytes() for p in self.charts().glob("*.csv")}
        self.run_cli("--run", str(self.run_dir))
        second = {p.name: p.read_bytes() for p in self.charts().glob("*.csv")}
        self.assertEqual(sorted(first), sorted(second))
        for name, blob in first.items():
            self.assertEqual(second[name], blob, name)

    def test_equity_csv_matches_the_official_curve(self):
        self.run_cli("--run", str(self.run_dir))
        import csv

        rows = list(csv.reader(
            line for line in
            (self.charts() / "equity.csv").read_text(encoding="utf-8")
            .splitlines() if not line.startswith("#")))
        series = self.metrics()["global"]["equity"]["series"]
        self.assertEqual(len(rows) - 1, len(series))
        self.assertAlmostEqual(float(rows[-1][2]),
                               self.metrics()["global"]["equity"]["final_r"],
                               places=9)

    def test_hypotheses_csv_has_a_row_per_question_and_metric(self):
        from backtest.analytics import HYPOTHESES

        self.run_cli("--run", str(self.run_dir))
        lines = [line for line in
                 (self.charts() / "hypotheses.csv").read_text(encoding="utf-8")
                 .splitlines() if not line.startswith("#")]
        self.assertEqual(len(lines) - 1, len(HYPOTHESES) * 2)

    def test_csvs_land_only_under_the_output_directory(self):
        before = sorted(p.name for p in self.run_dir.iterdir())
        self.run_cli("--run", str(self.run_dir))
        after = sorted(p.name for p in self.run_dir.iterdir())
        self.assertEqual(set(after) - set(before), {"analytics"})


if __name__ == "__main__":
    unittest.main()
