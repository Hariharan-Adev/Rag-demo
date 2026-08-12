"""Regression tests for bounded, evidence-aware final RAG context selection."""

import unittest
from unittest.mock import patch

from app.services import rag_service


def _source(
    chunk_id: int,
    document_id: int,
    chunk_index: int,
    content: str,
    score: float,
    *,
    content_id: int | None = None,
    source_type: str = "text",
    source_location: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build deterministic synthetic retrieval evidence without private content."""
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "version_id": 1,
        "filename": f"source-{document_id}.txt",
        "chunk_index": chunk_index,
        "content": content,
        "score": score,
        "content_id": content_id,
        "source_type": source_type,
        "source_location": source_location or {},
    }


class FinalContextSelectionTests(unittest.TestCase):
    """Verify selection favors sufficient, non-redundant evidence within budget."""

    def test_single_fact_uses_only_the_best_chunk(self) -> None:
        selected = rag_service.select_final_context(
            "Who owns the deployment policy?",
            [
                _source(1, 10, 0, "The deployment policy owner is Jordan Lee.", 0.95),
                _source(2, 10, 1, "The policy was revised in April.", 0.88),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1])

    def test_normal_question_keeps_table_and_relevant_paragraph_qualification(self) -> None:
        """Complementary prose remains available when it qualifies a table value."""
        selected = rag_service.select_final_context(
            "What is the Project Alpha current rate?",
            [
                _source(1, 10, 10, "Table row | Project: Project Alpha | Current rate: 17.5 percent", 0.92, source_type="word", source_location={"content_type": "table"}),
                _source(2, 10, 11, "Project Alpha current rate applies only after director approval.", 0.48, source_type="word", source_location={"content_type": "prose"}),
                _source(3, 10, 40, "The archive policy owner is Finance.", 0.10, source_type="word", source_location={"content_type": "prose"}),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 2])

    def test_complementary_context_excludes_wrong_version_and_duplicates(self) -> None:
        """Only same-version, non-duplicate complementary evidence joins the anchor."""
        selected = rag_service.select_final_context(
            "What is the Project Alpha current rate?",
            [
                _source(1, 10, 10, "Table row | Project: Project Alpha | Current rate: 17.5 percent", 0.92, source_type="word", source_location={"content_type": "table"}),
                _source(2, 10, 11, "Table row | Project: Project Alpha | Current rate: 17.5 percent", 0.90, source_type="word", source_location={"content_type": "table"}),
                {**_source(3, 10, 12, "Project Alpha current rate has a paragraph qualification.", 0.88, source_type="word", source_location={"content_type": "prose"}), "version_id": 2},
                _source(4, 10, 13, "Project Alpha current rate is visible in the OCR screenshot.", 0.80, source_type="pdf", source_location={"page_start": 1, "page_end": 1, "content_type": "image_ocr"}),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 4])

    def test_complementary_context_respects_count_limit(self) -> None:
        """Complementary additions remain bounded by the configured maximum."""
        candidates = [
            _source(1, 10, 10, "Table row | Project Alpha current rate: 17.5 percent", 0.92, source_type="word", source_location={"content_type": "table"}),
            _source(2, 10, 11, "Project Alpha current rate paragraph qualification.", 0.88, source_type="word", source_location={"content_type": "prose"}),
            _source(3, 10, 12, "Project Alpha current rate OCR evidence.", 0.86, source_type="pdf", source_location={"page_start": 1, "page_end": 1, "content_type": "image_ocr"}),
        ]
        with patch.object(rag_service.settings, "rag_complementary_context_limit", 2):
            selected = rag_service.select_final_context("Project Alpha current rate?", candidates)

        self.assertEqual([source["chunk_id"] for source in selected], [1, 2])

    def test_comparison_prefers_diverse_sources_then_adjacent_evidence(self) -> None:
        selected = rag_service.select_final_context(
            "Compare the Alpha and Beta rollout plans.",
            [
                _source(1, 10, 4, "Alpha rollout begins Monday.", 0.95),
                _source(2, 10, 5, "Alpha rollout has a two-week schedule.", 0.90),
                _source(3, 20, 0, "Beta rollout begins Friday.", 0.85),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 3, 2])

    def test_multi_document_question_keeps_independent_document_evidence(self) -> None:
        selected = rag_service.select_final_context(
            "What differs across both security documents?",
            [
                _source(1, 10, 0, "Document A requires quarterly reviews.", 0.94),
                _source(2, 10, 1, "Document A names the review owner.", 0.90),
                _source(3, 20, 0, "Document B requires monthly reviews.", 0.82),
            ],
        )

        self.assertEqual({source["document_id"] for source in selected[:2]}, {10, 20})

    def test_multi_part_question_adds_relevant_supporting_chunks(self) -> None:
        selected = rag_service.select_final_context(
            "What are the costs, timeline, and risks?",
            [
                _source(1, 10, 1, "Cost is 40 units.", 0.95),
                _source(2, 10, 2, "Timeline is six weeks.", 0.90),
                _source(3, 10, 4, "Risk is supplier availability.", 0.85),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 2, 3])

    def test_duplicate_chunks_do_not_consume_context_capacity(self) -> None:
        selected = rag_service.select_final_context(
            "Compare both service agreements.",
            [
                _source(1, 10, 0, "Service level is 99.9 percent.", 0.95),
                _source(2, 10, 1, "Service level is 99.9 percent.", 0.94),
                _source(3, 20, 0, "Second agreement service level is 99.5 percent.", 0.90),
                _source(4, 10, 2, "Credits apply after an outage.", 0.85),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 3, 4])

    def test_near_identical_prose_chunks_are_suppressed(self) -> None:
        selected = rag_service.select_final_context(
            "Compare both service agreements.",
            [
                _source(1, 10, 0, "The service agreement requires a response within four hours for every critical incident.", 0.95),
                _source(2, 10, 1, "The service agreement requires a response within four hours for each critical incident.", 0.94),
                _source(3, 20, 0, "The second agreement requires a response within eight hours for critical incidents.", 0.90),
                _source(4, 10, 2, "Service credits apply after a missed response target.", 0.85),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 3, 4])

    def test_overlapping_prose_chunks_with_the_same_evidence_are_suppressed(self) -> None:
        selected = rag_service.select_final_context(
            "Compare both response policies.",
            [
                _source(1, 10, 0, "Critical incidents require an acknowledged response within four hours and manager escalation after that deadline.", 0.95),
                _source(2, 10, 1, "Critical incidents require an acknowledged response within four hours and manager escalation after that deadline for every customer.", 0.94),
                _source(3, 20, 0, "The comparison policy requires an acknowledged response within eight hours.", 0.90),
                _source(4, 10, 2, "The policy records escalation ownership for each incident.", 0.85),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 3, 4])

    def test_canonical_duplicate_references_are_suppressed_across_documents(self) -> None:
        shared = "The published incident policy requires a response within four hours for every critical incident."
        selected = rag_service.select_final_context(
            "Compare both incident policy references.",
            [
                _source(1, 10, 0, shared, 0.95, content_id=900),
                _source(2, 20, 0, shared, 0.94, content_id=900),
                _source(3, 30, 0, "The exception policy permits an eight-hour response for low priority incidents.", 0.90, content_id=901),
                _source(4, 10, 1, "The incident policy owner approves service-credit exceptions.", 0.85, content_id=900),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 3, 4])

    def test_similar_distinct_workbook_rows_keep_their_provenance(self) -> None:
        row_text = "Employee Alex has an approved rating and completed all required goals for the review period."
        selected = rag_service.select_final_context(
            "Compare both employee review rows.",
            [
                _source(1, 10, 0, row_text, 0.95, source_type="excel", source_location={"sheet_name": "Reviews", "row_start": 2, "row_end": 2}),
                _source(2, 10, 1, row_text.replace("Alex", "Blair"), 0.94, source_type="excel", source_location={"sheet_name": "Reviews", "row_start": 3, "row_end": 3}),
                _source(3, 10, 2, "Both review rows contain final ratings approved by the manager.", 0.85, source_type="excel", source_location={"sheet_name": "Reviews", "row_start": 4, "row_end": 4}),
            ],
        )

        self.assertEqual([source["chunk_id"] for source in selected], [1, 2, 3])

    def test_selected_content_never_exceeds_the_configured_hard_budget(self) -> None:
        candidates = [
            _source(1, 10, 0, "a" * 20, 0.95),
            _source(2, 20, 0, "b" * 24, 0.90),
            _source(3, 10, 1, "c" * 20, 0.85),
        ]
        with patch.object(rag_service.settings, "rag_final_context_token_budget", 10):
            selected = rag_service.select_final_context("Compare both records.", candidates)

        self.assertLessEqual(
            sum(rag_service._context_tokens(source["content"]) for source in selected),
            10,
        )
        self.assertEqual([source["chunk_id"] for source in selected], [1, 3])

    def test_high_relevance_anchor_adds_same_section_neighbors(self) -> None:
        anchor = _source(
            1, 10, 4, "Heading: incident response timeline.", 0.90,
            content_id=100, source_type="pdf",
            source_location={"page_start": 2, "page_end": 2},
        )
        neighbor = _source(
            2, 10, 5, "The response timeline continues with the approval steps.", 0.90,
            content_id=100, source_type="pdf",
            source_location={"page_start": 2, "page_end": 2},
        )
        with patch.object(rag_service, "_load_adjacent_context_chunks", return_value=[neighbor]):
            expanded = rag_service.expand_final_context_neighbors([anchor], owner_id=7)

        self.assertEqual([source["chunk_id"] for source in expanded], [1, 2])

    def test_neighbor_expansion_rejects_low_relevance_or_wrong_scope(self) -> None:
        low_anchor = _source(1, 10, 4, "Weak reference.", 0.49, content_id=100)
        wrong_document = _source(2, 20, 5, "Wrong document neighbor.", 0.90, content_id=100)
        with patch.object(rag_service, "_load_adjacent_context_chunks", return_value=[wrong_document]) as loader:
            low_expanded = rag_service.expand_final_context_neighbors([low_anchor], owner_id=7)
            strong_anchor = _source(3, 10, 4, "Strong reference.", 0.90, content_id=100)
            wrong_scope_expanded = rag_service.expand_final_context_neighbors([strong_anchor], owner_id=7)

        self.assertEqual([source["chunk_id"] for source in low_expanded], [1])
        loader.assert_called_once_with(strong_anchor, owner_id=7)
        self.assertEqual([source["chunk_id"] for source in wrong_scope_expanded], [3])

    def test_neighbor_expansion_respects_the_existing_hard_token_budget(self) -> None:
        anchor = _source(1, 10, 4, "a" * 20, 0.90, content_id=100)
        neighbor = _source(2, 10, 5, "b" * 20, 0.90, content_id=100)
        with (
            patch.object(rag_service, "_load_adjacent_context_chunks", return_value=[neighbor]),
            patch.object(rag_service.settings, "rag_final_context_token_budget", 5),
        ):
            expanded = rag_service.expand_final_context_neighbors([anchor], owner_id=7)

        self.assertEqual([source["chunk_id"] for source in expanded], [1])
