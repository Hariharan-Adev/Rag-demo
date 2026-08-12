"""Chat prompt tests."""

import unittest
from unittest.mock import patch

from app.config import settings
from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT, UNAVAILABLE_ANSWER
from app.services.rag_service import answer_question
from app.services.source_selection import SelectionResult


class RagPromptFormattingTests(unittest.TestCase):
    def test_comparison_phrases_are_covered(self) -> None:
        for phrase in (
            "compare",
            "comparison",
            "difference between",
            "differences",
            "versus",
            "vs",
            "pros and cons",
            "similarities and differences",
            "side-by-side comparison",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, RAG_SYSTEM_PROMPT)

    def test_comparisons_require_gfm_pipe_tables(self) -> None:
        normalized_prompt = " ".join(RAG_SYSTEM_PROMPT.split())

        self.assertIn("GitHub-Flavored Markdown table", normalized_prompt)
        self.assertIn("actual Markdown pipe syntax", normalized_prompt)
        self.assertIn(
            "Do not put the table in a fenced code block",
            normalized_prompt,
        )

    def test_non_comparison_formats_and_grounding_are_preserved(self) -> None:
        self.assertIn("paragraphs for normal explanations", RAG_SYSTEM_PROMPT)
        self.assertIn("bullet lists for unordered information", RAG_SYSTEM_PROMPT)
        self.assertIn("numbered lists for procedures", RAG_SYSTEM_PROMPT)
        self.assertIn("Do not force non-comparison answers into tables", RAG_SYSTEM_PROMPT)
        self.assertIn("Do not infer missing facts", RAG_SYSTEM_PROMPT)
        self.assertIn("Do not use general knowledge", RAG_SYSTEM_PROMPT)
        self.assertIn("PDF page", RAG_SYSTEM_PROMPT)
        self.assertIn("PowerPoint slide", RAG_SYSTEM_PROMPT)
        self.assertIn("Excel sheet and cell/row range", RAG_SYSTEM_PROMPT)
        self.assertIn(
            "Never describe vector similarity or a retrieval-ranking score as factual confidence",
            RAG_SYSTEM_PROMPT,
        )


