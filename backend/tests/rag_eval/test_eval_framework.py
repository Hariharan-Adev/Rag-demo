"""Self-tests for the RAG evaluation case schema and reusable assertions."""

from __future__ import annotations

import unittest

from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from tests.rag_eval.helpers import (
    VALID_CATEGORIES,
    GenerationObservation,
    RetrievalObservation,
    RetrievedChunk,
    assert_generation_matches,
    assert_retrieval_matches,
    cases_by_category,
    load_eval_cases,
)
from tests.rag_eval.regression_helpers import load_regression_cases


class RagEvalFrameworkTests(unittest.TestCase):
    def test_cases_load_and_cover_required_categories(self) -> None:
        cases = load_eval_cases()
        grouped = cases_by_category(cases)

        self.assertGreaterEqual(len(cases), len(VALID_CATEGORIES))
        for category in VALID_CATEGORIES:
            with self.subTest(category=category):
                self.assertGreaterEqual(len(grouped[category]), 1)

    def test_case_schema_supports_optional_stable_identifiers(self) -> None:
        case = next(item for item in load_eval_cases() if item.id == "exact_number_lookup_rating")

        self.assertEqual(case.expected_route, "structured")
        self.assertEqual(case.expected_document.filename, "performance-scale.xlsx")
        self.assertEqual(case.expected_source_ids.document_id, 1601)
        self.assertIn("meets expectations", case.expected_keywords)

    def test_retrieval_expectations_are_checked_without_generation(self) -> None:
        case = next(item for item in load_eval_cases() if item.id == "workbook_lookup_objectives_percentage")
        observation = RetrievalObservation(
            route="structured",
            selected_document_id=1601,
            chunks=(
                RetrievedChunk(
                    chunk_id=101,
                    document_id=1601,
                    version_id=1601,
                    filename="performance-scale.xlsx",
                    text="Rating: Meets Expectations | Objectives: 0.5",
                    score=None,
                ),
            ),
        )

        assert_retrieval_matches(self, case, observation)

    def test_generation_expectations_are_checked_without_retrieval(self) -> None:
        case = next(item for item in load_eval_cases() if item.id == "exact_number_lookup_rating")
        observation = GenerationObservation(
            answer="Meets Expectations corresponds to score 3.",
            grounded=True,
            sources=({"document_id": 1601, "filename": "performance-scale.xlsx"},),
        )

        assert_generation_matches(self, case, observation)

    def test_unavailable_generation_requires_no_sources(self) -> None:
        case = next(item for item in load_eval_cases() if item.id == "unanswerable_salary_policy")
        observation = GenerationObservation(
            answer=UNAVAILABLE_ANSWER,
            grounded=False,
            sources=(),
        )

        assert_generation_matches(self, case, observation)

    def test_known_failure_cases_separate_retrieval_and_answer_expectations(self) -> None:
        cases = load_regression_cases()

        self.assertGreaterEqual(len(cases), 14)
        for case in cases:
            with self.subTest(case=case.id):
                self.assertTrue(case.expected_retrieval.route)
                self.assertIsInstance(case.expected_answer.grounded, bool)


if __name__ == "__main__":
    unittest.main()
