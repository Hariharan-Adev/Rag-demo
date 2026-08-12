"""Security regression coverage for hybrid search's authoritative recheck."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import database
from app.services import vector_search


class _InjectedVectorStore:
    """Return supplied IDs to prove search re-authorizes provider candidates."""

    def __init__(self, chunk_ids: list[int]) -> None:
        """Keep only identifiers; no private fixture content enters the provider seam."""
        self.chunk_ids = chunk_ids

    def search(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        """Simulate a stale or compromised provider response."""
        return [
            {"chunk_id": chunk_id, "score": 0.9 - index * 0.01}
            for index, chunk_id in enumerate(self.chunk_ids)
        ]


class HybridSearchSecurityTests(unittest.TestCase):
    """Verify lexical and vector signals cannot bypass document access rules."""

    def setUp(self) -> None:
        """Build an isolated multi-tenant current-version corpus."""
        self.temporary = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", Path(self.temporary.name) / "hybrid-security.db")
        )
        database.initialize_database()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                [("org-a", "Organization A"), ("org-b", "Organization B")],
            )
            connection.executemany(
                """INSERT INTO users (id, email, password_hash, organization_id)
                   VALUES (?, ?, 'hash', ?)""",
                [(1, "reader@example.com", "org-a"), (2, "private@example.com", "org-a"), (3, "other@example.com", "org-b")],
            )
            self._add_document(connection, 10, 1, "org-a", 100, 1000, "allowedonly101")
            self._add_document(connection, 20, 2, "org-a", 200, 2000, "privateonly202")
            self._add_document(connection, 30, 3, "org-b", 300, 3000, "crossorgonly303")
            self._add_document(connection, 40, 1, "org-a", 401, 4001, "oldonly404", current_version_id=402)
            self._add_version(connection, 402, 40, 1, "org-a", 4002, "currentonly405")
            self._add_document(connection, 50, 1, "org-a", 500, 5000, "deletedonly505", deleted=True)

    def tearDown(self) -> None:
        """Release patches before removing the temporary database."""
        self.stack.close()
        self.temporary.cleanup()

    @staticmethod
    def _add_version(
        connection: object,
        version_id: int,
        document_id: int,
        owner_id: int,
        organization_id: str,
        chunk_id: int,
        marker: str,
    ) -> None:
        """Insert a completed version and one searchable text chunk."""
        connection.execute(
            """INSERT INTO document_versions
               (id, organization_id, document_id, version_number, content_id,
                stored_filename, file_hash, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?)""",
            (
                version_id,
                organization_id,
                document_id,
                version_id,
                document_id,
                f"document-{document_id}.txt",
                f"hash-{document_id}",
                owner_id,
            ),
        )
        connection.execute(
            """INSERT INTO chunks
               (id, content_id, chunk_index, text, organization_id, document_id, version_id, source_type, indexing_status)
               VALUES (?, ?, 0, ?, ?, ?, ?, 'text', 'completed')""",
            (chunk_id, document_id, marker, organization_id, document_id, version_id),
        )

    def _add_document(
        self,
        connection: object,
        document_id: int,
        owner_id: int,
        organization_id: str,
        version_id: int,
        chunk_id: int,
        marker: str,
        *,
        current_version_id: int | None = None,
        deleted: bool = False,
    ) -> None:
        """Insert a private document with a current or intentionally stale version."""
        connection.execute(
            """INSERT INTO document_contents
               (id, owner_id, file_hash, normalized_content_hash, extracted_text,
                processing_status, organization_id)
               VALUES (?, ?, ?, ?, ?, 'completed', ?)""",
            (
                document_id,
                owner_id,
                f"hash-{document_id}",
                f"normalized-{document_id}",
                marker,
                organization_id,
            ),
        )
        connection.execute(
            """INSERT INTO documents
               (id, owner_id, original_filename, display_filename, stored_filename,
                file_hash, content_id, organization_id, visibility, processing_status, current_version_id, deleted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'private', 'completed', ?,
                       CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)""",
            (
                document_id,
                owner_id,
                f"document-{document_id}.txt",
                f"document-{document_id}.txt",
                f"document-{document_id}.txt",
                f"hash-{document_id}",
                document_id,
                organization_id,
                current_version_id or version_id,
                deleted,
            ),
        )
        self._add_version(connection, version_id, document_id, owner_id, organization_id, chunk_id, marker)

    def _search(self, query: str, injected_chunk_ids: list[int]) -> list[dict[str, object]]:
        """Run hybrid search with a provider response containing the supplied IDs."""
        with (
            patch.object(vector_search.settings, "rag_retrieval_mode", "hybrid"),
            patch.object(vector_search.settings, "rag_vector_candidate_limit", 10),
            patch.object(vector_search.settings, "rag_keyword_candidate_limit", 10),
            patch.object(vector_search, "create_embeddings", return_value=[[1.0] + [0.0] * 383]),
            patch.object(vector_search, "get_vector_store", return_value=_InjectedVectorStore(injected_chunk_ids)),
        ):
            return vector_search.search_chunks(query, owner_id=1, organization_id="org-a", limit=10)

    def test_hybrid_search_discards_private_and_cross_organization_provider_hits(self) -> None:
        """Both fusion signals remain limited to documents readable by the caller."""
        results = self._search("allowedonly101", [1000, 2000, 3000])

        self.assertEqual([item["chunk_id"] for item in results], [1000])
        self.assertEqual([item["document_id"] for item in results], [10])

    def test_hybrid_search_discards_deleted_and_noncurrent_provider_hits(self) -> None:
        """A provider cannot revive a soft-deleted document or an old version."""
        results = self._search("oldonly404 deletedonly505", [4001, 5000])

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
