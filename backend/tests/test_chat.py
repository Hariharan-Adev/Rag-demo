"""Chat prompt tests."""

import unittest
from unittest.mock import patch

from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT
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
        self.assertIn("do not\ninvent facts", RAG_SYSTEM_PROMPT)
        self.assertIn("PDF page", RAG_SYSTEM_PROMPT)
        self.assertIn("PowerPoint slide", RAG_SYSTEM_PROMPT)
        self.assertIn("Excel sheet and cell/row range", RAG_SYSTEM_PROMPT)
        self.assertIn(
            "Never describe vector similarity or a retrieval-ranking score as factual confidence",
            RAG_SYSTEM_PROMPT,
        )


class RagRetrievalPolicyTests(unittest.TestCase):
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
