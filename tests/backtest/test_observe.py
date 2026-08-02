"""Operational observability — `backtest/observe.py`.

The properties that matter are not "does it print something". They are:

* nothing it writes can leak strategy performance to a watching operator;
* a percentage is never invented for un-enumerated work;
* a stalled job is distinguishable from a dead one;
* a crashed job leaves a traceback and its configuration behind;
* a crashed run bundle is preserved, never overwritten.

Time is injected everywhere so the suite is instant and deterministic.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backtest.observe import (
    FORBIDDEN_FIELDS,
    Heartbeat,
    JobRecorder,
    OperationalLog,
    PerformanceLeak,
    Progress,
    assert_operational,
    cpu_fraction,
    human_duration,
    preserve_failed_run,
    rss_bytes,
)


class FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _collector():
    lines: list[str] = []
    return lines, lines.append


# ---------------------------------------------------------------------------


class TestPerformanceBlindness(unittest.TestCase):
    """Counts are permitted; ratios and R-denominated quantities are not."""

    def test_every_forbidden_name_is_rejected(self):
        for name in FORBIDDEN_FIELDS:
            with self.subTest(field=name):
                with self.assertRaises(PerformanceLeak):
                    assert_operational({name: 1})

    def test_rejection_is_by_substring_not_exact_match(self):
        # `current_win_rate` and `win_rate_so_far` are the same mistake wearing
        # different names; an exact-match guard would miss both.
        for name in ("current_win_rate", "win_rate_so_far", "running_expectancy",
                     "PROFIT_FACTOR", "cum_max_drawdown_r"):
            with self.subTest(field=name):
                with self.assertRaises(PerformanceLeak):
                    assert_operational({name: 0.5})

    def test_r_denominated_suffix_is_rejected(self):
        # `avg_excursion_r` trips only the suffix rule; no other banned token
        # appears in it.
        with self.assertRaises(PerformanceLeak):
            assert_operational({"avg_excursion_r": 1.2})

    def test_the_suffix_rule_does_not_reject_throughput_counts(self):
        # `_r` matched anywhere would reject these, which is the whole reason
        # the R rule is a suffix and not a substring.
        assert_operational({"ledger_rows": 900, "bytes_read": 12, "run_root": "x"})

    def test_throughput_counts_are_permitted(self):
        # These say the machine is working; none says how the strategy is doing.
        assert_operational({
            "completed": 10, "total": 100, "cycles": 5, "ledger_rows": 900,
            "alerts": 3, "retries": 2, "bytes_done": 1024, "rss_bytes": 1,
            "symbols": 30, "elapsed_seconds": 12.5,
        })

    def test_a_heartbeat_cannot_be_serialised_with_a_leak(self):
        # The guard runs inside `to_dict`, so it protects the log sink too.
        beat = Heartbeat(
            sequence=1, at="2026-08-01T00:00:00Z", phase="p", unit="u",
            completed=1, total=2, elapsed_seconds=1.0, rate_trailing=1.0,
            rate_cumulative=1.0, eta_seconds=1.0, stalled=False)
        self.assertNotIn("win_rate", beat.to_dict())

    def test_the_log_sink_refuses_a_leaking_record(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            with self.assertRaises(PerformanceLeak):
                log.append({"kind": "event", "expectancy": 0.3})


class TestHonestProgress(unittest.TestCase):

    def test_no_percentage_without_an_enumerated_total(self):
        clock = FakeClock()
        progress = Progress(total=None, sink=None, clock=clock)
        progress.advance(5)
        self.assertIsNone(progress.snapshot().percent)

    def test_percentage_is_reported_when_work_was_enumerated(self):
        clock = FakeClock()
        progress = Progress(total=200, sink=None, clock=clock)
        progress.advance(50)
        self.assertEqual(progress.snapshot().percent, 25.0)

    def test_trailing_and_cumulative_rates_are_both_reported(self):
        # Their divergence is the diagnostic: a job that starts fast and slows
        # must show it, and a single blended number would hide it.
        clock = FakeClock()
        progress = Progress(total=1000, sink=None, clock=clock, window=4)
        for _ in range(10):                 # fast phase: 10 units/s
            clock.tick(0.1)
            progress.advance(1)
        for _ in range(3):                  # slow phase: 1 unit/s
            clock.tick(1.0)
            progress.advance(1)
        beat = progress.snapshot()
        self.assertIsNotNone(beat.rate_trailing)
        self.assertIsNotNone(beat.rate_cumulative)
        self.assertLess(beat.rate_trailing, beat.rate_cumulative)

    def test_eta_uses_trailing_throughput_so_it_reacts_to_a_slowdown(self):
        clock = FakeClock()
        progress = Progress(total=100, sink=None, clock=clock, window=4)
        for _ in range(10):
            clock.tick(1.0)
            progress.advance(1)
        fast_eta = progress.snapshot().eta_seconds
        for _ in range(4):
            clock.tick(10.0)
            progress.advance(1)
        slow_eta = progress.snapshot().eta_seconds
        self.assertGreater(slow_eta, fast_eta)

    def test_eta_is_zero_not_negative_at_completion(self):
        clock = FakeClock()
        progress = Progress(total=10, sink=None, clock=clock)
        clock.tick(1.0)
        progress.advance(10)
        self.assertEqual(progress.snapshot().eta_seconds, 0.0)

    def test_completed_counter_is_monotonic_across_heartbeats(self):
        clock = FakeClock()
        progress = Progress(total=10, sink=None, clock=clock, interval=0.0)
        seen = []
        for _ in range(10):
            clock.tick(1.0)
            progress.advance(1)
            seen.append(progress.snapshot().completed)
        self.assertEqual(seen, sorted(seen))


class TestEmissionCadence(unittest.TestCase):

    def test_heartbeats_are_rate_limited(self):
        # 43,800 cycles must not become 43,800 lines.
        clock = FakeClock()
        lines, sink = _collector()
        progress = Progress(total=1000, sink=sink, clock=clock, interval=60.0)
        for _ in range(100):
            clock.tick(1.0)
            progress.advance(1)
        self.assertEqual(len(lines), 1)     # 100 s elapsed -> one heartbeat

    def test_close_always_emits_and_is_idempotent(self):
        clock = FakeClock()
        lines, sink = _collector()
        progress = Progress(total=10, sink=sink, clock=clock, interval=60.0)
        progress.advance(10)
        self.assertEqual(len(lines), 0)
        progress.close()
        self.assertEqual(len(lines), 1)
        progress.close()                    # safe inside a `finally`
        self.assertEqual(len(lines), 1)

    def test_phase_change_forces_a_heartbeat_at_the_transition(self):
        clock = FakeClock()
        lines, sink = _collector()
        progress = Progress(total=10, sink=sink, clock=clock, interval=60.0)
        progress.set_phase("corpus/download", total=500)
        self.assertEqual(len(lines), 1)     # entering the new phase
        self.assertIn("corpus/download", lines[-1])

    def test_an_idle_phase_is_not_given_a_closing_heartbeat(self):
        # Every transition at the start of a job would otherwise emit two
        # identical lines saying nothing.
        clock = FakeClock()
        lines, sink = _collector()
        progress = Progress(total=10, phase="a", sink=sink, clock=clock,
                            interval=60.0)
        progress.set_phase("b")
        self.assertEqual([line.split(" |")[0] for line in lines], ["[b]"])

    def test_a_phase_that_did_work_gets_a_closing_heartbeat(self):
        clock = FakeClock()
        lines, sink = _collector()
        progress = Progress(total=10, phase="a", sink=sink, clock=clock,
                            interval=60.0)
        progress.advance(3)
        progress.set_phase("b")
        self.assertEqual([line.split(" |")[0] for line in lines], ["[a]", "[b]"])

    def test_a_new_phase_total_resets_the_completed_counter(self):
        clock = FakeClock()
        progress = Progress(total=10, sink=None, clock=clock)
        progress.advance(10)
        progress.set_phase("next", total=100)
        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.snapshot().total, 100)


class TestStallDetection(unittest.TestCase):
    """Alive and progressing are different questions."""

    def test_no_advance_for_long_enough_is_a_stall(self):
        clock = FakeClock()
        progress = Progress(total=100, sink=None, clock=clock, stall_after=300.0)
        progress.advance(1)
        self.assertFalse(progress.stalled)
        clock.tick(301.0)
        self.assertTrue(progress.stalled)

    def test_slow_but_advancing_is_not_a_stall(self):
        clock = FakeClock()
        progress = Progress(total=100, sink=None, clock=clock, stall_after=300.0)
        for _ in range(5):
            clock.tick(299.0)
            progress.advance(1)
        self.assertFalse(progress.stalled)

    def test_a_finished_job_is_never_stalled(self):
        clock = FakeClock()
        progress = Progress(total=5, sink=None, clock=clock, stall_after=1.0)
        progress.advance(5)
        clock.tick(10_000.0)
        self.assertFalse(progress.stalled)

    def test_the_stall_flag_reaches_the_rendered_line(self):
        clock = FakeClock()
        progress = Progress(total=100, sink=None, clock=clock, stall_after=1.0)
        progress.advance(1)
        clock.tick(60.0)
        self.assertIn("STALLED", progress.snapshot().render())


class TestOperationalLog(unittest.TestCase):

    def test_records_are_append_only_jsonl(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "ops.jsonl"
            log = OperationalLog(path)
            log.append({"kind": "event", "message": "one"})
            log.append({"kind": "event", "message": "two"})
            rows = [json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["message"] for r in rows], ["one", "two"])
            self.assertTrue(all("at" in r for r in rows))

    def test_progress_writes_heartbeats_to_the_log(self):
        with TemporaryDirectory() as tmp:
            clock = FakeClock()
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            progress = Progress(total=10, sink=None, log=log, clock=clock,
                                interval=0.0)
            progress.advance(1)
            progress.close()
            rows = [json.loads(line) for line
                    in log.path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(r["kind"] == "heartbeat" for r in rows))

    def test_note_records_an_event_without_advancing_work(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            progress = Progress(total=10, sink=None, log=log,
                                clock=FakeClock())
            progress.note("resume", already_covered=40_000)
            self.assertEqual(progress.completed, 0)
            row = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["kind"], "event")
            self.assertEqual(row["already_covered"], 40_000)


class TestFailureForensics(unittest.TestCase):
    """A job that dies at hour 37 must be diagnosable without re-running."""

    def test_a_failure_records_the_traceback_and_the_configuration(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            config = {"start": 1, "end": 2, "universe_mode": "volume"}
            with self.assertRaises(ZeroDivisionError):
                with JobRecorder(log, "replay", config):
                    raise ZeroDivisionError("degenerate ATR")
            rows = [json.loads(line) for line
                    in log.path.read_text(encoding="utf-8").splitlines()]
            failure = [r for r in rows if r["kind"] == "job_failure"][0]
            self.assertEqual(failure["error_type"], "ZeroDivisionError")
            self.assertIn("degenerate ATR", failure["error"])
            self.assertIn("ZeroDivisionError", failure["traceback"])
            self.assertEqual(failure["config"], config)

    def test_a_failure_records_how_far_the_job_got(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            progress = Progress(total=1000, sink=None, clock=FakeClock())
            progress.advance(370)
            with self.assertRaises(RuntimeError):
                with JobRecorder(log, "replay", {}, progress):
                    raise RuntimeError("boom")
            rows = [json.loads(line) for line
                    in log.path.read_text(encoding="utf-8").splitlines()]
            failure = [r for r in rows if r["kind"] == "job_failure"][0]
            self.assertEqual(failure["progress"]["completed"], 370)
            self.assertEqual(failure["progress"]["percent"], 37.0)

    def test_the_exception_is_never_swallowed(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            with self.assertRaises(KeyError):
                with JobRecorder(log, "job", {}):
                    raise KeyError("must propagate")

    def test_a_clean_job_records_a_finish(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            with JobRecorder(log, "job", {}):
                pass
            kinds = [json.loads(line)["kind"] for line
                     in log.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(kinds, ["job_start", "job_finish"])


class TestRunPreservation(unittest.TestCase):
    """A crashed bundle is the only evidence the failure produced."""

    def test_an_existing_bundle_is_renamed_not_deleted(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "replay-abc"
            run_dir.mkdir(parents=True)
            (run_dir / "run_health.jsonl").write_text("{}\n", encoding="utf-8")
            archived = preserve_failed_run(run_dir)
            self.assertIsNotNone(archived)
            self.assertFalse(run_dir.exists())
            self.assertTrue((archived / "run_health.jsonl").exists())
            self.assertIn("failed-", archived.name)

    def test_nothing_to_preserve_returns_none(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "runs" / "replay-abc"
            self.assertIsNone(preserve_failed_run(missing))
            missing.mkdir(parents=True)
            self.assertIsNone(preserve_failed_run(missing))

    def test_repeated_failures_do_not_collide(self):
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "replay-abc"
            first_names = set()
            for _ in range(2):
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "partial.jsonl").write_text("x", encoding="utf-8")
                archived = preserve_failed_run(run_dir)
                first_names.add(archived.name)
            self.assertEqual(len(first_names), 2)


class TestAcquisitionReporting(unittest.TestCase):
    """`corpus._item_reporter` maps one acquired item onto the heartbeat.

    Acquisition is the phase that runs for hours with nothing to say, so the
    mapping is the whole operational story: an operator must be able to tell "we
    could not get this" from "Binance correctly had nothing for that period".
    """

    def _reporter(self, progress):
        from backtest.corpus import _item_reporter
        return _item_reporter(progress)

    def _item(self, status: str, attempts: int = 1, retries: int = 0):
        from backtest.acquire.downloader import ItemOutcome
        return ItemOutcome(symbol="AAABUSDT", timeframe="1h", period="2023-04",
                           status=status, attempts=attempts, retries=retries,
                           detail="boom")

    def test_no_observer_means_no_callback_at_all(self):
        self.assertIsNone(self._reporter(None))

    def test_a_downloaded_item_advances_without_counting_a_skip(self):
        progress = Progress(total=3, sink=None, clock=FakeClock())
        self._reporter(progress)(self._item("downloaded"), 1, 3)
        beat = progress.snapshot()
        self.assertEqual((beat.completed, beat.skipped, beat.failures), (1, 0, 0))

    def test_a_404_is_counted_as_skipped_not_failed(self):
        # An `empty` outcome is Binance reporting the symbol was not listed in
        # that period. It is committed as evidence, and calling it a failure
        # would make a correct acquisition look broken.
        progress = Progress(total=3, sink=None, clock=FakeClock())
        self._reporter(progress)(self._item("empty"), 1, 3)
        beat = progress.snapshot()
        self.assertEqual((beat.skipped, beat.failures), (1, 0))

    def test_a_failure_is_counted_and_logged_with_its_reason(self):
        with TemporaryDirectory() as tmp:
            log = OperationalLog(Path(tmp) / "ops.jsonl")
            progress = Progress(total=3, sink=None, log=log, clock=FakeClock())
            self._reporter(progress)(self._item("failed", attempts=4), 1, 3)
            self.assertEqual(progress.snapshot().failures, 1)
            events = [json.loads(line) for line
                      in log.path.read_text(encoding="utf-8").splitlines()
                      if json.loads(line)["kind"] == "event"]
            self.assertEqual(events[0]["message"], "acquisition_failed")
            self.assertEqual(events[0]["symbol"], "AAABUSDT")
            self.assertEqual(events[0]["detail"], "boom")

    def test_retries_come_from_the_outcome_not_from_attempts(self):
        # Was `test_retries_are_attempts_beyond_the_first`, which computed
        # `attempts - 1`. `attempts` counts *operations* — a bulk candidate
        # performs two (checksum, payload) even when nothing goes wrong — so
        # that formula reported one retry per flawless download and turned a
        # perfect Stage 1 network into "31,738 retries".
        progress = Progress(total=3, sink=None, clock=FakeClock())
        report = self._reporter(progress)
        report(self._item("downloaded", attempts=2), 1, 3)   # clean: 0 retries
        self.assertEqual(progress.snapshot().retries, 0)
        report(self._item("downloaded", attempts=4, retries=2), 2, 3)
        self.assertEqual(progress.snapshot().retries, 2)

    def test_an_absent_period_counts_as_skipped_not_failed(self):
        progress = Progress(total=3, sink=None, clock=FakeClock())
        self._reporter(progress)(self._item("absent"), 1, 3)
        beat = progress.snapshot()
        self.assertEqual((beat.skipped, beat.failures), (1, 0))

    def test_the_downloader_calls_progress_once_per_item(self):
        from backtest.acquire.downloader import Downloader
        from backtest.acquire.planner import DownloadPlan, PlanItem

        # Items with no candidates fail immediately without touching transport
        # or store, which is enough to exercise the loop's call contract.
        items = tuple(
            PlanItem(symbol=f"S{i}USDT", timeframe="1h", period="2023-04",
                     coverage_start_ms=0, coverage_end_ms=1, candidates=())
            for i in range(3))
        plan = DownloadPlan(items=items, already_covered=7,
                            requested_start="2023-04-01",
                            requested_end="2023-04-30")
        seen: list[tuple[str, int, int]] = []
        report = Downloader(transport=None, store=None).run(
            plan, progress=lambda outcome, index, total: seen.append(
                (outcome.symbol, index, total)))
        self.assertEqual(seen, [("S0USDT", 1, 3), ("S1USDT", 2, 3),
                                ("S2USDT", 3, 3)])
        self.assertEqual(len(report.failures), 3)


class TestRendering(unittest.TestCase):

    def test_duration_formats_and_tolerates_absence(self):
        self.assertEqual(human_duration(0), "0:00:00")
        self.assertEqual(human_duration(3661), "1:01:01")
        self.assertEqual(human_duration(None), "-")
        self.assertEqual(human_duration(-1), "-")

    def test_the_rendered_line_is_pure_ascii(self):
        # The Windows console renders `.` and `-` as mojibake; a status line an
        # operator squints at is a status line they stop reading.
        clock = FakeClock()
        progress = Progress(total=100, phase="corpus/download", sink=None,
                            clock=clock, stall_after=1.0)
        clock.tick(5.0)
        progress.advance(1, detail="BTCUSDT", retries=1, nbytes=10, failed=1)
        clock.tick(5.0)
        line = progress.snapshot().render()
        line.encode("ascii")                # raises if anything non-ASCII crept in
        self.assertIn("STALLED", line)

    def test_cpu_fraction_is_none_without_elapsed_time(self):
        self.assertIsNone(cpu_fraction(1.0, 0.0))
        self.assertEqual(cpu_fraction(0.5, 1.0), 0.5)

    def test_rss_is_an_int_or_none_and_never_raises(self):
        value = rss_bytes()
        self.assertTrue(value is None or isinstance(value, int))

    def test_rendered_line_carries_the_operational_essentials(self):
        clock = FakeClock()
        progress = Progress(total=100, phase="corpus/download", unit="files",
                            sink=None, clock=clock)
        clock.tick(10.0)
        progress.advance(25, detail="BTCUSDT 1h 2023-04", retries=2, nbytes=2048)
        line = progress.snapshot().render()
        for fragment in ("corpus/download", "25", "100", "files", "elapsed",
                         "eta", "retries 2", "BTCUSDT"):
            self.assertIn(fragment, line)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
