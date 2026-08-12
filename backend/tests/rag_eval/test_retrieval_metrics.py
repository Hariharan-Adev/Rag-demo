"""Unit tests for deterministic retrieval metric calculations."""

from __future__ import annotations

import unittest

from tests.rag_eval.metrics import (
    DEFAULT_K_VALUES,
    calculate_metrics_at_k,
    correct_source_selected,
    expected_chunk_rank,
    hit_rate_at_k,
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


class RetrievalMetricTests(unittest.TestCase):
    """Verify relevance metrics against small, explicit ranked lists."""

    def test_relevant_result_at_position_one(self) -> None:
        ranked = ["relevant", "other"]

        self.assertEqual(hit_rate_at_k(ranked, {"relevant"}, 1), 1.0)
        self.assertEqual(recall_at_k(ranked, {"relevant"}, 1), 1.0)
        self.assertEqual(precision_at_k(ranked, {"relevant"}, 1), 1.0)
        self.assertEqual(reciprocal_rank(ranked, {"relevant"}), 1.0)
        self.assertEqual(expected_chunk_rank(ranked, {"relevant"}), 1)

    def test_relevant_result_at_position_five(self) -> None:
        ranked = ["a", "b", "c", "d", "relevant"]

        self.assertEqual(hit_rate_at_k(ranked, {"relevant"}, 3), 0.0)
        self.assertEqual(hit_rate_at_k(ranked, {"relevant"}, 5), 1.0)
        self.assertEqual(recall_at_k(ranked, {"relevant"}, 5), 1.0)
        self.assertEqual(precision_at_k(ranked, {"relevant"}, 5), 0.2)
        self.assertEqual(reciprocal_rank(ranked, {"relevant"}), 0.2)
        self.assertEqual(expected_chunk_rank(ranked, {"relevant"}, k=5), 5)

    def test_no_relevant_result_returns_zero_metrics(self) -> None:
        ranked = ["a", "b", "c"]

        self.assertEqual(hit_rate_at_k(ranked, {"missing"}, 3), 0.0)
        self.assertEqual(recall_at_k(ranked, {"missing"}, 3), 0.0)
        self.assertEqual(precision_at_k(ranked, {"missing"}, 3), 0.0)
        self.assertEqual(reciprocal_rank(ranked, {"missing"}), 0.0)
        self.assertIsNone(expected_chunk_rank(ranked, {"missing"}))

    def test_multiple_relevant_results_use_unique_hits(self) -> None:
        ranked = ["x", "b", "y", "a", "b"]
        relevant = {"a", "b", "c"}

        self.assertAlmostEqual(recall_at_k(ranked, relevant, 3), 1 / 3)
        self.assertAlmostEqual(precision_at_k(ranked, relevant, 3), 1 / 3)
        self.assertAlmostEqual(recall_at_k(ranked, relevant, 5), 2 / 3)
        self.assertAlmostEqual(precision_at_k(ranked, relevant, 5), 2 / 5)
        self.assertEqual(reciprocal_rank(ranked, relevant), 0.5)

    def test_mean_reciprocal_rank_averages_queries(self) -> None:
        ranked = [["r", "x"], ["a", "b", "c", "d", "r"], ["x"]]
        relevant = [{"r"}, {"r"}, {"r"}]

        self.assertAlmostEqual(mean_reciprocal_rank(ranked, relevant), 0.4)
        self.assertAlmostEqual(mean_reciprocal_rank(ranked, relevant, k=3), 1 / 3)

    def test_source_selection_and_chunk_rank_helpers(self) -> None:
        self.assertTrue(correct_source_selected(12, {10, 12}))
        self.assertFalse(correct_source_selected(20, {10, 12}))
        self.assertFalse(correct_source_selected(None, {10, 12}))
        self.assertEqual(expected_chunk_rank([101, 102, 103], {102, 103}), 2)
        self.assertIsNone(expected_chunk_rank([101, 102, 103], {103}, k=2))

    def test_configurable_k_values_include_standard_defaults(self) -> None:
        default_metrics = calculate_metrics_at_k(["r"], {"r"})
        custom_metrics = calculate_metrics_at_k(["x", "r"], {"r"}, [1, 3, 3])

        self.assertEqual(tuple(default_metrics), DEFAULT_K_VALUES)
        self.assertEqual(tuple(custom_metrics), (1, 3))
        self.assertEqual(custom_metrics[1].hit_rate, 0.0)
        self.assertEqual(custom_metrics[3].hit_rate, 1.0)
        self.assertAlmostEqual(custom_metrics[3].precision, 1 / 3)

    def test_invalid_inputs_are_rejected(self) -> None:
        for invalid_k in (0, -1, True, 1.5):
            with self.subTest(k=invalid_k), self.assertRaisesRegex(ValueError, "positive integer"):
                hit_rate_at_k(["r"], {"r"}, invalid_k)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "equal length"):
            mean_reciprocal_rank([["r"]], [])


if __name__ == "__main__":
    unittest.main()
