"""Evaluation-only comparison of original and manually clarified queries.

These tests deliberately do not implement or invoke query rewriting.  They
record whether a manually clarified version changes deterministic retrieval.
"""

from __future__ import annotations

from dataclasses import replace
import unittest

from tests.rag_eval.regression_helpers import (
    RagRegressionCase,
    RagRegressionHarness,
    RetrievalRun,
    load_regression_cases,
)


CASES = {case.id: case for case in load_regression_cases()}


class ManualQueryPhrasingAssessmentTests(unittest.TestCase):
    """Compare equivalent wording with fresh, isolated local fixtures."""

    def _observe(self, case: RagRegressionCase) -> RetrievalRun:
        """Run one wording without allowing state to leak into the next run."""
        harness = RagRegressionHarness()
        harness.start()
        try:
            harness.prepare(case.fixture)
            return harness.observe_retrieval(case)
        finally:
            harness.close()

    @staticmethod
    def _source_signature(run: RetrievalRun) -> tuple[object, ...]:
        """Keep the comparison focused on route and provenance, not wording."""
        return (
            run.observation.route,
            run.observation.selected_document_id,
            tuple(sorted(chunk.filename for chunk in run.observation.chunks)),
            run.sheets,
            run.observation.unavailable,
        )

    def test_natural_language_spreadsheet_questions_match_manual_clarifications(self) -> None:
        """Natural spreadsheet wording already selects the same structured evidence."""
        clarifications = {
            "multi_tab_inventory_lookup": "Count all inventory records across the workbook.",
            "generic_percentage_aggregation": "Calculate the average of the Completion Rate column.",
            "multiple_workbook_schema_selection": "Sum Amount where Period is March.",
        }
        for case_id, clarified_query in clarifications.items():
            with self.subTest(case_id=case_id):
                original = CASES[case_id]
                clarified = replace(original, query=clarified_query)
                self.assertEqual(
                    self._source_signature(self._observe(original)),
                    self._source_signature(self._observe(clarified)),
                )

    def test_verbose_question_matches_concise_equivalent(self) -> None:
        """Polite filler does not change selection when the key constraints remain."""
        base = CASES["generic_record_count"]
        verbose = replace(
            base,
            query=(
                "Could you please help me understand, from the project roster workbook, "
                "how many projects are currently marked active?"
            ),
        )
        concise = replace(base, query="Count active projects.")
        self.assertEqual(
            self._source_signature(self._observe(verbose)),
            self._source_signature(self._observe(concise)),
        )

    def test_follow_up_clarification_changes_route_but_not_source(self) -> None:
        """Expanding a pronoun requires prior-result facts and is not a safe generic rewrite."""
        original = CASES["follow_up_show_those"]
        clarified = replace(
            original,
            query="List the equipment priced between 50000 and 200000.",
            prior_turns=(),
        )
        original_run = self._observe(original)
        clarified_run = self._observe(clarified)
        self.assertEqual(original_run.observation.route, "follow_up")
        self.assertEqual(clarified_run.observation.route, "structured")
        self.assertEqual(
            {chunk.filename for chunk in original_run.observation.chunks},
            {chunk.filename for chunk in clarified_run.observation.chunks},
        )


if __name__ == "__main__":
    unittest.main()
