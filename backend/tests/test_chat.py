"""Chat prompt tests."""

import unittest

from app.prompts.rag_prompt import RAG_SYSTEM_PROMPT


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
