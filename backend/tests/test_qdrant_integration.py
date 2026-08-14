"""Isolated end-to-end checks for SQLite, Qdrant, retrieval, and lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app import database
from app.auth import get_current_user
from app.main import app
from app.routes import ingestion as ingestion_route
from app.services import ingestion_jobs, rag_service, vector_search, vector_store
from app.services.workbooks import STRUCTURED_INDEX_VERSION
from scripts.check_vector_consistency import check_consistency


class QdrantIntegrationTests(unittest.TestCase):
    """Use a real in-memory Qdrant client with an isolated SQLite database."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", root / "integration.db")
        )
        self.stack.enter_context(
            patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
        )
        self.stack.enter_context(
            patch.object(ingestion_route, "UPLOAD_DIRECTORY", root / "uploads")
        )
        self.stack.enter_context(
            patch.object(ingestion_jobs, "UPLOAD_DIRECTORY", root / "uploads")
        )
        for name, value in (
            ("vector_store", "qdrant"),
            ("qdrant_mode", "memory"),
            ("qdrant_url", ""),
            ("qdrant_path", ""),
            ("qdrant_local_path", ""),
            ("qdrant_collection", f"integration_{uuid4().hex}"),
            ("embedding_model_version", "integration-model"),
            ("embedding_dimension", 4),
            ("embedding_batch_size", 2),
            ("vector_store_rollback_dual_write", True),
            ("rag_retrieval_limit", 15),
            ("rag_final_context_limit", 5),
            ("rag_min_score", 0.35),
        ):
            self.stack.enter_context(patch.object(vector_store.settings, name, value))
        vector_store.reset_vector_store_for_tests()
        database.initialize_database()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES ('org-a', 'A')"
            )
            connection.executemany(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (?, ?, 'hash', 'org-a', 'member')""",
                [
                    (10, "user-a@example.com"),
                    (11, "user-b@example.com"),
                ],
            )
        self.current_user = {
            "id": 10,
            "email": "user-a@example.com",
            "organization_id": "org-a",
            "role": "member",
        }
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)
        self.stack.enter_context(
            patch.object(
                ingestion_jobs,
                "create_embeddings",
                side_effect=self._embed,
            )
        )
        self.stack.enter_context(
            patch.object(
                vector_search,
                "create_embeddings",
                side_effect=self._embed,
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        store = getattr(vector_store, "_store", None)
        client = getattr(store, "client", None)
        if client is not None:
            client.close()
        vector_store.reset_vector_store_for_tests()
        self.stack.close()
        self.temporary.cleanup()

    @staticmethod
    def _embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "phone" in lowered:
                vectors.append([-1.0, 0.0, 0.0, 0.0])
            elif "project manager" in lowered:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif "isolated secret" in lowered:
                vectors.append([0.0, 1.0, 0.0, 0.0])
            elif "budget" in lowered:
                vectors.append([0.0, 0.0, 0.0, 1.0])
            else:
                vectors.append([0.0, 0.0, 1.0, 0.0])
        return vectors

    def _upload(
        self,
        *,
        filename: str = "phoenix.txt",
        content: bytes = (
            b"Project Phoenix started on 14 July 2026.\n"
            b"The project manager is Anitha.\n"
            b"The approved budget is INR 4,50,000."
        ),
    ) -> dict[str, object]:
        response = self.client.post(
            "/api/documents/upload",
            files={"file": (filename, content, "text/plain")},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(response.status_code, 202, response.text)
        accepted = response.json()
        self.assertTrue(ingestion_jobs.run_one("integration-worker"))
        job = self.client.get(f"/api/jobs/{accepted['job_id']}").json()
        self.assertEqual(job["status"], "completed", job)
        return accepted

    def test_qdrant_collection_created(self) -> None:
        """Collection should exist with the configured vector size."""
        store = vector_store.get_vector_store()
        status = store.health()
        self.assertEqual(status["collection"], vector_store.settings.qdrant_collection)
        self.assertEqual(status["vector_size"], 4)
        # Embedded Qdrant documents that payload indexes have no effect.
        self.assertEqual(status["payload_indexes"], [])
        with TestClient(app) as started_backend:
            health = started_backend.get("/health")
            self.assertEqual(health.status_code, 200)
            payload = health.json()
            self.assertEqual(payload["status"], "healthy")
            self.assertEqual(payload["database"], "connected")
            self.assertEqual(payload["qdrant"], "connected")
            self.assertEqual(payload["embedding"], {
                "status": "uninitialized",
                "loaded": False,
                "error_type": None,
            })
            self.assertIn(payload["ocr"]["status"], {"ready", "unavailable"})

    def test_upload_indexes_chunks(self) -> None:
        """Successful ingestion should store vectors in Qdrant."""
        accepted = self._upload()
        with database.get_connection() as connection:
            row = connection.execute(
                """SELECT d.processing_status, dc.processing_status AS content_status,
                          c.vector_point_id, c.indexing_status
                   FROM documents d
                   JOIN document_contents dc ON dc.id = d.content_id
                   JOIN chunks c ON c.document_id = d.id
                   WHERE d.id = ?""",
                (accepted["document_id"],),
            ).fetchone()
        self.assertEqual(row["processing_status"], "completed")
        self.assertEqual(row["content_status"], "completed")
        self.assertEqual(row["indexing_status"], "completed")
        self.assertTrue(
            vector_store.get_vector_store().contains_points(
                [str(row["vector_point_id"])]
            )
        )
        consistency = check_consistency("org-a")
        self.assertTrue(consistency["consistent"])
        self.assertEqual(
            consistency["sqlite_indexed_chunks"],
            consistency["vector_store_active_points"],
        )

    def test_readiness_distinguishes_total_and_current_vector_points(self) -> None:
        """Soft-deleted Qdrant points remain stored but are never reported active."""
        accepted = self._upload()
        before_delete = self.client.get("/health/ready").json()["vector_store"]
        self.assertEqual(before_delete["total_points"], 3)
        self.assertEqual(before_delete["active_points"], 3)
        self.assertEqual(before_delete["deleted_or_stale_points"], 0)
        self.assertEqual(before_delete["sqlite_current_chunks"], 3)
        self.assertEqual(before_delete["sync_status"], "in_sync")

        self.assertEqual(self.client.delete(f"/documents/{accepted['document_id']}").status_code, 200)
        after_delete = self.client.get("/health/ready").json()["vector_store"]
        self.assertEqual(after_delete["total_points"], 3)
        self.assertEqual(after_delete["active_points"], 0)
        self.assertEqual(after_delete["deleted_or_stale_points"], 3)
        self.assertEqual(after_delete["sqlite_current_chunks"], 0)
        self.assertEqual(after_delete["sync_status"], "in_sync")

    def test_search_returns_relevant_chunk(self) -> None:
        """A known question should retrieve its source chunk."""
        self._upload()
        results = vector_search.search_chunks(
            "Who is the project manager?",
            owner_id=10,
            organization_id="org-a",
            min_score=0.35,
        )
        self.assertTrue(results)
        self.assertIn("Anitha", str(results[0]["content"]))

    def test_search_respects_owner_filter(self) -> None:
        """Users must not retrieve another user's private vectors."""
        self._upload(
            filename="private.txt",
            content=b"An isolated secret belongs only to User A.",
        )
        owner_results = vector_search.search_chunks(
            "isolated secret",
            owner_id=10,
            organization_id="org-a",
            min_score=0.35,
        )
        other_results = vector_search.search_chunks(
            "isolated secret",
            owner_id=11,
            organization_id="org-a",
            min_score=0.35,
        )
        self.assertTrue(owner_results)
        self.assertEqual(other_results, [])

    def test_identical_content_reuses_vectors(self) -> None:
        """Deleted-content re-upload should reuse vectors and one active result."""
        first = self._upload()
        active_before = vector_store.get_vector_store().list_active_points("org-a")
        self.assertEqual(
            self.client.delete(
                f"/documents/{first['document_id']}"
            ).status_code,
            200,
        )
        with patch.object(
            ingestion_jobs,
            "create_embeddings",
            side_effect=AssertionError("compatible Qdrant vectors must be reused"),
        ):
            second = self._upload(filename="phoenix-copy.txt")
        active = vector_store.get_vector_store().list_active_points("org-a")
        self.assertEqual(len(active), len(active_before))
        self.assertNotEqual(first["document_id"], second["document_id"])

    def test_soft_deleted_document_is_not_retrieved(self) -> None:
        """Deleted documents must be excluded from retrieval."""
        accepted = self._upload()
        self.assertTrue(vector_search.search_chunks(
            "Who is the project manager?",
            owner_id=10,
            organization_id="org-a",
            min_score=0.35,
        ))
        self.assertEqual(
            self.client.delete(
                f"/documents/{accepted['document_id']}"
            ).status_code,
            200,
        )
        self.assertEqual(vector_search.search_chunks(
            "Who is the project manager?",
            owner_id=10,
            organization_id="org-a",
            min_score=0.35,
        ), [])
        with database.get_connection() as connection:
            deleted = connection.execute(
                "SELECT deleted_at FROM documents WHERE id = ?",
                (accepted["document_id"],),
            ).fetchone()
            audit_count = connection.execute(
                """SELECT COUNT(*) FROM audit_events
                   WHERE event_type = 'document.delete'"""
            ).fetchone()[0]
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertGreaterEqual(audit_count, 1)

    def test_no_relevant_result_returns_safe_message(self) -> None:
        """Missing information must not produce hallucinated answers."""
        self._upload()
        with patch.object(
            rag_service,
            "is_analytical_question",
            return_value=False,
        ), patch.object(
            rag_service,
            "generate_answer",
            side_effect=AssertionError("LLM must not run without context"),
        ):
            result = rag_service.answer_question(
                "What is the client's phone number?",
                user_id=10,
            )
        self.assertEqual(
            result["answer"],
            "Information not available in the uploaded files.",
        )
        self.assertFalse(result["grounded"])

    def test_csv_price_range_uses_all_structured_rows(self) -> None:
        fixture = (
            Path(__file__).parent / "fixtures" / "agriculture_dataset.csv"
        ).read_bytes()
        accepted = self._upload(
            filename="agriculture_dataset.csv",
            content=fixture,
        )
        with database.get_connection() as connection:
            structured = connection.execute(
                """SELECT dc.structured_index_status,
                          dc.structured_index_version,
                          dc.structured_indexed_at
                   FROM documents d
                   JOIN document_contents dc ON dc.id = d.content_id
                   WHERE d.id = ?""",
                (accepted["document_id"],),
            ).fetchone()
        self.assertEqual(structured["structured_index_status"], "completed")
        self.assertEqual(
            structured["structured_index_version"],
            STRUCTURED_INDEX_VERSION,
        )
        self.assertIsNotNone(structured["structured_indexed_at"])
        with patch.object(
            rag_service,
            "generate_answer",
            side_effect=AssertionError("structured CSV ranges must not use the LLM"),
        ):
            result = rag_service.answer_question(
                "Show all equipment priced between ₹50,000 and ₹200,000.",
                user_id=10,
                document_id=int(accepted["document_id"]),
            )

        answer = str(result["answer"])
        self.assertIn("Power Tiller", answer)
        self.assertIn("Irrigation Pump", answer)
        self.assertIn("Harvester", answer)
        self.assertNotIn("Seed Drill", answer)
        self.assertNotIn("Tractor", answer)
        self.assertNotIn("Agricultural Drone", answer)
        self.assertTrue(result["grounded"])
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["source_type"], "csv")
        self.assertEqual(result["sources"][0]["source_location"]["row_start"], 4)
        self.assertEqual(result["sources"][0]["source_location"]["row_end"], 6)

    def test_retry_is_idempotent(self) -> None:
        """Retrying ingestion should not create extra points."""
        accepted = self._upload()
        store = vector_store.get_vector_store()
        before = store.health()["points_count"]
        with database.get_connection() as connection:
            connection.execute(
                """UPDATE ingestion_jobs SET status = 'processing'
                   WHERE id = ?""",
                (accepted["job_id"],),
            )
        ingestion_jobs.process_job(str(accepted["job_id"]))
        after = store.health()["points_count"]
        self.assertEqual(after, before)

    def test_sqlite_feature_flag_provides_validated_rollback(self) -> None:
        """Dual-written vectors should remain searchable through SQLite rollback."""
        self._upload()
        qdrant_store = vector_store.get_vector_store()
        with database.get_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM chunks
                       WHERE embedding IS NOT NULL"""
                ).fetchone()[0],
                3,
            )
        qdrant_store.client.close()
        with patch.object(vector_store.settings, "vector_store", "sqlite"):
            vector_store.reset_vector_store_for_tests()
            results = vector_search.search_chunks(
                "Who is the project manager?",
                owner_id=10,
                organization_id="org-a",
                min_score=0.35,
            )
            other_user_results = vector_search.search_chunks(
                "Who is the project manager?",
                owner_id=11,
                organization_id="org-a",
                min_score=0.35,
            )
            health = self.client.get("/health").json()
        self.assertTrue(results)
        self.assertEqual(other_user_results, [])
        self.assertIn("Anitha", str(results[0]["content"]))
        self.assertEqual(health["qdrant"], "standby")

    def test_qdrant_search_failure_returns_safe_error(self) -> None:
        """Provider failures should not expose internals or fabricate an answer."""
        self._upload()
        store = vector_store.get_vector_store()
        with patch.object(
            store,
            "search",
            side_effect=ConnectionError("private Qdrant connection detail"),
        ):
            response = self.client.post(
                "/chat",
                json={"question": "Who is the project manager?"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "The AI answer service is unavailable.",
        )
        self.assertNotIn("private Qdrant", response.text)


if __name__ == "__main__":
    unittest.main()
