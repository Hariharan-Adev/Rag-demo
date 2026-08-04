"""Chat prompt tests."""

import unittest
from unittest.mock import patch

from app.config import settings
from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT, UNAVAILABLE_ANSWER
from app.services.rag_service import (
    _context_limit_for_question,
    _output_contract,
    answer_question,
)
from app.services.source_selection import SelectionResult


class RagPromptFormattingTests(unittest.TestCase):
    def test_explicit_output_contracts_are_detected(self) -> None:
        contract = _output_contract("List all records as a JSON answer only")

        self.assertIn("every supported matching item", contract)
        self.assertIn("valid JSON only", contract)
        self.assertIn("without preamble", contract)
        self.assertEqual(
            _context_limit_for_question("Show every matching record"),
            max(
                settings.rag_final_context_limit,
                settings.rag_comprehensive_context_limit,
            ),
        )

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
    def test_comprehensive_request_uses_expanded_context_and_contract(self) -> None:
        candidates = [
            {
                "document_id": 12,
                "version_id": 34,
                "filename": "milestones.txt",
                "content": f"Project milestone {index}",
                "source_type": "text",
                "source_location": {"paragraph": index},
                "score": 0.9,
            }
            for index in range(1, 11)
        ]
        with patch(
            "app.services.rag_service.has_structured_workbook",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            return_value=candidates,
        ) as search, patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "| Milestone |\n|---|\n| 1 |",
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
            result = answer_question("List all project milestones as a table", 7)

        self.assertEqual(
            search.call_args.kwargs["limit"],
            max(settings.rag_retrieval_limit, settings.rag_comprehensive_context_limit),
        )
        self.assertEqual(len(result["sources"]), 10)
        prompt = generate.call_args.args[0]
        self.assertIn("every supported matching item", prompt)
        self.assertIn("GitHub-Flavored Markdown table", prompt)

    def test_unscoped_pdf_analytics_are_not_stolen_by_an_unrelated_workbook(self) -> None:
        pdf_candidate = {
            "document_id": 54,
            "version_id": 35,
            "filename": "FINAL INSPECTION REJECTION.pdf",
            "content": "CUSTOMER Rejection % LUCAS-TVS 50.6%",
            "source_type": "pdf",
            "source_location": {"page_start": 1, "page_end": 1},
            "score": 0.95,
        }
        with patch(
            "app.services.rag_service.has_structured_workbook",
            side_effect=lambda _user, _collection=None, document_id=None: (
                document_id is None
            ),
        ), patch(
            "app.services.rag_service.is_structured_lookup_question",
            return_value=False,
        ), patch(
            "app.services.rag_service.search_chunks",
            return_value=[pdf_candidate],
        ), patch(
            "app.services.rag_service.analyze_workbook_question",
            side_effect=AssertionError("unrelated workbook must not handle the question"),
        ), patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "LUCAS-TVS has the highest rejection percentage at 50.6%.",
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
                "Which customer has the highest rejection percentage?",
                7,
            )

        self.assertEqual(result["sources"][0]["filename"], pdf_candidate["filename"])
        self.assertIn("LUCAS-TVS", result["answer"])

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
