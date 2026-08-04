"""Vector search tests."""

import unittest

from app.services.vector_search import _search_terms


class HybridSearchTermTests(unittest.TestCase):
    def test_percent_symbol_and_percentage_have_the_same_search_term(self) -> None:
        self.assertIn("percentage", _search_terms("overall rejection %"))
        self.assertIn("percentage", _search_terms("overall rejection percentage"))

    def test_common_question_words_are_removed(self) -> None:
        self.assertEqual(
            set(_search_terms("Which customer has the highest rejection percentage?")),
            {"customer", "highest", "rejection", "percentage"},
        )


if __name__ == "__main__":
    unittest.main()
