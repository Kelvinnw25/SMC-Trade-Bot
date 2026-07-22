"""Unit tests for v2.infra.clock — the injectable time source (Phase 2.1).

Isolated, no external dependencies. Covers: UTC-awareness of both clocks,
FakeClock determinism, manual time advance, Protocol compatibility, and the
edge cases (naive input, non-UTC normalization, negative advance, independence).
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone

from v2.infra.clock import Clock, FakeClock, SystemClock


class TestSystemClock(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = SystemClock()

    def test_now_is_timezone_aware_utc(self):
        moment = self.clock.now()
        self.assertIsInstance(moment, datetime)
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_timestamp_is_float_epoch(self):
        ts = self.clock.timestamp()
        self.assertIsInstance(ts, float)
        # Same semantics as V1's time.time() (Unix epoch), generous window.
        self.assertAlmostEqual(ts, time.time(), delta=5.0)

    def test_timestamp_agrees_with_now(self):
        before = self.clock.now().timestamp()
        ts = self.clock.timestamp()
        after = self.clock.now().timestamp()
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_now_does_not_go_backwards(self):
        first = self.clock.now()
        second = self.clock.now()
        self.assertGreaterEqual(second, first)


class TestFakeClockDeterminism(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        self.clock = FakeClock(self.start)

    def test_now_is_fixed_until_advanced(self):
        self.assertEqual(self.clock.now(), self.start)
        self.assertEqual(self.clock.now(), self.start)  # repeated read, unchanged

    def test_now_is_timezone_aware_utc(self):
        moment = self.clock.now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_timestamp_is_fixed_epoch(self):
        self.assertEqual(self.clock.timestamp(), self.start.timestamp())
        self.assertEqual(self.clock.timestamp(), self.start.timestamp())

    def test_timestamp_agrees_with_now(self):
        self.assertEqual(self.clock.timestamp(), self.clock.now().timestamp())


class TestFakeClockAdvance(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        self.clock = FakeClock(self.start)

    def test_advance_moves_now_forward(self):
        self.clock.advance(3600)
        self.assertEqual(self.clock.now(), self.start + timedelta(hours=1))

    def test_advance_moves_timestamp_forward(self):
        before = self.clock.timestamp()
        self.clock.advance(90)
        self.assertEqual(self.clock.timestamp(), before + 90)

    def test_advance_is_cumulative(self):
        self.clock.advance(10)
        self.clock.advance(5)
        self.assertEqual(self.clock.now(), self.start + timedelta(seconds=15))

    def test_advance_accepts_fractional_seconds(self):
        self.clock.advance(0.5)
        self.assertEqual(self.clock.now(), self.start + timedelta(seconds=0.5))

    def test_advance_zero_is_noop(self):
        self.clock.advance(0)
        self.assertEqual(self.clock.now(), self.start)

    def test_advance_negative_raises(self):
        with self.assertRaises(ValueError):
            self.clock.advance(-1)


class TestFakeClockEdgeCases(unittest.TestCase):
    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            FakeClock(datetime(2026, 7, 22, 12, 0, 0))  # no tzinfo

    def test_non_utc_input_normalized_to_utc(self):
        tz_plus7 = timezone(timedelta(hours=7))
        local = datetime(2026, 7, 22, 19, 0, 0, tzinfo=tz_plus7)  # == 12:00 UTC
        clock = FakeClock(local)
        self.assertEqual(clock.now().utcoffset(), timedelta(0))
        self.assertEqual(
            clock.now(), datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        )
        # The instant is preserved; only the representation is normalized.
        self.assertEqual(clock.timestamp(), local.timestamp())

    def test_instances_are_independent_no_global_state(self):
        start = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        a = FakeClock(start)
        b = FakeClock(start)
        a.advance(100)
        self.assertEqual(a.now(), start + timedelta(seconds=100))
        self.assertEqual(b.now(), start)  # unaffected by a


class TestProtocolCompatibility(unittest.TestCase):
    def test_system_clock_satisfies_protocol(self):
        self.assertIsInstance(SystemClock(), Clock)

    def test_fake_clock_satisfies_protocol(self):
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIsInstance(clock, Clock)

    def test_arbitrary_object_is_not_a_clock(self):
        self.assertNotIsInstance(object(), Clock)

    def test_consumer_accepts_any_clock_implementation(self):
        # A consumer typed against the Protocol works with either implementation
        # without knowing which — the injection point later phases rely on.
        def read_epoch(clock: Clock) -> float:
            return clock.timestamp()

        self.assertIsInstance(read_epoch(SystemClock()), float)
        seed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(read_epoch(FakeClock(seed)), seed.timestamp())


if __name__ == "__main__":
    unittest.main()
