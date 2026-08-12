"""Regression tests for conservative retrieval-query normalization."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import vector_search
from app.utils.query_normalization import normalize_retrieval_query


class _Rows(list):
    """Provide the small SQLite cursor surface used by the retrieval function."""

    def fetchall(self):
        return self


class _Connection:
    """Minimal read-only database seam for checking retrieval handoff strings."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        if "SELECT d.id, d.current_version_id" in query:
            return _Rows([{"id": 1, "searchable_version_id": 2}])
        return _Rows()


class _Store:
    def search(self, *args, **kwargs):
        return []


class QueryNormalizationTests(unittest.TestCase):
    def test_presentation_variations_normalize_without_lowercasing_entities(self) -> None:
        self.assertEqual(
            normalize_retrieval_query("  Maren\u00a0Voss\u2014EMP\u20117429  "),
            "Maren Voss-EMP-7429",
        )
        self.assertEqual(normalize_retrieval_query("Revenue is 50 percent"), "Revenue is 50%")
        self.assertEqual(normalize_retrieval_query("Invoice 1,250.50"), "Invoice 1250.50")

    def test_filenames_identifiers_and_factual_text_are_preserved(self) -> None:
        query = "Find Q4-2026.xlsx for ZXQ-19 and 1,234 records"
        normalized = normalize_retrieval_query(query)

        self.assertIn("Q4-2026.xlsx", normalized)
        self.assertIn("ZXQ-19", normalized)
        self.assertIn("1234", normalized)
        self.assertNotIn("q4-2026", normalized)

    def test_vector_and_keyword_retrieval_receive_the_same_normalized_query(self) -> None:
        with (
            patch.object(vector_search, "get_connection", return_value=_Connection()),
            patch.object(vector_search, "search_keyword_chunks", return_value=[]) as keyword,
            patch.object(vector_search, "create_embeddings", return_value=[[1.0]]) as embeddings,
            patch.object(vector_search, "get_vector_store", return_value=_Store()),
            patch.object(vector_search.settings, "rag_retrieval_mode", "hybrid"),
        ):
            vector_search.search_chunks("  ZXQ\u201119\u00a0at\u00a050 percent  ", owner_id=1, organization_id="org-a")

        self.assertEqual(keyword.call_args.args[0], "ZXQ-19 at 50%")
        self.assertEqual(embeddings.call_args.args[0], ["ZXQ-19 at 50%"])


if __name__ == "__main__":
    unittest.main()
