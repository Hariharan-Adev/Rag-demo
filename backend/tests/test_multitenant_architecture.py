"""End-to-end contract tests for tenant, job, version, ACL, and lifecycle behavior."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.auth import get_current_user
from app.main import app
from app.services import ingestion_jobs, vector_search
from app.services.rag_service import answer_question
from app.services.source_extraction import SourceChunk
from app.services.vector_store import VectorPoint, VectorStore


class FakeVectorStore(VectorStore):
    def __init__(self) -> None:
        self.points: dict[str, VectorPoint] = {}

    def upsert(self, points: list[VectorPoint]) -> None:
        self.points.update({point.point_id: point for point in points})

    def search(
        self,
        vector: list[float],
        *,
        organization_id: str,
        user_id: int,
        current_version_ids: list[int],
        limit: int,
        document_id: int | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, object]]:
        matches = [
            {
                "chunk_id": point.chunk_id,
                "document_id": point.document_id,
                "version_id": point.version_id,
                "chunk_index": point.chunk_index,
                "text": point.text,
                "filename": point.filename,
                "source_type": point.source_type,
                "source_location": point.source_location,
                "score": 1.0,
            }
            for point in self.points.values()
            if point.organization_id == organization_id
            and not point.deleted
            and point.version_id in current_version_ids
            and (document_id is None or point.document_id == document_id)
        ]
        if score_threshold is not None:
            matches = [
                match for match in matches
                if float(match["score"]) >= score_threshold
            ]
        return matches[:limit]

    def contains_points(self, point_ids: list[str]) -> bool:
        return all(point_id in self.points for point_id in point_ids)

    def get_vectors(self, point_ids: list[str]) -> dict[str, list[float]]:
        return {
            point_id: self.points[point_id].vector
            for point_id in point_ids
            if point_id in self.points
        }

    def set_document_deleted(
        self, organization_id: str, document_id: int, deleted: bool
    ) -> None:
        self.points = {
            key: VectorPoint(**{
                **point.__dict__,
                "deleted": deleted,
            }) if point.organization_id == organization_id
            and point.document_id == document_id else point
            for key, point in self.points.items()
        }

    def set_document_visibility(
        self, organization_id: str, document_id: int, visibility: str
    ) -> None:
        self.points = {
            key: VectorPoint(**{
                **point.__dict__,
                "visibility": visibility,
            }) if point.organization_id == organization_id
            and point.document_id == document_id else point
            for key, point in self.points.items()
        }

    def set_version_deleted(
        self,
        organization_id: str,
        document_id: int,
        version_id: int,
        deleted: bool,
    ) -> None:
        self.points = {
            key: VectorPoint(**{
                **point.__dict__,
                "deleted": deleted,
            }) if point.organization_id == organization_id
            and point.document_id == document_id
            and point.version_id == version_id else point
            for key, point in self.points.items()
        }

    def delete_document(self, organization_id: str, document_id: int) -> None:
        self.points = {
            key: point for key, point in self.points.items()
            if not (
                point.organization_id == organization_id
                and point.document_id == document_id
            )
        }

    def clear(self, organization_id: str | None = None) -> None:
        self.points = {
            key: point for key, point in self.points.items()
            if organization_id is not None and point.organization_id != organization_id
        }

    def health(self) -> dict[str, object]:
        return {"provider": "fake", "mode": "test", "status": "ok"}


class MultitenantArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db_patch = patch.object(database, "DATABASE_PATH", root / "rag.db")
        self.upload_patch = patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
        self.db_patch.start()
        self.upload_patch.start()
        self.route_upload_patch = patch(
            "app.routes.ingestion.UPLOAD_DIRECTORY", root / "uploads"
        )
        self.job_upload_patch = patch.object(
            ingestion_jobs, "UPLOAD_DIRECTORY", root / "uploads"
        )
        self.route_upload_patch.start()
        self.job_upload_patch.start()
        database.initialize_database()
        self.fake_store = FakeVectorStore()
        self.store_patches = [
            patch("app.services.ingestion_jobs.get_vector_store", return_value=self.fake_store),
            patch("app.services.vector_search.get_vector_store", return_value=self.fake_store),
            patch("app.routes.documents.get_vector_store", return_value=self.fake_store),
        ]
        for item in self.store_patches:
            item.start()
        self.embedding_patch = patch.object(
            ingestion_jobs,
            "create_embeddings",
            side_effect=lambda texts: [[1.0] + [0.0] * 383 for _ in texts],
        )
        self.query_embedding_patch = patch.object(
            vector_search, "create_embeddings", return_value=[[1.0] + [0.0] * 383]
        )
        self.embedding_patch.start()
        self.query_embedding_patch.start()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                [("org-a", "A"), ("org-b", "B")],
            )
            connection.executemany(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (?, ?, 'hash', ?, ?)""",
                [
                    (10, "owner@example.com", "org-a", "organization_admin"),
                    (11, "reader@example.com", "org-a", "member"),
                    (20, "owner-b@example.com", "org-b", "organization_admin"),
                ],
            )
        self.current_user = {
            "id": 10,
            "email": "owner@example.com",
            "organization_id": "org-a",
            "role": "organization_admin",
        }
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.query_embedding_patch.stop()
        self.embedding_patch.stop()
        for item in reversed(self.store_patches):
            item.stop()
        self.job_upload_patch.stop()
        self.route_upload_patch.stop()
        self.upload_patch.stop()
        self.db_patch.stop()
        self.temporary.cleanup()

    def upload(self, content: bytes, key: str):
        return self.upload_named("policy.txt", content, key)

    def upload_named(self, filename: str, content: bytes, key: str):
        return self.client.post(
            "/api/documents/upload",
            files={"file": (filename, content, "application/octet-stream")},
            headers={"Idempotency-Key": key},
        )

    def upload_version(self, document_id: int, content: bytes, key: str):
        return self.client.post(
            f"/api/documents/{document_id}/versions",
            files={"file": ("policy.txt", content, "text/plain")},
            headers={"Idempotency-Key": key},
        )

    def test_schema_has_tenant_lifecycle_and_source_metadata(self) -> None:
        tenant_tables = {
            "users", "documents", "document_versions", "document_contents",
            "chunks", "ingestion_jobs", "audit_events", "llm_usage",
            "rate_limit_windows", "chat_sessions", "chat_messages",
            "document_permissions",
        }
        with database.get_connection() as connection:
            for table in tenant_tables:
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertIn("organization_id", columns, table)
            version_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(document_versions)")
            }
            self.assertTrue({
                "storage_key", "mime_type", "file_size", "ingestion_status",
                "extraction_status", "indexing_status", "deleted_at", "deleted_by",
                "source_metadata_json",
            }.issubset(version_columns))
            chunk_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(chunks)")
            }
            self.assertTrue({
                "source_type", "source_location_json", "token_count",
                "vector_point_id", "embedding_model", "embedding_dimension",
                "qdrant_indexed_at", "indexing_status", "deleted_at", "deleted_by",
            }.issubset(chunk_columns))
            content_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(document_contents)"
                )
            }
            self.assertTrue({
                "structured_index_status", "structured_index_version",
                "structured_indexed_at", "structured_index_error",
            }.issubset(content_columns))
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ingestion_jobs)")
            }
            self.assertTrue({
                "request_idempotency_key", "pipeline_version", "next_retry_at",
                "last_error_code", "last_error_message",
            }.issubset(job_columns))
            for table in ("document_versions", "document_contents", "chunks"):
                self.assertIn(
                    "deleted_with_document",
                    {
                        row["name"]
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    },
                )
            indexes = {
                row["name"]: row["sql"]
                for row in connection.execute(
                    """SELECT name, sql FROM sqlite_master
                       WHERE type = 'index'"""
                )
            }
            self.assertNotIn("idx_document_contents_owner_content_hash", indexes)
            self.assertNotIn("idx_chunks_content_chunk_index", indexes)
            self.assertIn(
                "WHERE deleted_at IS NULL",
                indexes["idx_document_contents_owner_active_content_hash"],
            )
            self.assertIn("idx_chunks_content_version_index", indexes)
            self.assertIn("ux_chunks_vector_point_id", indexes)
            self.assertIn("idx_contents_structured_retry", indexes)
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM schema_migrations
                   WHERE version = '008_active_content_indexes'"""
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM schema_migrations
                   WHERE version = '009_chunk_vector_sync'"""
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM schema_migrations
                   WHERE version = '010_structured_csv_indexing'"""
            ).fetchone())

    def test_active_content_index_migration_is_restart_safe(self) -> None:
        database.initialize_database()
        database.initialize_database()
        with database.get_connection() as connection:
            indexes = {
                row["name"]: row["sql"]
                for row in connection.execute(
                    """SELECT name, sql FROM sqlite_master
                       WHERE type = 'index'"""
                )
            }
            self.assertNotIn("idx_document_contents_owner_content_hash", indexes)
            self.assertNotIn("idx_chunks_content_chunk_index", indexes)
            self.assertIn(
                "WHERE deleted_at IS NULL",
                indexes["idx_document_contents_owner_active_content_hash"],
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_active_content_index_migration_rejects_ambiguous_active_duplicates(self) -> None:
        with database.get_connection() as connection:
            connection.execute(
                "DROP INDEX idx_document_contents_owner_active_content_hash"
            )
            connection.executemany(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text, processing_status)
                   VALUES (10, 'org-a', ?, 'conflict', 'conflict', 'completed')""",
                [("one",), ("two",)],
            )
        with self.assertRaisesRegex(
            RuntimeError, "active duplicate content identities"
        ):
            database.initialize_database()

    def test_lifecycle_repair_checks_schema_even_when_old_migration_was_recorded(self) -> None:
        with database.get_connection() as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = '007_lifecycle_repair'"
            )
            for table in ("document_versions", "document_contents", "chunks"):
                connection.execute(
                    f"ALTER TABLE {table} DROP COLUMN deleted_with_document"
                )
        database.initialize_database()
        with database.get_connection() as connection:
            for table in ("document_versions", "document_contents", "chunks"):
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertIn("deleted_with_document", columns)
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM schema_migrations
                   WHERE version = '007_lifecycle_repair'"""
            ).fetchone())

    def test_queued_idempotent_versioned_tenant_acl_and_soft_delete_flow(self) -> None:
        accepted = self.upload(b"first line\nsecond line", "upload-v1")
        self.assertEqual(accepted.status_code, 202)
        first = accepted.json()
        self.assertEqual(first["status"], "queued")
        with database.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
            job_row = connection.execute(
                """SELECT idempotency_key, request_idempotency_key,
                          pipeline_version
                   FROM ingestion_jobs WHERE id = ?""",
                (first["job_id"],),
            ).fetchone()
            self.assertEqual(
                job_row["idempotency_key"],
                f"org-a:{first['version_id']}:document_ingestion:v1",
            )
            self.assertEqual(job_row["request_idempotency_key"], "upload-v1")
            self.assertEqual(job_row["pipeline_version"], "v1")

        duplicate = self.upload(b"first line\nsecond line", "upload-v1")
        self.assertEqual(duplicate.json()["job_id"], first["job_id"])
        self.assertTrue(ingestion_jobs.run_one("worker-test"))
        job = self.client.get(f"/api/jobs/{first['job_id']}").json()
        self.assertEqual(job["status"], "completed")
        with database.get_connection() as connection:
            locations = [
                row["source_location_json"]
                for row in connection.execute(
                    "SELECT source_location_json FROM chunks ORDER BY chunk_index"
                )
            ]
        self.assertIn('"line_start": 1', locations[0])

        second = self.upload_version(
            first["document_id"], b"replacement content", "upload-v2"
        ).json()
        self.assertEqual(second["document_id"], first["document_id"])
        self.assertNotEqual(second["version_id"], first["version_id"])
        self.assertTrue(ingestion_jobs.run_one("worker-test"))
        versions = self.client.get(
            f"/documents/{first['document_id']}/versions"
        ).json()["versions"]
        self.assertEqual(len(versions), 2)
        current = next(item for item in versions if item["id"] == second["version_id"])
        self.assertTrue(current["is_current"])
        self.assertEqual(current["ingestion_status"], "completed")
        self.assertEqual(current["extraction_status"], "completed")
        self.assertEqual(current["indexing_status"], "completed")
        self.assertEqual(current["mime_type"], "text/plain")
        self.assertEqual(current["file_size"], len(b"replacement content"))
        old_results = vector_search.search_chunks(
            "first",
            10,
            document_id=first["document_id"],
            organization_id="org-a",
            version_id=first["version_id"],
        )
        self.assertEqual(old_results[0]["version_id"], first["version_id"])
        self.assertEqual(
            vector_search.search_chunks(
                "first",
                20,
                document_id=first["document_id"],
                organization_id="org-b",
                version_id=first["version_id"],
            ),
            [],
        )

        switched = self.client.post(
            f"/documents/{first['document_id']}/versions/{first['version_id']}/make-current"
        )
        self.assertEqual(switched.status_code, 200)
        results = vector_search.search_chunks("first", 10, organization_id="org-a")
        self.assertEqual(results[0]["version_id"], first["version_id"])
        deleted_version = self.client.delete(
            f"/documents/{first['document_id']}/versions/{second['version_id']}"
        )
        self.assertEqual(deleted_version.status_code, 200)
        self.assertEqual(
            self.client.delete(
                f"/documents/{first['document_id']}/versions/{first['version_id']}"
            ).status_code,
            409,
        )
        with database.get_connection() as connection:
            deleted_row = connection.execute(
                "SELECT deleted_by FROM document_versions WHERE id = ?",
                (second["version_id"],),
            ).fetchone()
            self.assertEqual(deleted_row["deleted_by"], 10)

        self.current_user = {
            "id": 20, "email": "owner-b@example.com",
            "organization_id": "org-b", "role": "organization_admin",
        }
        self.assertEqual(self.client.get("/documents").json()["documents"], [])
        self.assertEqual(
            self.client.get(f"/api/jobs/{first['job_id']}").status_code, 404
        )

        self.current_user = {
            "id": 11, "email": "reader@example.com",
            "organization_id": "org-a", "role": "member",
        }
        self.assertEqual(self.client.get("/documents").json()["documents"], [])
        self.current_user = {
            "id": 10, "email": "owner@example.com",
            "organization_id": "org-a", "role": "organization_admin",
        }
        self.assertEqual(self.client.post(
            f"/documents/{first['document_id']}/shares",
            json={"user_id": 11, "permission": "read"},
        ).status_code, 201)
        self.current_user = {
            "id": 11, "email": "reader@example.com",
            "organization_id": "org-a", "role": "member",
        }
        self.assertEqual(
            len(self.client.get("/documents").json()["documents"]), 1
        )

        self.current_user = {
            "id": 10, "email": "owner@example.com",
            "organization_id": "org-a", "role": "organization_admin",
        }
        self.assertEqual(
            self.client.delete(f"/documents/{first['document_id']}").status_code, 200
        )
        self.assertEqual(vector_search.search_chunks("first", 10, organization_id="org-a"), [])
        self.assertEqual(len(self.client.get("/documents/trash").json()["documents"]), 1)
        self.assertEqual(
            self.client.post(f"/documents/{first['document_id']}/restore").status_code,
            200,
        )
        self.assertEqual(len(self.client.get("/documents").json()["documents"]), 1)
        with database.get_connection() as connection:
            manually_deleted = connection.execute(
                "SELECT deleted_at FROM document_versions WHERE id = ?",
                (second["version_id"],),
            ).fetchone()
            self.assertIsNotNone(manually_deleted["deleted_at"])

    def test_job_failure_retry_and_cancellation_are_explicit(self) -> None:
        cancelled = self.upload(b"cancel me", "cancel-job").json()
        response = self.client.post(f"/api/jobs/{cancelled['job_id']}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/jobs/{cancelled['job_id']}").json()["status"],
            "cancelled",
        )

        failed = self.upload(b"retry me", "failed-job").json()
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE ingestion_jobs SET max_attempts = 1 WHERE id = ?",
                (failed["job_id"],),
            )
        with patch.object(
            ingestion_jobs,
            "extract_source_chunks",
            side_effect=ValueError("safe parser failure"),
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-failure"))
        failure = self.client.get(f"/api/jobs/{failed['job_id']}").json()
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["error"]["code"], "validation_failed")
        self.assertIn("safe parser failure", failure["error"]["message"])
        self.assertEqual(failure["attempt_count"], 1)

        retried = self.client.post(f"/api/jobs/{failed['job_id']}/retry")
        self.assertEqual(retried.status_code, 202)
        self.assertTrue(ingestion_jobs.run_one("worker-retry"))
        self.assertEqual(
            self.client.get(f"/api/jobs/{failed['job_id']}").json()["status"],
            "completed",
        )

    def test_duplicate_versioning_reuse_and_storage_are_tenant_safe(self) -> None:
        first = self.upload(b"alpha", "duplicate-v1").json()
        self.assertTrue(ingestion_jobs.run_one("worker-duplicate"))
        self.assertEqual(
            self.client.get(f"/ingestion-jobs/{first['job_id']}").json()["status"],
            "completed",
        )

        rejected = self.client.post(
            "/documents/upload",
            files={"file": ("policy.txt", b"alpha", "text/plain")},
        )
        self.assertEqual(rejected.status_code, 409)

        changed = self.client.post(
            "/documents/upload",
            files={"file": ("policy.txt", b"beta", "text/plain")},
            headers={"Idempotency-Key": "duplicate-v2"},
        )
        self.assertEqual(changed.status_code, 202)
        self.assertEqual(changed.json()["document_id"], first["document_id"])
        self.assertTrue(ingestion_jobs.run_one("worker-duplicate"))

        explicit = self.upload_version(
            first["document_id"], b"beta", "duplicate-v3-explicit"
        )
        self.assertEqual(explicit.status_code, 202)
        self.assertTrue(ingestion_jobs.run_one("worker-duplicate"))

        alias = self.client.post(
            "/documents/upload",
            files={"file": ("policy-copy.txt", b"beta", "text/plain")},
            headers={"Idempotency-Key": "duplicate-alias"},
        )
        self.assertEqual(alias.status_code, 202)
        self.assertNotEqual(alias.json()["document_id"], first["document_id"])
        self.assertTrue(ingestion_jobs.run_one("worker-duplicate"))
        duplicate_job = self.client.get(
            f"/api/jobs/{alias.json()['job_id']}"
        ).json()
        self.assertEqual(duplicate_job["status"], "failed")
        self.assertEqual(
            duplicate_job["error"]["code"], "DOCUMENT_ALREADY_EXISTS"
        )
        self.assertFalse(duplicate_job["error"]["retryable"])

        with database.get_connection() as connection:
            org_a_versions = connection.execute(
                """SELECT document_id, content_id, storage_key
                   FROM document_versions
                   WHERE organization_id = 'org-a'
                     AND status = 'completed' AND deleted_at IS NULL
                   ORDER BY id"""
            ).fetchall()
            beta_content_ids = {
                row["content_id"] for row in org_a_versions[1:]
            }
            self.assertEqual(len(beta_content_ids), 1)
            self.assertTrue(all("/" in row["storage_key"] for row in org_a_versions))
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(DISTINCT document_id) FROM chunks
                       WHERE organization_id = 'org-a' AND content_id = ?
                         AND deleted_at IS NULL""",
                    (next(iter(beta_content_ids)),),
                ).fetchone()[0],
                1,
            )

        self.current_user = {
            "id": 20,
            "email": "owner-b@example.com",
            "organization_id": "org-b",
            "role": "organization_admin",
        }
        other = self.client.post(
            "/documents/upload",
            files={"file": ("other.txt", b"beta", "text/plain")},
            headers={"Idempotency-Key": "org-b-copy"},
        )
        self.assertEqual(other.status_code, 202)
        self.assertTrue(ingestion_jobs.run_one("worker-duplicate"))
        with database.get_connection() as connection:
            org_b_content = connection.execute(
                """SELECT content_id, storage_key FROM document_versions
                   WHERE id = ?""",
                (other.json()["version_id"],),
            ).fetchone()
            self.assertNotIn(org_b_content["content_id"], beta_content_ids)
            self.assertNotEqual(
                org_b_content["storage_key"].split("/", 1)[0],
                org_a_versions[0]["storage_key"].split("/", 1)[0],
            )
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn(
            'rag_ingestion_stage_duration_ms{organization_id="org-a",stage="extraction"}',
            metrics.text,
        )
        self.assertIn(
            'rag_chunks_created_total{organization_id="org-b"}',
            metrics.text,
        )

    def test_deleted_content_reupload_reuses_embeddings_and_is_searchable(self) -> None:
        first = self.upload(b"restorable policy", "reupload-original").json()
        self.assertTrue(ingestion_jobs.run_one("worker-reupload-original"))
        with database.get_connection() as connection:
            original = connection.execute(
                """SELECT dv.content_id, c.embedding, c.vector_point_id
                   FROM document_versions dv
                   JOIN chunks c ON c.version_id = dv.id
                   WHERE dv.id = ?""",
                (first["version_id"],),
            ).fetchone()
        self.assertEqual(
            self.client.delete(f"/documents/{first['document_id']}").status_code,
            200,
        )

        second = self.upload(
            b"restorable policy", "reupload-replacement"
        ).json()
        with patch.object(
            ingestion_jobs,
            "create_embeddings",
            side_effect=AssertionError("valid deleted embeddings must be reused"),
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-reupload-replacement"))
        completed = self.client.get(f"/api/jobs/{second['job_id']}").json()
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["reused_deleted_content"])

        with database.get_connection() as connection:
            replacement = connection.execute(
                """SELECT dv.content_id, c.embedding, c.vector_point_id
                   FROM document_versions dv
                   JOIN chunks c ON c.version_id = dv.id
                   WHERE dv.id = ?""",
                (second["version_id"],),
            ).fetchone()
            self.assertEqual(replacement["content_id"], original["content_id"])
            self.assertEqual(replacement["embedding"], original["embedding"])
            self.assertNotEqual(
                replacement["vector_point_id"], original["vector_point_id"]
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM chunks
                       WHERE version_id = ? AND deleted_at IS NULL""",
                    (second["version_id"],),
                ).fetchone()[0],
                1,
            )
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM audit_events
                   WHERE event_type = 'document.reupload.reused'
                     AND job_id = ?""",
                (second["job_id"],),
            ).fetchone())

        before = len(self.fake_store.points)
        ingestion_jobs.process_job(second["job_id"])
        self.assertEqual(len(self.fake_store.points), before)
        self.fake_store.points = {
            key: VectorPoint(**{**point.__dict__, "text": "forged vector payload"})
            for key, point in self.fake_store.points.items()
        }
        results = vector_search.search_chunks(
            "restorable", 10, organization_id="org-a"
        )
        self.assertEqual({row["document_id"] for row in results}, {second["document_id"]})
        self.assertEqual(results[0]["content"], "restorable policy")
        with patch(
            "app.services.rag_service.generate_answer",
            return_value={
                "answer": "Grounded answer",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            },
        ), patch("app.services.rag_service.reserve_groq_call"), patch(
            "app.services.rag_service.record_groq_tokens"
        ):
            chat = answer_question(
                "What is the policy?",
                10,
                document_id=second["document_id"],
            )
        self.assertEqual(chat["sources"][0]["document_id"], second["document_id"])
        restore_conflict = self.client.post(
            f"/documents/{first['document_id']}/restore"
        )
        self.assertEqual(restore_conflict.status_code, 409)
        self.assertEqual(
            restore_conflict.json()["detail"]["code"], "DOCUMENT_ALREADY_EXISTS"
        )

    def test_pending_chunks_commit_before_embeddings_and_keep_text_authoritative(self) -> None:
        accepted = self.upload(
            b"authoritative sqlite chunk text", "pending-before-embedding"
        ).json()

        def embed_after_pending_commit(texts: list[str]) -> list[list[float]]:
            with database.get_connection() as connection:
                rows = connection.execute(
                    """SELECT text, embedding, indexing_status
                       FROM chunks WHERE version_id = ?""",
                    (accepted["version_id"],),
                ).fetchall()
                content_status = connection.execute(
                    """SELECT dc.processing_status
                       FROM document_versions dv
                       JOIN document_contents dc ON dc.id = dv.content_id
                       WHERE dv.id = ?""",
                    (accepted["version_id"],),
                ).fetchone()["processing_status"]
            self.assertEqual([row["text"] for row in rows], texts)
            self.assertTrue(all(row["embedding"] is None for row in rows))
            self.assertTrue(all(row["indexing_status"] == "pending" for row in rows))
            self.assertEqual(content_status, "processing")
            return [[1.0] + [0.0] * 383 for _ in texts]

        with patch.object(
            ingestion_jobs,
            "create_embeddings",
            side_effect=embed_after_pending_commit,
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-pending-first"))

        with database.get_connection() as connection:
            row = connection.execute(
                """SELECT c.embedding, c.indexing_status, c.qdrant_indexed_at,
                          dc.processing_status
                   FROM chunks c
                   JOIN document_contents dc ON dc.id = c.content_id
                   WHERE c.version_id = ?""",
                (accepted["version_id"],),
            ).fetchone()
        self.assertIsNone(row["embedding"])
        self.assertEqual(row["indexing_status"], "completed")
        self.assertIsNotNone(row["qdrant_indexed_at"])
        self.assertEqual(row["processing_status"], "completed")

    def test_active_duplicate_is_terminal_and_cross_user_content_is_isolated(self) -> None:
        first = self.upload(b"owner scoped content", "active-owner").json()
        self.assertTrue(ingestion_jobs.run_one("worker-active-owner"))
        same_name = self.upload_named(
            "policy.txt", b"owner scoped content", "active-same-name"
        )
        self.assertEqual(same_name.status_code, 409)
        self.assertEqual(
            same_name.json()["detail"]["code"], "DOCUMENT_ALREADY_EXISTS"
        )

        alias = self.upload_named(
            "alias.txt", b"owner scoped content", "active-alias"
        ).json()
        with patch.object(
            ingestion_jobs,
            "create_embeddings",
            side_effect=AssertionError("active duplicates must fail before embedding"),
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-active-alias"))
        alias_job = self.client.get(f"/api/jobs/{alias['job_id']}").json()
        self.assertEqual(alias_job["error"]["code"], "DOCUMENT_ALREADY_EXISTS")
        self.assertFalse(alias_job["error"]["retryable"])
        retry = self.client.post(f"/api/jobs/{alias['job_id']}/retry")
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(
            retry.json()["detail"]["code"], "DOCUMENT_ALREADY_EXISTS"
        )
        with database.get_connection() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT deleted_at FROM documents WHERE id = ?",
                (alias["document_id"],),
            ).fetchone()["deleted_at"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM chunks WHERE document_id = ?",
                    (alias["document_id"],),
                ).fetchone()[0],
                0,
            )

        self.current_user = {
            "id": 11,
            "email": "reader@example.com",
            "organization_id": "org-a",
            "role": "member",
        }
        self.assertEqual(
            self.client.get(f"/api/jobs/{first['job_id']}").status_code, 404
        )
        other_owner = self.upload_named(
            "other-owner.txt", b"owner scoped content", "other-owner"
        ).json()
        self.assertTrue(ingestion_jobs.run_one("worker-other-owner"))
        self.assertEqual(
            self.client.get(f"/api/jobs/{other_owner['job_id']}").json()["status"],
            "completed",
        )
        self.current_user = {
            "id": 10,
            "email": "owner@example.com",
            "organization_id": "org-a",
            "role": "organization_admin",
        }
        self.assertEqual(
            self.client.get(f"/api/jobs/{other_owner['job_id']}").status_code,
            404,
        )
        with database.get_connection() as connection:
            content_ids = {
                row["content_id"]
                for row in connection.execute(
                    """SELECT content_id FROM document_versions
                       WHERE id IN (?, ?)""",
                    (first["version_id"], other_owner["version_id"]),
                )
            }
        self.assertEqual(len(content_ids), 2)

    def test_deleted_reupload_supported_format_matrix(self) -> None:
        def office_bytes(marker: str) -> bytes:
            output = BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("marker.txt", marker)
            return output.getvalue()

        fixtures = {
            ".txt": b"format txt",
            ".pdf": b"%PDF-1.4\nformat pdf",
            ".docx": office_bytes("format docx"),
            ".xlsx": office_bytes("format xlsx"),
            ".pptx": office_bytes("format pptx"),
            ".png": b"\x89PNG\r\n\x1a\nformat image",
        }

        def extracted(path: Path):
            text = sha256(path.read_bytes()).hexdigest()
            return [
                SourceChunk(
                    text=text,
                    source_type="text",
                    location={"line_start": 1, "line_end": 1},
                )
            ], {}, None

        with patch.object(
            ingestion_jobs, "_extract_bundle", side_effect=extracted
        ), patch("app.routes.ingestion.enforce_request_limit"):
            for extension, content in fixtures.items():
                with self.subTest(extension=extension):
                    first = self.upload_named(
                        f"original{extension}",
                        content,
                        f"format-original-{extension}",
                    ).json()
                    self.assertTrue(ingestion_jobs.run_one("worker-format-original"))
                    self.assertEqual(
                        self.client.delete(
                            f"/documents/{first['document_id']}"
                        ).status_code,
                        200,
                    )
                    second = self.upload_named(
                        f"renamed{extension}",
                        content,
                        f"format-reupload-{extension}",
                    ).json()
                    with patch.object(
                        ingestion_jobs,
                        "create_embeddings",
                        side_effect=AssertionError(
                            "format re-upload must reuse embeddings"
                        ),
                    ):
                        self.assertTrue(
                            ingestion_jobs.run_one("worker-format-reupload")
                        )
                    job = self.client.get(
                        f"/api/jobs/{second['job_id']}"
                    ).json()
                    self.assertEqual(job["status"], "completed")
                    self.assertTrue(job["result"]["reused_deleted_content"])

    def test_deleted_reupload_uses_qdrant_when_sqlite_embedding_is_missing(self) -> None:
        first = self.upload(b"rebuild missing embedding", "missing-embedding").json()
        self.assertTrue(ingestion_jobs.run_one("worker-missing-original"))
        self.assertEqual(
            self.client.delete(f"/documents/{first['document_id']}").status_code,
            200,
        )
        with database.get_connection() as connection:
            original_content_id = connection.execute(
                "SELECT content_id FROM document_versions WHERE id = ?",
                (first["version_id"],),
            ).fetchone()["content_id"]
            connection.execute(
                "UPDATE chunks SET embedding = NULL WHERE version_id = ?",
                (first["version_id"],),
            )

        second = self.upload(
            b"rebuild missing embedding", "missing-embedding-reupload"
        ).json()
        embedded_batches: list[list[str]] = []

        def regenerate(texts: list[str]) -> list[list[float]]:
            embedded_batches.append(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

        with patch.object(
            ingestion_jobs, "create_embeddings", side_effect=regenerate
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-missing-reupload"))

        completed = self.client.get(f"/api/jobs/{second['job_id']}").json()
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["reused_deleted_content"])
        self.assertEqual(embedded_batches, [])
        with database.get_connection() as connection:
            rebuilt = connection.execute(
                """SELECT dv.content_id, c.embedding
                   FROM document_versions dv
                   JOIN chunks c ON c.version_id = dv.id
                   WHERE dv.id = ?""",
                (second["version_id"],),
            ).fetchone()
        self.assertEqual(rebuilt["content_id"], original_content_id)
        self.assertIsNone(rebuilt["embedding"])

    def test_deleted_reupload_missing_vectors_regenerates_embeddings_safely(self) -> None:
        first = self.upload(b"vector recovery", "vector-original").json()
        self.assertTrue(ingestion_jobs.run_one("worker-vector-original"))
        self.assertEqual(
            self.client.delete(f"/documents/{first['document_id']}").status_code,
            200,
        )
        second = self.upload(b"vector recovery", "vector-reupload").json()
        with patch.object(
            ingestion_jobs,
            "create_embeddings",
            side_effect=AssertionError("deleted embeddings must be reused"),
        ), patch.object(
            self.fake_store,
            "upsert",
            side_effect=ConnectionError("qdrant temporarily unavailable"),
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-vector-failure"))
        scheduled = self.client.get(f"/api/jobs/{second['job_id']}").json()
        self.assertEqual(scheduled["status"], "retry_scheduled")
        with database.get_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT DISTINCT indexing_status FROM chunks
                       WHERE version_id = ?""",
                    (second["version_id"],),
                ).fetchone()["indexing_status"],
                "failed",
            )
            connection.execute(
                """UPDATE ingestion_jobs
                   SET available_at = CURRENT_TIMESTAMP,
                       next_retry_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (second["job_id"],),
            )
        regenerated: list[list[str]] = []

        def regenerate(texts: list[str]) -> list[list[float]]:
            regenerated.append(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

        with patch.object(
            ingestion_jobs, "create_embeddings", side_effect=regenerate
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-vector-resume"))
        completed = self.client.get(f"/api/jobs/{second['job_id']}").json()
        self.assertEqual(completed["status"], "completed")
        with database.get_connection() as connection:
            rows = [
                row
                for row in connection.execute(
                    """SELECT vector_point_id, indexing_status, qdrant_indexed_at
                       FROM chunks WHERE version_id = ?""",
                    (second["version_id"],),
                )
            ]
        point_ids = [row["vector_point_id"] for row in rows]
        self.assertTrue(all(row["indexing_status"] == "completed" for row in rows))
        self.assertTrue(all(row["qdrant_indexed_at"] for row in rows))
        self.assertEqual(len(regenerated), 1)
        self.assertTrue(self.fake_store.contains_points(point_ids))

    def test_transient_vector_failure_resumes_existing_chunks_with_backoff(self) -> None:
        accepted = self.upload(b"recover without re-extracting", "resume-job").json()
        with patch.object(
            self.fake_store,
            "upsert",
            side_effect=ConnectionError("qdrant temporarily unavailable"),
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-transient"))
        scheduled = self.client.get(f"/api/jobs/{accepted['job_id']}").json()
        self.assertEqual(scheduled["status"], "retry_scheduled")
        self.assertEqual(
            scheduled["error"]["code"], "dependency_unavailable"
        )
        with database.get_connection() as connection:
            row = connection.execute(
                """SELECT available_at, next_retry_at, attempt_count
                   FROM ingestion_jobs WHERE id = ?""",
                (accepted["job_id"],),
            ).fetchone()
            self.assertEqual(row["available_at"], row["next_retry_at"])
            self.assertEqual(row["attempt_count"], 1)
            connection.execute(
                """UPDATE ingestion_jobs
                   SET available_at = CURRENT_TIMESTAMP,
                       next_retry_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (accepted["job_id"],),
            )
        regenerated: list[list[str]] = []

        def regenerate(texts: list[str]) -> list[list[float]]:
            regenerated.append(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

        with patch.object(
            ingestion_jobs, "create_embeddings", side_effect=regenerate
        ):
            self.assertTrue(ingestion_jobs.run_one("worker-resume"))
        completed = self.client.get(f"/api/jobs/{accepted['job_id']}").json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["attempt_count"], 2)
        self.assertEqual(len(regenerated), 1)
        with database.get_connection() as connection:
            chunk = connection.execute(
                """SELECT embedding, indexing_status, qdrant_indexed_at
                   FROM chunks WHERE document_id = ?""",
                (accepted["document_id"],),
            ).fetchone()
        self.assertIsNone(chunk["embedding"])
        self.assertEqual(chunk["indexing_status"], "completed")
        self.assertIsNotNone(chunk["qdrant_indexed_at"])

    def test_archive_request_only_enqueues_and_worker_fans_out_child_jobs(self) -> None:
        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("folder/one.txt", "first archived document")
            archive.writestr("folder/two.txt", "second archived document")
        accepted = self.client.post(
            "/api/documents/upload-zip",
            files={"archive": ("bundle.zip", output.getvalue(), "application/zip")},
            headers={"Idempotency-Key": "archive-one"},
        )
        self.assertEqual(accepted.status_code, 202)
        parent_id = accepted.json()["job_id"]
        with database.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        self.assertTrue(ingestion_jobs.run_one("archive-worker"))
        parent = self.client.get(f"/api/jobs/{parent_id}").json()
        self.assertEqual(parent["status"], "completed")
        child_ids = [item["job_id"] for item in parent["result"]["files"]]
        self.assertEqual(len(child_ids), 2)
        self.assertTrue(ingestion_jobs.run_one("document-worker"))
        self.assertTrue(ingestion_jobs.run_one("document-worker"))
        self.assertEqual(
            [
                self.client.get(f"/api/jobs/{job_id}").json()["status"]
                for job_id in child_ids
            ],
            ["completed", "completed"],
        )


if __name__ == "__main__":
    unittest.main()
