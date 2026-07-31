"""Chat prompt tests."""

import unittest
from unittest.mock import patch

from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT, UNAVAILABLE_ANSWER
from app.services.rag_service import answer_question


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
    def test_structured_lookup_bypasses_vector_search_and_llm(self) -> None:
        structured = {
            "answer": "| Revenue |\n|---:|\n| 108,000 |",
            "question_type": "structured_lookup",
            "calculation_basis": "1 matching row.",
            "grounded": True,
            "sources": [{"filename": "finance.xlsx"}],
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
            "app.services.rag_service.analyze_workbook_question",
            return_value=structured,
        ), patch(
            "app.services.rag_service.search_chunks",
            side_effect=AssertionError("vector search must not run"),
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
            "The requested information is not available in the uploaded documents.",
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
        self.assertEqual(search.call_args.kwargs["min_score"], 0.35)
        self.assertEqual(len(result["sources"]), 5)
        prompt = generate.call_args.args[0]
        self.assertIn("context 5", prompt)
        self.assertNotIn("context 6", prompt)

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
