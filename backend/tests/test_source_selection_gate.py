"""Regression tests for the central RAG source-selection gate."""

from __future__ import annotations

import unittest

from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services.source_selection import (
    safe_tokens,
    select_sources,
    validate_grounded_result,
)


class SourceSelectionGateTests(unittest.TestCase):
    def test_weak_tokens_and_substrings_are_ignored(self) -> None:
        self.assertNotIn("a", safe_tokens("A"))
        self.assertNotIn("in", safe_tokens("How many items in July?"))
        self.assertNotIn("art", safe_tokens("partial"))
        self.assertIn("partial", safe_tokens("partial"))

    def test_ambiguous_top_candidates_ask_for_clarification(self) -> None:
        candidates = [
            {
                "document_id": 10,
                "version_id": 11,
                "filename": "alpha.txt",
                "content": "Alpha topic evidence.",
                "source_type": "text",
                "source_location": {},
                "score": 0.9,
            },
            {
                "document_id": 20,
                "version_id": 21,
                "filename": "alpha-notes.txt",
                "content": "Alpha topic evidence.",
                "source_type": "text",
                "source_location": {},
                "score": 0.9,
            },
        ]

        result = select_sources(
            question="What is the alpha topic?",
            owner_id=999,
            searcher=lambda *args, **kwargs: candidates,
        )

        self.assertEqual(result.path, "clarification")
        self.assertEqual(result.reason, "ambiguous_candidate")

    def test_grounded_answer_requires_selected_plan_and_citation_match(self) -> None:
        result = validate_grounded_result(
            {
                "answer": "Grounded",
                "grounded": True,
                "sources": [{"document_id": 2, "version_id": 3, "filename": "b.txt"}],
                "_context": {"document_ids": [1], "version_ids": [3]},
            },
            selected_document_id=2,
            owner_id=999,
        )

        self.assertFalse(result["grounded"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(result["unavailable_reason"], "result_plan_document_mismatch")


if __name__ == "__main__":
    unittest.main()
