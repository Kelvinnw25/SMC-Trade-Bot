"""Tests for tests.regression.compare — the field-level comparison engine."""
from __future__ import annotations

import unittest

from tests.regression.compare import (
    FAIL,
    PASS,
    compare_field_maps,
    compare_observables,
)
from tests.regression.test_observable import make_ob


def status_at(report, path):
    for fc in report.fields:
        if fc.path == path:
            return fc
    raise AssertionError(f"no field {path!r} in report")


class TestFieldMapComparison(unittest.TestCase):
    def test_identical_maps_all_pass(self):
        m = {"a": 1, "b": "x", "c": True}
        report = compare_field_maps(m, dict(m), label="t")
        self.assertTrue(report.ok)
        self.assertEqual(report.passed, 3)
        self.assertEqual(report.failed, 0)

    def test_scalar_difference_fails(self):
        report = compare_field_maps({"a": 1}, {"a": 2}, label="t")
        self.assertFalse(report.ok)
        fc = status_at(report, "a")
        self.assertEqual(fc.status, FAIL)
        self.assertEqual(fc.expected, 1)
        self.assertEqual(fc.actual, 2)

    def test_float_exact_by_default(self):
        report = compare_field_maps({"a": 1.0}, {"a": 1.0001}, label="t")
        self.assertEqual(status_at(report, "a").status, FAIL)

    def test_float_tolerance_allows_close(self):
        report = compare_field_maps({"a": 1.0}, {"a": 1.0001}, label="t", float_tolerance=0.001)
        self.assertEqual(status_at(report, "a").status, PASS)

    def test_int_float_equal_values_pass(self):
        report = compare_field_maps({"a": 12}, {"a": 12.0}, label="t")
        self.assertEqual(status_at(report, "a").status, PASS)

    def test_nan_equals_nan(self):
        nan = float("nan")
        self.assertTrue(compare_field_maps({"a": nan}, {"a": nan}, label="t").ok)
        self.assertFalse(compare_field_maps({"a": nan}, {"a": 1.0}, label="t").ok)

    def test_missing_and_extra_fields_fail(self):
        report = compare_field_maps({"only_expected": 1}, {"only_actual": 2}, label="t")
        self.assertFalse(report.ok)
        self.assertIn("missing observable", status_at(report, "only_expected").detail)
        self.assertIn("added observable", status_at(report, "only_actual").detail)

    def test_bool_vs_int_is_type_divergence(self):
        report = compare_field_maps({"a": True}, {"a": 1}, label="t")
        fc = status_at(report, "a")
        self.assertEqual(fc.status, FAIL)
        self.assertIn("type", fc.detail)

    def test_none_vs_value_fails(self):
        report = compare_field_maps({"a": None}, {"a": "x"}, label="t")
        self.assertEqual(status_at(report, "a").status, FAIL)


class TestObservableComparison(unittest.TestCase):
    def test_equal_observables_pass(self):
        self.assertTrue(compare_observables(make_ob(), make_ob(), label="t").ok)

    def test_single_field_divergence_localized(self):
        report = compare_observables(make_ob(score=12), make_ob(score=13), label="t")
        self.assertFalse(report.ok)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].path, "score")

    def test_nested_payload_divergence_localized(self):
        report = compare_observables(
            make_ob(persistence_payload={"direction": "SHORT"}),
            make_ob(persistence_payload={"direction": "LONG"}),
            label="t",
        )
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].path, "persistence_payload.direction")


if __name__ == "__main__":
    unittest.main()
