"""Tests for tests.regression.report — rendering comparison results as text."""
from __future__ import annotations

import unittest

from tests.regression.compare import compare_field_maps
from tests.regression.report import render_report, render_suite


class TestRenderReport(unittest.TestCase):
    def test_passing_report_reads_pass(self):
        report = compare_field_maps({"a": 1}, {"a": 1}, label="BTC/USDT")
        text = render_report(report)
        self.assertIn("[PASS] BTC/USDT", text)
        self.assertIn("no divergences", text)

    def test_failing_report_shows_field_detail(self):
        report = compare_field_maps({"score": 12}, {"score": 13}, label="BTC/USDT")
        text = render_report(report)
        self.assertIn("[FAIL] BTC/USDT", text)
        self.assertIn("FAIL score", text)
        self.assertIn("expected 12", text)
        self.assertIn("got 13", text)

    def test_show_passes_lists_passing_fields(self):
        report = compare_field_maps({"a": 1, "b": 2}, {"a": 1, "b": 2}, label="t")
        self.assertIn("PASS a", render_report(report, show_passes=True))


class TestRenderSuite(unittest.TestCase):
    def test_aggregates_verdict(self):
        good = compare_field_maps({"a": 1}, {"a": 1}, label="good")
        bad = compare_field_maps({"a": 1}, {"a": 2}, label="bad")
        text = render_suite([good, bad])
        self.assertIn("2 input(s)", text)
        self.assertIn("diverged: bad", text)

    def test_all_equivalent_message(self):
        good = compare_field_maps({"a": 1}, {"a": 1}, label="good")
        self.assertIn("behaviorally equivalent", render_suite([good]))


if __name__ == "__main__":
    unittest.main()
