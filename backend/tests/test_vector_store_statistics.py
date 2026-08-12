"""Tests for privacy-safe vector-store health statistics."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.vector_store import VectorStore, vector_store_statistics


class _StatisticsStore(VectorStore):
    """Minimal provider seam for deterministic count reconciliation tests."""

    def upsert(self, points):
        return None

    def search(self, vector, **kwargs):
        return []

    def set_document_deleted(self, organization_id, document_id, deleted):
        return None

    def set_document_visibility(self, organization_id, document_id, visibility):
        return None

    def delete_document(self, organization_id, document_id):
        return None

    def clear(self, organization_id=None):
        return None

    def health(self):
        return {"total_points": 4, "status": "ok"}

    def list_active_points(self, organization_id=None):
        return {"current-point": {}, "orphaned-point": {}}


class VectorStoreStatisticsTests(unittest.TestCase):
    def test_counts_distinguish_total_current_and_stale_points(self) -> None:
        """Only IDs backed by current SQLite chunks count as active points."""
        with patch(
            "app.services.vector_store._current_sqlite_vector_point_ids",
            return_value={"current-point", "missing-point"},
        ):
            statistics = vector_store_statistics(_StatisticsStore())

        self.assertEqual(statistics["total_points"], 4)
        self.assertEqual(statistics["active_points"], 1)
        self.assertEqual(statistics["deleted_or_stale_points"], 3)
        self.assertEqual(statistics["sqlite_current_chunks"], 2)
        self.assertEqual(statistics["sync_status"], "out_of_sync")
        self.assertNotIn("content", statistics)
        self.assertNotIn("filename", statistics)


if __name__ == "__main__":
    unittest.main()
