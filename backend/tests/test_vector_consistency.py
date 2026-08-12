"""Unit tests for read-only SQLite/Qdrant discrepancy classification."""

from __future__ import annotations

import unittest

from scripts.check_vector_consistency import _discrepancies


class VectorConsistencyTests(unittest.TestCase):
    def test_classifies_lifecycle_and_provider_discrepancies(self) -> None:
        """Every reported issue contains IDs and lifecycle metadata, never chunk text."""
        active_chunks = [
            {"chunk_id": 1, "document_id": 10, "version_id": 100, "vector_point_id": None},
            {"chunk_id": 2, "document_id": 10, "version_id": 100, "vector_point_id": "missing"},
            {"chunk_id": 3, "document_id": 11, "version_id": 110, "vector_point_id": "current"},
            {"chunk_id": 4, "document_id": 12, "version_id": 120, "vector_point_id": "duplicate"},
            {"chunk_id": 5, "document_id": 12, "version_id": 120, "vector_point_id": "duplicate"},
        ]
        all_chunks = active_chunks + [
            {"chunk_id": 6, "document_id": 20, "version_id": 200, "vector_point_id": "deleted", "document_deleted": True},
            {"chunk_id": 7, "document_id": 30, "version_id": 300, "vector_point_id": "old-version", "document_deleted": False},
        ]
        points = {
            "current": {"document_id": 11, "document_version_id": 999},
            "deleted": {"document_id": 20, "document_version_id": 200},
            "old-version": {"document_id": 30, "document_version_id": 300},
            "orphan": {"document_id": 40, "document_version_id": 400},
        }

        report = _discrepancies(active_chunks, all_chunks, points)

        self.assertEqual(report["sqlite_chunks_without_vector"], [{"chunk_id": 1, "document_id": 10, "version_id": 100}])
        self.assertEqual(report["sqlite_chunks_missing_active_vector"], [{"chunk_id": 2, "document_id": 10, "version_id": 100, "vector_point_id": "missing"}, {"chunk_id": 4, "document_id": 12, "version_id": 120, "vector_point_id": "duplicate"}, {"chunk_id": 5, "document_id": 12, "version_id": 120, "vector_point_id": "duplicate"}])
        self.assertEqual({item["vector_point_id"] for item in report["vectors_without_valid_current_sqlite_chunk"]}, {"deleted", "old-version", "orphan"})
        self.assertNotIn(
            "current",
            {item["vector_point_id"] for item in report["vectors_without_valid_current_sqlite_chunk"]},
        )
        self.assertEqual(report["deleted_document_vectors_marked_active"][0]["vector_point_id"], "deleted")
        self.assertEqual({item["vector_point_id"] for item in report["wrong_document_version_vectors"]}, {"current", "old-version"})
        self.assertEqual(report["duplicate_active_vector_ids"], [{"vector_point_id": "duplicate", "chunk_count": 2}])
        self.assertNotIn("text", str(report))


if __name__ == "__main__":
    unittest.main()