class RagRetrievalPolicyTests(unittest.TestCase):
    def test_structured_lookup_uses_selected_source_without_llm(self) -> None:
        structured = {
            "answer": "| Revenue |\n|---:|\n| 108,000 |",
            "question_type": "structured_lookup",
            "calculation_basis": "1 matching row.",
            "grounded": True,
            "sources": [{"document_id": 12, "version_id": 13, "filename": "finance.xlsx"}],
            "matched_document_count": 1,
            "matched_row_count": 1,
        }
        with patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.is_structured_lookup_question",
            return_value=True,
        ), patch(
            "app.services.rag_service.select_sources",
            return_value=SelectionResult(path="structured", document_id=12),
        ), patch(
            "app.services.rag_service.analyze_workbook_question",
            return_value=structured,
        ), patch(
            "app.services.rag_service.generate_answer",
            side_effect=AssertionError("LLM must not run"),
        ), patch(
            "app.services.rag_service.log_audit_event",
        ) as audit:
            result = answer_question("February revenue", 7)

        self.assertEqual(result, structured)
        self.assertEqual(audit.call_args.kwargs["outcome"], "structured_lookup")

    def test_chat_does_not_call_llm_without_relevant_context(self) -> None:
        with patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            return_value=[],
        ), patch(
            "app.services.rag_service.generate_answer",
            side_effect=AssertionError("LLM must not run without context"),
        ):
            result = answer_question("Unknown", 7)

        self.assertEqual(result["sources"], [])
        self.assertFalse(result["grounded"])
        self.assertEqual(
            result["answer"],
            "Information not available in the uploaded files.",
        )

    def test_irrelevant_retrieved_context_never_reaches_answer_generation(self) -> None:
        """A citable chunk still needs query relevance before it can ground an answer."""
        source = {
            "document_id": 12,
            "version_id": 34,
            "filename": "weather-notes.txt",
            "content": "The forecast predicts rain and wind through Friday.",
            "source_type": "text",
            "source_location": {"line_start": 1, "line_end": 1},
            "score": 0.10,
        }
        with patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.select_sources",
            return_value=SelectionResult(
                path="retrieval", document_id=12, sources=[source]
            ),
        ), patch(
            "app.services.rag_service.generate_answer",
            side_effect=AssertionError("irrelevant context must not reach the model"),
        ), patch(
            "app.services.rag_service.log_audit_event",
        ):
            result = answer_question("What is the employee bonus amount?", 7)

        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
        self.assertFalse(result["grounded"])
        self.assertEqual(result["sources"], [])

    def test_uncitable_retrieved_context_never_reaches_answer_generation(self) -> None:
        """Missing version provenance cannot be repaired by a model response."""
        source = {
            "document_id": 12,
            "filename": "bonus.txt",
            "content": "The employee bonus amount is 500.",
            "source_type": "text",
            "source_location": {"line_start": 1, "line_end": 1},
            "score": 0.91,
        }
        with patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.select_sources",
            return_value=SelectionResult(
                path="retrieval", document_id=12, sources=[source]
            ),
        ), patch(
            "app.services.rag_service.generate_answer",
            side_effect=AssertionError("uncitable context must not reach the model"),
        ), patch(
            "app.services.rag_service.log_audit_event",
        ):
            result = answer_question("What is the employee bonus amount?", 7)

        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
        self.assertFalse(result["grounded"])
        self.assertEqual(result["sources"], [])

    def test_chat_retrieves_candidates_then_limits_grounded_context(self) -> None:
        candidates = [
            {
                "document_id": index,
                "version_id": index,
                "filename": f"{index}.txt",
                "content": f"context {index}",
                "source_type": "text",
                "source_location": {},
                "score": 0.9,
            }
            for index in range(1, 11)
        ]
        with patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            return_value=candidates,
        ) as search, patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "Grounded",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            },
        ) as generate, patch(
            "app.services.rag_service.reserve_groq_call"
        ), patch(
            "app.services.rag_service.record_groq_tokens"
        ), patch(
            "app.services.rag_service.log_audit_event"
        ):
            result = answer_question("Question", 7)

        self.assertEqual(search.call_args.kwargs["limit"], 15)
        self.assertEqual(search.call_args.kwargs["min_score"], settings.rag_min_score)
        self.assertFalse(result["grounded"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["question_type"], "clarification")
        generate.assert_not_called()

    def test_unavailable_llm_answer_never_returns_sources(self) -> None:
        candidate = {
            "document_id": 12,
            "version_id": 34,
            "filename": "agriculture_dataset.csv",
            "content": "Irrigation Pump\t75000\tIrrigation",
            "source_type": "csv",
            "source_location": {"row_start": 5, "row_end": 5},
            "score": 0.61,
        }
        with patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            return_value=[candidate],
        ), patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": UNAVAILABLE_ANSWER,
                "prompt_tokens": 8,
                "completion_tokens": 9,
            },
        ) as generate, patch(
            "app.services.rag_service.reserve_groq_call"
        ), patch(
            "app.services.rag_service.record_groq_tokens"
        ), patch(
            "app.services.rag_service.log_audit_event"
        ) as audit:
            result = answer_question(
                "Show all equipment priced between ₹50,000 and ₹200,000.",
                7,
            )

        self.assertIn("Irrigation Pump", generate.call_args.args[0])
        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(result["sources"], [])
        self.assertFalse(result["grounded"])
        self.assertEqual(audit.call_args.kwargs["outcome"], "insufficient_context")

    def test_weak_structured_result_does_not_return_before_retrieval(self) -> None:
        weak = {
            "answer": "No accessible structured answer.",
            "question_type": "analytical",
            "grounded": False,
            "sources": [],
        }
        with patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_analytical_question",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_structured_lookup_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.analyze_workbook_question",
            return_value=weak,
        ), patch(
            "app.services.rag_service.search_chunks",
            side_effect=[[], []],
        ), patch(
            "app.services.rag_service.generate_answer",
            side_effect=AssertionError("LLM must not run without evidence"),
        ), patch(
            "app.services.rag_service.log_audit_event",
        ):
            result = answer_question("Which summary applies to July?", 7)

        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(result["sources"], [])
        self.assertFalse(result["grounded"])

    def test_metric_question_uses_report_pdf_evidence(self) -> None:
        candidate = {
            "document_id": 44,
            "version_id": 45,
            "filename": "summary-report.pdf",
            "content": "Total reviewed 13,268. Total variance quantity 789. Variance rate 5.9%.",
            "source_type": "pdf",
            "source_location": {"page": 1},
            "score": 0.42,
        }
        with patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_analytical_question",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_structured_lookup_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.analyze_workbook_question",
            return_value={"answer": UNAVAILABLE_ANSWER, "grounded": False, "sources": []},
        ), patch(
            "app.services.rag_service.search_chunks",
            return_value=[candidate],
        ), patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "The total variance quantity is 789.",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            },
        ), patch(
            "app.services.rag_service.reserve_groq_call"
        ), patch(
            "app.services.rag_service.record_groq_tokens"
        ), patch(
            "app.services.rag_service.log_audit_event"
        ):
            result = answer_question("What is the total variance quantity?", 7)

        self.assertTrue(result["grounded"])
        self.assertEqual(result["sources"][0]["filename"], "summary-report.pdf")
        self.assertNotRegex(result["answer"].casefold(), r"select|type.*file|filename")

    def test_low_score_table_chunk_can_be_used_with_scoped_evidence(self) -> None:
        candidate = {
            "document_id": 12,
            "version_id": 34,
            "filename": "activity-log.xlsx",
            "content": "Period: 2026-07-01\nTitle: Folder Analysis\nTitle description: Reviewed folders.",
            "source_type": "excel",
            "source_location": {"sheet_name": "Rows", "row_start": 2, "row_end": 2},
            "score": 0.13,
        }
        with patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.is_structured_lookup_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            side_effect=[[], [candidate]],
        ), patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "Folder Analysis was listed for July.",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            },
        ) as generate, patch(
            "app.services.rag_service.reserve_groq_call"
        ), patch(
            "app.services.rag_service.record_groq_tokens"
        ), patch(
            "app.services.rag_service.log_audit_event"
        ):
            result = answer_question("Which title is listed for 2026 07?", 7)

        self.assertTrue(result["grounded"])
        self.assertEqual(result["sources"][0]["filename"], "activity-log.xlsx")
        self.assertIn("Folder Analysis", generate.call_args.args[0])

    def test_low_score_unrelated_chunk_is_not_cited(self) -> None:
        unrelated = {
            "document_id": 99,
            "version_id": 100,
            "filename": "unrelated.xlsx",
            "content": "Code: X1\nState: IN\nTime: 09:00",
            "source_type": "excel",
            "source_location": {"sheet_name": "Sheet1", "row_start": 2, "row_end": 2},
            "score": 0.13,
        }
        with patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=True,
        ), patch(
            "app.services.rag_service.is_analytical_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.is_structured_lookup_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            side_effect=[[], [unrelated]],
        ), patch(
            "app.services.rag_service.generate_answer",
            side_effect=AssertionError("unrelated evidence must not be grounded"),
        ), patch(
            "app.services.rag_service.log_audit_event",
        ):
            result = answer_question("Which title is listed for 2026 07?", 7)

        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(result["sources"], [])
        self.assertFalse(result["grounded"])
        self.assertNotRegex(result["answer"].casefold(), r"select|type.*file|filename")

    def test_unstructured_follow_up_retrieves_inside_prior_document(self) -> None:
        candidate = {
            "chunk_id": 501,
            "content_id": 50,
            "document_id": 12,
            "version_id": 34,
            "filename": "UARD-Hunt-BMT.docx",
            "content": "SAB change request items include approval workflow and export validation.",
            "source_type": "word",
            "source_location": {"paragraph_start": 11, "paragraph_end": 11},
            "score": 0.42,
        }
        calls = []

        def searcher(*args, **kwargs):
            calls.append(kwargs)
            return [candidate]

        with patch(
            "app.services.rag_service.scoped_unstructured_follow_up_document",
            return_value=(12, 34),
        ), patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            side_effect=searcher,
        ), patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "SAB change request items include approval workflow and export validation.",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            },
        ), patch(
            "app.services.rag_service.reserve_groq_call"
        ), patch(
            "app.services.rag_service.record_groq_tokens"
        ), patch(
            "app.services.rag_service.log_audit_event"
        ):
            result = answer_question(
                "Change Request Items: in SAB",
                7,
                conversation_id="bmt-chat",
            )

        self.assertTrue(result["grounded"])
        self.assertEqual(calls[0]["document_id"], 12)
        self.assertEqual(calls[0]["version_id"], 34)
        self.assertEqual(result["sources"][0]["filename"], "UARD-Hunt-BMT.docx")
