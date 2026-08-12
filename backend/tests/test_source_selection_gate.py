"""Regression tests for the central RAG source-selection gate."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services.source_selection import (
    CandidateDecision,
    safe_tokens,
    select_sources,
    validate_grounded_result,
)


class SourceSelectionGateTests(unittest.TestCase):
    """Exercise routing decisions without running embedding or vector services."""

    @staticmethod
    def _decision(
        document_id: int,
        *,
        source_type: str = "excel",
        score: int = 8,
        reasons: list[str] | None = None,
        rejection_reason: str | None = None,
    ) -> CandidateDecision:
        """Build a focused structured candidate returned by the planner seam."""
        return CandidateDecision(
            document_id=document_id,
            source_type=source_type,
            score=score,
            schema_score=score,
            reasons=reasons or ["valid_structured_plan"],
            rejection_reason=rejection_reason,
        )

    @staticmethod
    def _source(
        document_id: int,
        *,
        filename: str = "notes.txt",
        content: str = "policy evidence",
        score: float = 0.9,
        version_id: int = 1,
        source_type: str = "text",
    ) -> dict[str, object]:
        """Build a stable retrieval result with no real document content."""
        return {
            "document_id": document_id,
            "version_id": version_id,
            "filename": filename,
            "content": content,
            "source_type": source_type,
            "source_location": {},
            "score": score,
        }

    def _select(self, **kwargs: object):
        """Avoid database-backed filename lookup unless a test explicitly routes it."""
        with patch(
            "app.services.source_selection._confident_document_id_from_question",
            return_value=None,
        ):
            return select_sources(owner_id=999, **kwargs)

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

    def test_explicit_filename_reference_routes_semantic_result(self) -> None:
        """A confident filename scope prevents unrelated semantic candidates winning."""
        selected = self._source(
            11,
            filename="leave-policy.txt",
            content="Leave policy permits five days.",
        )
        with patch(
            "app.services.source_selection._confident_document_id_from_question",
            return_value=11,
        ):
            result = select_sources(
                question="What does leave policy say?",
                owner_id=999,
                searcher=lambda *_args, **_kwargs: [selected],
            )

        self.assertEqual((result.path, result.document_id), ("retrieval", 11))
        self.assertEqual(result.reason, "filename_routed_semantic_evidence")
        self.assertGreaterEqual(result.diagnostics[0].score, 3)

    def test_explicit_filename_reference_routes_structured_workbook(self) -> None:
        """A named workbook uses its schema plan before broad retrieval."""
        with (
            patch(
                "app.services.source_selection._confident_document_id_from_question",
                return_value=12,
            ),
            patch(
                "app.services.source_selection._structured_decisions",
                return_value=[self._decision(12)],
            ),
        ):
            result = select_sources(
                question="In payroll workbook, list department totals.",
                owner_id=999,
                structured_requested=True,
                searcher=lambda *_args, **_kwargs: [],
            )

        self.assertEqual((result.path, result.document_id), ("structured", 12))
        self.assertEqual(result.reason, "filename_routed_structured_scope")

    def test_implicit_workbook_schema_reference_selects_structured_route(self) -> None:
        """Schema evidence can select a workbook when the filename is absent."""
        with patch(
            "app.services.source_selection._structured_decisions",
            return_value=[self._decision(21)],
        ):
            result = self._select(
                question="What is the total amount by department?",
                structured_requested=True,
                searcher=lambda *_args, **_kwargs: [],
            )

        self.assertEqual((result.path, result.document_id), ("structured", 21))
        self.assertEqual(result.reason, "structured_schema_evidence")

    def test_exact_column_value_filter_selects_structured_route(self) -> None:
        """An exact column/value match is stronger than incidental text overlap."""
        decision = self._decision(
            22,
            score=11,
            reasons=["valid_structured_plan", "validated_filter"],
        )
        with patch(
            "app.services.source_selection._structured_decisions",
            return_value=[decision],
        ):
            result = self._select(
                question="How many employees have Department equal to Finance?",
                structured_requested=True,
                searcher=lambda *_args, **_kwargs: [],
            )

        self.assertEqual((result.path, result.document_id), ("structured", 22))
        self.assertEqual(result.reason, "structured_filter_evidence")
        self.assertEqual(result.diagnostics[0].score, 11)

    def test_multiple_candidates_choose_stronger_semantic_document(self) -> None:
        """A clearly stronger semantic candidate is selected without clarification."""
        result = self._select(
            question="What is the travel reimbursement policy?",
            searcher=lambda *_args, **_kwargs: [
                self._source(31, content="Travel reimbursement policy and receipts."),
                self._source(32, content="Travel calendar dates.", score=0.2),
            ],
        )

        self.assertEqual((result.path, result.document_id), ("retrieval", 31))
        self.assertEqual(result.reason, "semantic_evidence")
        self.assertGreater(result.diagnostics[0].score, result.diagnostics[1].score)

    def test_weak_numeric_overlap_is_rejected_for_insufficient_confidence(self) -> None:
        """A bare numeric overlap must not be treated as a confident source match."""
        result = self._select(
            question="100",
            searcher=lambda *_args, **_kwargs: [
                self._source(41, content="The annual target is 100.", score=0.0),
            ],
        )

        self.assertEqual(result.path, "unavailable")
        self.assertEqual(result.reason, "insufficient_evidence")

    def test_ambiguous_sources_require_clarification(self) -> None:
        """Equal candidates from separate documents cannot be selected arbitrarily."""
        result = self._select(
            question="What is the travel policy?",
            searcher=lambda *_args, **_kwargs: [
                self._source(51, content="Travel policy evidence."),
                self._source(52, content="Travel policy evidence."),
            ],
        )

        self.assertEqual((result.path, result.document_id), ("clarification", None))
        self.assertEqual(result.reason, "ambiguous_candidate")

    def test_nonexistent_source_returns_unavailable(self) -> None:
        """A filename that resolves to no authorized retrieval result is unavailable."""
        with patch(
            "app.services.source_selection._confident_document_id_from_question",
            return_value=None,
        ):
            result = select_sources(
                question="What does missing handbook say?",
                owner_id=999,
                searcher=lambda *_args, **_kwargs: [],
            )

        self.assertEqual((result.path, result.document_id), ("unavailable", None))
        self.assertEqual(result.reason, "insufficient_evidence")

    def test_semantic_unstructured_question_uses_retrieval(self) -> None:
        """Narrative questions remain on the semantic/unstructured route."""
        result = self._select(
            question="Explain the remote work policy.",
            searcher=lambda *_args, **_kwargs: [
                self._source(61, content="The remote work policy permits two days."),
            ],
        )

        self.assertEqual((result.path, result.document_id), ("retrieval", 61))
        self.assertEqual(result.reason, "semantic_evidence")

    def test_multi_workbook_question_rejects_weaker_workbook(self) -> None:
        """A multi-workbook query keeps only the evidence-backed workbook route."""
        decisions = [
            self._decision(71, score=12, reasons=["valid_structured_plan", "validated_filter"]),
            self._decision(72, score=2, rejection_reason="insufficient_evidence"),
        ]
        with patch(
            "app.services.source_selection._structured_decisions",
            return_value=decisions,
        ):
            result = self._select(
                question="Compare Finance department totals across workbooks.",
                structured_requested=True,
                searcher=lambda *_args, **_kwargs: [],
            )

        self.assertEqual((result.path, result.document_id), ("structured", 71))
        self.assertEqual(result.reason, "structured_filter_evidence")
        self.assertEqual(result.diagnostics[1].rejection_reason, "insufficient_evidence")

    def test_inaccessible_current_document_is_rejected_before_search(self) -> None:
        """Deleted, stale, or unauthorized current document scopes never reach search."""
        called = False

        def searcher(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            nonlocal called
            called = True
            return []

        with patch(
            "app.services.source_selection._active_accessible_document",
            return_value=False,
        ):
            result = select_sources(
                question="Show current policy.",
                owner_id=999,
                document_id=81,
                version_id=4,
                searcher=searcher,
            )

        self.assertEqual((result.path, result.reason), ("unavailable", "acl_excluded"))
        self.assertFalse(called)

    def test_version_scope_is_forwarded_without_structured_cross_version_plan(self) -> None:
        """An explicit version stays in retrieval and is forwarded as the search filter."""
        search_args: dict[str, object] = {}

        def searcher(*_args: object, **kwargs: object) -> list[dict[str, object]]:
            search_args.update(kwargs)
            return [self._source(91, version_id=7, content="Current handbook policy.")]

        with patch("app.services.source_selection._structured_decisions") as structured:
            result = select_sources(
                question="What is in handbook version seven?",
                owner_id=999,
                document_id=91,
                version_id=7,
                structured_requested=True,
                searcher=searcher,
            )

        structured.assert_not_called()
        self.assertEqual(search_args["document_id"], 91)
        self.assertEqual(search_args["version_id"], 7)
        self.assertEqual((result.path, result.document_id), ("retrieval", 91))
        self.assertEqual(result.reason, "semantic_evidence")

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

    def test_grounded_answer_rejects_result_plan_row_outside_cited_source(self) -> None:
        """Provenance cannot claim a worksheet row that its source citation excludes."""
        result = {
            "answer": "Count: 1",
            "grounded": True,
            "sources": [{
                "document_id": 2,
                "version_id": 3,
                "filename": "ledger.xlsx",
                "source_location": {
                    "sheet_name": "March",
                    "row_ranges": [{"row_start": 2, "row_end": 2}],
                },
            }],
            "provenance": {
                "document_id": 2,
                "version_id": 3,
                "sheets": [{"sheet_name": "March", "row_ranges": [{"row_start": 9, "row_end": 9}]}],
            },
            "_context": {
                "document_ids": [2],
                "version_ids": [3],
                "result_plan": {
                    "document_id": 2,
                    "version_id": 3,
                    "sheets": [{"sheet_name": "March", "row_ranges": [{"row_start": 9, "row_end": 9}]}],
                },
            },
        }
        with patch("app.services.source_selection._active_accessible_document", return_value=True):
            validated = validate_grounded_result(result, selected_document_id=2, owner_id=999)

        self.assertFalse(validated["grounded"])
        self.assertEqual(validated["unavailable_reason"], "result_plan_source_mismatch")

    def test_retrieval_citation_must_match_a_final_context_chunk(self) -> None:
        """A discarded retrieved chunk cannot be cited after final context selection."""
        final_context = [{
            "document_id": 2,
            "version_id": 3,
            "filename": "guide.txt",
            "content": "Approved policy evidence.",
            "source_type": "text",
            "source_location": {"line_start": 1, "line_end": 2},
        }]
        result = {
            "answer": "Grounded",
            "grounded": True,
            "sources": [{
                "document_id": 2,
                "version_id": 3,
                "filename": "guide.txt",
                "text": "Discarded retrieved chunk.",
                "source_type": "text",
                "source_location": {"line_start": 3, "line_end": 4},
            }],
        }
        with patch("app.services.source_selection._active_accessible_document", return_value=True):
            validated = validate_grounded_result(
                result,
                selected_document_id=2,
                owner_id=999,
                final_context_sources=final_context,
            )

        self.assertFalse(validated["grounded"])
        self.assertEqual(validated["sources"], [])
        self.assertEqual(validated["unavailable_reason"], "citation_not_in_final_context")

    def test_unavailable_result_clears_misleading_citations(self) -> None:
        """An unavailable answer must never retain evidence citations."""
        validated = validate_grounded_result(
            {
                "answer": UNAVAILABLE_ANSWER,
                "grounded": False,
                "sources": [{"document_id": 2, "version_id": 3, "filename": "guide.txt"}],
            },
            selected_document_id=2,
            owner_id=999,
        )

        self.assertFalse(validated["grounded"])
        self.assertEqual(validated["sources"], [])
        self.assertEqual(validated["unavailable_reason"], "unavailable_with_citations")

    def test_grounded_citation_requires_current_version_metadata(self) -> None:
        """A citation without its current version cannot be validated as current."""
        validated = validate_grounded_result(
            {
                "answer": "Grounded",
                "grounded": True,
                "sources": [{"document_id": 2, "filename": "guide.txt"}],
            },
            selected_document_id=2,
            owner_id=999,
        )

        self.assertFalse(validated["grounded"])
        self.assertEqual(validated["sources"], [])
        self.assertEqual(validated["unavailable_reason"], "citation_version_missing")

    def test_unreadable_or_noncurrent_citation_is_cleared(self) -> None:
        """A citation that is no longer readable/current cannot support grounding."""
        with patch("app.services.source_selection._active_accessible_document", return_value=False):
            validated = validate_grounded_result(
                {
                    "answer": "Grounded",
                    "grounded": True,
                    "sources": [{
                        "document_id": 2,
                        "version_id": 3,
                        "filename": "guide.txt",
                    }],
                },
                selected_document_id=2,
                owner_id=999,
            )

        self.assertFalse(validated["grounded"])
        self.assertEqual(validated["sources"], [])
        self.assertEqual(validated["unavailable_reason"], "source_no_longer_accessible")

    def test_weak_unmatched_retrieval_evidence_is_unavailable(self) -> None:
        """A below-floor vector score without token evidence cannot create an answer context."""
        result = self._select(
            question="What is the Phoenix warranty duration?",
            searcher=lambda *_args, **_kwargs: [self._source(9, content="routine maintenance schedule", score=0.29)],
        )

        self.assertEqual((result.path, result.reason), ("unavailable", "insufficient_evidence"))
        self.assertEqual(result.sources, [])


if __name__ == "__main__":
    unittest.main()
