#!/usr/bin/env python3
"""Offline tests for the local-model salary target contract."""

from __future__ import annotations

import unittest

from job_search.salary.training_target import (
    canonical_all_pay,
    canonical_figure_key_v3_simple,
    canonical_key,
    canonical_usd_pay,
    usd_primary_match_v3,
    usd_primary_match_v3_simple,
    usd_primary_match,
)


def result(*ranges):
    return {"classification": "salaried", "ranges": list(ranges)}


class SalaryTrainingTargetTests(unittest.TestCase):
    def test_base_ote_and_target_labels_are_equivalent(self) -> None:
        base = result({
            "component": "base_salary", "value_kind": "range", "currency": "USD",
            "period": "year", "min_value": 120000, "max_value": 160000,
        })
        ote_target = result({
            "component": "ote", "value_kind": "target", "currency": "usd",
            "period": "year", "min_value": 120000.0, "max_value": 160000.0,
        })
        unknown = result({
            "component": "unknown", "value_kind": "average", "currency": "USD",
            "period": "year", "min_value": 120000, "max_value": 160000,
        })
        self.assertEqual(canonical_key(base), canonical_key(ote_target))
        self.assertEqual(canonical_key(base), canonical_key(unknown))

    def test_only_usd_annual_or_hourly_complete_bounds_survive(self) -> None:
        extracted = canonical_usd_pay(result(
            {"component": "base_salary", "currency": "USD", "period": "hour", "min_value": 50, "max_value": 70},
            {"component": "base_salary", "currency": "CAD", "period": "year", "min_value": 100000, "max_value": 130000},
            {"component": "base_salary", "currency": "USD", "period": "month", "min_value": 9000, "max_value": 11000},
            {"component": "base_salary", "currency": "USD", "period": "year", "min_value": 100000, "max_value": None},
        ))
        self.assertEqual(extracted, [{
            "currency": "USD", "period": "hour", "min_value": 50, "max_value": 70,
        }])

    def test_raw_canonical_values_retain_non_usd_but_primary_score_does_not(self) -> None:
        mixed = result(
            {"currency": "USD", "period": "year", "min_value": 100000, "max_value": 130000},
            {"currency": "CAD", "period": "year", "min_value": 110000, "max_value": 145000},
            {"currency": "EUR", "period": "hour", "min_value": 60, "max_value": 75},
        )
        self.assertEqual({item["currency"] for item in canonical_all_pay(mixed)}, {
            "CAD", "EUR", "USD",
        })
        self.assertEqual({item["currency"] for item in canonical_usd_pay(mixed)}, {"USD"})

    def test_primary_match_ignores_non_usd_disagreement(self) -> None:
        gold = result(
            {"currency": "USD", "period": "year", "min_value": 100000, "max_value": 130000},
            {"currency": "CAD", "period": "year", "min_value": 110000, "max_value": 145000},
        )
        prediction = result(
            {"currency": "USD", "period": "year", "min_value": 100000, "max_value": 130000},
            {"currency": "CAD", "period": "year", "min_value": 1, "max_value": 2},
        )
        self.assertTrue(usd_primary_match(prediction, gold))
        prediction["ranges"][0]["max_value"] = 130001
        self.assertFalse(usd_primary_match(prediction, gold))

    def test_primary_match_accepts_one_supported_range_from_multiple_gold_ranges(self) -> None:
        gold = result(
            {"currency": "USD", "period": "year", "min_value": 60000, "max_value": 80000},
            {"currency": "USD", "period": "year", "min_value": 90000, "max_value": 115000},
        )
        one_range = result(
            {"currency": "USD", "period": "year", "min_value": 90000, "max_value": 115000},
        )
        self.assertTrue(usd_primary_match(one_range, gold))

    def test_primary_match_rejects_a_correct_range_plus_an_unsupported_guess(self) -> None:
        gold = result(
            {"currency": "USD", "period": "year", "min_value": 60000, "max_value": 80000},
            {"currency": "USD", "period": "year", "min_value": 90000, "max_value": 115000},
        )
        prediction = result(
            {"currency": "USD", "period": "year", "min_value": 60000, "max_value": 80000},
            {"currency": "USD", "period": "year", "min_value": 150000, "max_value": 200000},
        )
        self.assertFalse(usd_primary_match(prediction, gold))

    def test_primary_match_requires_no_usd_prediction_for_no_usd_gold(self) -> None:
        non_usd_gold = result(
            {"currency": "CAD", "period": "year", "min_value": 100000, "max_value": 130000},
        )
        self.assertTrue(usd_primary_match(result(), non_usd_gold))
        self.assertTrue(usd_primary_match(result(
            {"currency": "EUR", "period": "hour", "min_value": 50, "max_value": 70},
        ), non_usd_gold))
        self.assertFalse(usd_primary_match(result(
            {"currency": "USD", "period": "year", "min_value": 100000, "max_value": 130000},
        ), non_usd_gold))

    def test_bonus_equity_commission_and_stipend_are_ignored(self) -> None:
        ranges = [
            {"component": component, "currency": "USD", "period": "year", "min_value": 10000, "max_value": 20000}
            for component in ("bonus", "equity", "commission", "stipend")
        ]
        self.assertEqual(canonical_usd_pay(result(*ranges)), [])

    def test_exact_and_duplicate_values_canonicalize(self) -> None:
        item = {
            "component": "ote", "currency": "USD", "period": "year",
            "min_value": 150000, "max_value": 150000,
        }
        self.assertEqual(canonical_usd_pay(result(item, dict(item))), [{
            "currency": "USD", "period": "year",
            "min_value": 150000, "max_value": 150000,
        }])

    def test_v3_one_sided_values_preserve_shape_and_use_supported_overlap(self) -> None:
        gold = {"ranges": [
            {"value_kind": "minimum", "currency": "USD", "period": "year",
             "min_value": 175000, "max_value": None},
            {"value_kind": "range", "currency": "USD", "period": "year",
             "min_value": 190000, "max_value": 220000},
        ]}
        minimum = {"ranges": [gold["ranges"][0]]}
        incorrectly_exact = {"ranges": [{
            "value_kind": "exact", "currency": "USD", "period": "year",
            "min_value": 175000, "max_value": 175000,
        }]}
        self.assertTrue(usd_primary_match_v3(minimum, gold))
        self.assertFalse(usd_primary_match_v3(incorrectly_exact, gold))

    def test_v3_simple_matches_figures_without_bound_classification(self) -> None:
        gold = {"ranges": [{
            "value_kind": "minimum", "currency": "USD", "period": "year",
            "min_value": 175000, "max_value": None,
        }]}
        exact_prediction = {"ranges": [{
            "currency": "USD", "period": "year",
            "min_value": 175000, "max_value": 175000,
        }]}
        self.assertTrue(usd_primary_match_v3_simple(exact_prediction, gold))
        self.assertEqual(canonical_figure_key_v3_simple(exact_prediction), (
            ("USD", "year", 175000),
        ))

    def test_v3_simple_tolerates_extra_candidates_only_on_positive_jobs(self) -> None:
        gold = result({
            "currency": "USD", "period": "year",
            "min_value": 100000, "max_value": 120000,
        })
        prediction = result(
            {"currency": "USD", "period": "year", "min_value": 100000, "max_value": 120000},
            {"currency": "USD", "period": "year", "min_value": 5000, "max_value": 5000},
        )
        self.assertTrue(usd_primary_match_v3_simple(prediction, gold))
        self.assertFalse(usd_primary_match_v3_simple(prediction, result()))

    def test_v3_simple_still_requires_matching_period(self) -> None:
        gold = result({
            "currency": "USD", "period": "hour", "min_value": 20, "max_value": 24,
        })
        prediction = result({
            "currency": "USD", "period": "year", "min_value": 20, "max_value": 24,
        })
        self.assertFalse(usd_primary_match_v3_simple(prediction, gold))


if __name__ == "__main__":
    unittest.main()
