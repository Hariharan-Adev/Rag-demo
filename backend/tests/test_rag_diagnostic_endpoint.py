"""Authorization and data-minimization tests for the RAG diagnostic API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import database
from app.auth import get_current_user
from app.main import app


class RagDiagnosticEndpointTests(unittest.TestCase):
    """Exercise authentication, ACL filtering, and the disabled-by-default flag."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_patch = patch.object(
            database,
            "DATABASE_PATH",
            Path(self.temporary.name) / "diagnostics.db",
        )
        self.database_patch.start()
        database.initialize_database()
        self.current_user = {
            "id": 10,
            "email": "reader@example.com",
            "organization_id": "org-a",
            "role": "member",
        }
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                [("org-a", "A"), ("org-b", "B")],
            )
            connection.executemany(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (?, ?, 'hash', ?, 'member')""",
                [
                    (10, "reader@example.com", "org-a"),
                    (11, "private-owner@example.com", "org-a"),
                    (20, "other-org@example.com", "org-b"),
                ],
            )
            for content_id, owner_id, organization_id in (
                (1, 10, "org-a"),
                (2, 11, "org-a"),
                (3, 20, "org-b"),
            ):
                connection.execute(
                    """INSERT INTO document_contents
                       (id, owner_id, file_hash, normalized_content_hash,
                        extracted_text, processing_status, organization_id)
                       VALUES (?, ?, ?, ?, 'private source text', 'completed', ?)""",
                    (
                        content_id,
                        owner_id,
                        f"hash-{content_id}",
                        f"normalized-{content_id}",
                        organization_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO documents
                       (id, owner_id, original_filename, display_filename,
                        stored_filename, file_hash, content_id, organization_id,
                        visibility, processing_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'private', 'completed')""",
                    (
                        content_id,
                        owner_id,
                        f"private-{content_id}.txt",
                        f"private-{content_id}.txt",
                        f"private-{content_id}.txt",
                        f"hash-{content_id}",
                        content_id,
                        organization_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO document_versions
                       (id, organization_id, document_id, version_number,
                        content_id, stored_filename, file_hash, status, created_by)
                       VALUES (?, ?, ?, 1, ?, ?, ?, 'completed', ?)""",
                    (
                        content_id,
                        organization_id,
                        content_id,
                        content_id,
                        f"private-{content_id}.txt",
                        f"hash-{content_id}",
                        owner_id,
                    ),
                )
                connection.execute(
                    "UPDATE documents SET current_version_id = ? WHERE id = ?",
                    (content_id, content_id),
                )
                connection.execute(
                    """INSERT INTO chunks
                       (id, content_id, chunk_index, text, organization_id,
                        document_id, version_id, source_type, indexing_status)
                       VALUES (?, ?, 0, ?, ?, ?, ?, 'text', 'completed')""",
                    (
                        content_id * 100,
                        content_id,
                        f"private document {content_id} content",
                        organization_id,
                        content_id,
                        content_id,
                    ),
                )
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.limit_patch = patch("app.routes.chat.enforce_request_limit")
        self.limit_patch.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.limit_patch.stop()
        app.dependency_overrides.clear()
        self.database_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _request(document_id: int | None = None) -> dict[str, object]:
        """Build a valid diagnostic request without sensitive fixture text."""
        payload: dict[str, object] = {"question": "Which policy applies?"}
        if document_id is not None:
            payload["document_id"] = document_id
        return payload

    @staticmethod
    def _record_mixed_trace(*args, **kwargs) -> dict[str, object]:
        """Simulate a compromised upstream trace to verify response ACL filtering."""
        diagnostic = kwargs["diagnostic"]
        diagnostic.start_request(
            query=str(args[0]),
            conversation_id=None,
            collection_id=None,
            document_id=None,
            version_id=None,
        )
        sources = [
            {"chunk_id": 100, "document_id": 1, "score": 0.9, "content": "allowed"},
            {"chunk_id": 200, "document_id": 2, "score": 0.8, "content": "private"},
            {"chunk_id": 300, "document_id": 3, "score": 0.7, "content": "other org"},
        ]
        diagnostic.record_retrieval_attempt(limit=15, min_score=0.35, sources=sources)
        diagnostic.selected_document_ids.extend([1, 2, 3])
        diagnostic.record_final_context(sources)
        diagnostic.record_selection(
            decision="retrieval",
            reason="semantic_evidence",
            document_id=1,
        )
        diagnostic.finalize({"grounded": True})
        return {"grounded": True, "sources": sources}

    def test_endpoint_requires_authentication(self) -> None:
        app.dependency_overrides.pop(get_current_user)
        try:
            with patch("app.routes.chat.settings.rag_diagnostics_enabled", True):
                response = self.client.post("/chat/diagnostics", json=self._request())
        finally:
            app.dependency_overrides[get_current_user] = lambda: self.current_user

        self.assertEqual(response.status_code, 401)

    def test_endpoint_is_disabled_by_default(self) -> None:
        with patch("app.routes.chat.settings.rag_diagnostics_enabled", False), patch(
            "app.routes.chat.answer_question"
        ) as answer:
            response = self.client.post("/chat/diagnostics", json=self._request())

        self.assertEqual(response.status_code, 404)
        answer.assert_not_called()

    def test_private_document_cannot_be_diagnosed_by_another_user(self) -> None:
        with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
            "app.routes.chat.answer_question"
        ) as answer:
            response = self.client.post(
                "/chat/diagnostics",
                json=self._request(document_id=2),
            )

        self.assertEqual(response.status_code, 404)
        answer.assert_not_called()

    def test_rate_limit_runs_before_document_authorization(self) -> None:
        with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
            "app.routes.chat.enforce_request_limit",
            side_effect=HTTPException(status_code=429, detail="Rate limit exceeded."),
        ), patch("app.routes.chat.require_document") as require:
            response = self.client.post(
                "/chat/diagnostics",
                json=self._request(document_id=2),
            )

        self.assertEqual(response.status_code, 429)
        require.assert_not_called()

    def test_internal_value_error_is_not_returned(self) -> None:
        secret = "AZURE_OPENAI_API_KEY=private-provider-secret"
        with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
            "app.routes.chat.answer_question",
            side_effect=ValueError(secret),
        ):
            response = self.client.post("/chat/diagnostics", json=self._request())

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "The RAG diagnostic request failed.")
        self.assertNotIn(secret, response.text)

    def test_response_filters_private_and_cross_organization_trace_ids(self) -> None:
        with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
            "app.routes.chat.answer_question",
            side_effect=self._record_mixed_trace,
        ):
            response = self.client.post("/chat/diagnostics", json=self._request())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = response.text
        self.assertEqual(payload["capability"], "rag_diagnostic")
        self.assertTrue(payload["development_capability"])
        self.assertIsNone(payload["query"])
        self.assertEqual(payload["selected_sources"]["selected_document_ids"], [1])
        self.assertEqual(payload["selected_sources"]["source_document_ids"], [1])
        self.assertEqual(payload["retrieved_chunks"]["chunk_ids"], [100])
        self.assertEqual(payload["retrieved_chunks"]["similarity_scores"], [0.9])
        self.assertEqual(payload["final_context_selection"]["chunk_ids"], [100])
        self.assertEqual(payload["retrieved_chunks"]["text_previews"], [])
        for forbidden in (
            "private document 2 content",
            "private document 3 content",
            "AZURE_OPENAI_API_KEY",
            "QDRANT_API_KEY",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_explicit_read_permission_allows_diagnostic_source_metadata(self) -> None:
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO document_permissions
                   (organization_id, document_id, user_id, permission)
                   VALUES ('org-a', 2, 10, 'read')"""
            )

        def record_permitted(*args, **kwargs):
            diagnostic = kwargs["diagnostic"]
            diagnostic.start_request(
                query=str(args[0]),
                conversation_id=None,
                collection_id=None,
                document_id=2,
                version_id=None,
            )
            source = {"chunk_id": 200, "document_id": 2, "score": 0.75}
            diagnostic.record_retrieval_attempt(limit=15, min_score=0.35, sources=[source])
            diagnostic.record_selection(
                decision="retrieval",
                reason="semantic_evidence",
                document_id=2,
            )
            diagnostic.record_final_context([source])
            diagnostic.finalize({"grounded": True})
            return {"grounded": True, "sources": [source]}

        with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
            "app.routes.chat.answer_question",
            side_effect=record_permitted,
        ):
            response = self.client.post(
                "/chat/diagnostics",
                json=self._request(document_id=2),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected_sources"]["source_document_ids"], [2])

    def test_diagnostic_route_disables_conversation_context_persistence(self) -> None:
        with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
            "app.routes.chat.answer_question",
            side_effect=self._record_mixed_trace,
        ) as answer:
            response = self.client.post("/chat/diagnostics", json=self._request())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(answer.call_args.kwargs["persist_context"])

    def test_soft_deleted_document_chunk_or_version_is_filtered(self) -> None:
        mutations = (
            (
                "UPDATE documents SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1",
                "UPDATE documents SET deleted_at = NULL WHERE id = 1",
                "documents",
            ),
            (
                "UPDATE chunks SET deleted_at = CURRENT_TIMESTAMP WHERE id = 100",
                "UPDATE chunks SET deleted_at = NULL WHERE id = 100",
                "chunks",
            ),
            (
                "UPDATE document_versions SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1",
                "UPDATE document_versions SET deleted_at = NULL WHERE id = 1",
                "document_versions",
            ),
        )
        for statement, restore_statement, table in mutations:
            with self.subTest(table=table):
                with database.get_connection() as connection:
                    connection.execute(statement)
                with patch("app.routes.chat.settings.rag_diagnostics_enabled", True), patch(
                    "app.routes.chat.answer_question",
                    side_effect=self._record_mixed_trace,
                ):
                    response = self.client.post("/chat/diagnostics", json=self._request())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["retrieved_chunks"]["chunk_ids"], [])
                with database.get_connection() as connection:
                    connection.execute(restore_statement)


if __name__ == "__main__":
    unittest.main()
