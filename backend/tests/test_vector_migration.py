"""Safety and rollback tests for the one-time Qdrant vector migration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app.services.vector_store import make_vector_point_id
from scripts import migrate_vectors_to_qdrant as migration


class FakeMigrationStore:
    def __init__(self) -> None:
        self.points = {}

    def upsert_chunks(self, points) -> None:
        self.points.update({point.point_id: point for point in points})

    def get_vectors(self, point_ids: list[str]) -> dict[str, list[float]]:
        return {
            point_id: self.points[point_id].vector
            for point_id in point_ids
            if point_id in self.points
        }

    def search(self, vector, **filters):
        return [
            {
                "chunk_id": point.chunk_id,
                "document_id": point.document_id,
                "document_version_id": point.version_id,
                "score": 1.0,
            }
            for point in self.points.values()
            if point.organization_id == filters["organization_id"]
            and point.owner_id == filters["user_id"]
            and point.document_id == filters["document_id"]
            and point.version_id in filters["current_version_ids"]
        ]


class VectorMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "migration.db"
        self.database_patch = patch.object(
            database, "DATABASE_PATH", self.database_path
        )
        self.database_patch.start()
        database.initialize_database()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES ('org-a', 'A')"
            )
            connection.execute(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (10, 'owner@example.com', 'hash', 'org-a',
                           'organization_admin')"""
            )
            content_id = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text, processing_status)
                   VALUES (10, 'org-a', 'file', 'normalized',
                           'first second', 'completed')"""
            ).lastrowid
            document_id = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    visibility, processing_status)
                   VALUES (10, 'org-a', 'source.txt', 'source.txt',
                           'stored.txt', 'file', ?, 'private', 'completed')""",
                (content_id,),
            ).lastrowid
            version_id = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, file_hash, normalized_content_hash,
                    status, created_by)
                   VALUES ('org-a', ?, 1, ?, 'stored.txt', 'file',
                           'normalized', 'completed', 10)""",
                (document_id, content_id),
            ).lastrowid
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (version_id, document_id),
            )
            connection.executemany(
                """INSERT INTO chunks
                   (content_id, chunk_index, text, embedding, organization_id,
                    document_id, version_id, source_type, source_location_json,
                    vector_point_id, embedding_model, embedding_dimension,
                    indexing_status)
                   VALUES (?, ?, ?, ?, 'org-a', ?, ?, 'text', ?,
                           ?, 'test-model', 4, 'completed')""",
                [
                    (
                        content_id, 0, "first", json.dumps([1, 0, 0, 0]),
                        document_id, version_id, json.dumps({"line_start": 1}),
                        "legacy-point-one",
                    ),
                    (
                        content_id, 1, "second", json.dumps([1]),
                        document_id, version_id, json.dumps({"line_start": 2}),
                        "legacy-point-two",
                    ),
                ],
            )
        self.document_id = int(document_id)
        self.version_id = int(version_id)
        self.setting_patches = [
            patch.object(migration.settings, "embedding_model_version", "test-model"),
            patch.object(migration.settings, "embedding_dimension", 4),
            patch.object(migration.settings, "embedding_batch_size", 1),
        ]
        for setting_patch in self.setting_patches:
            setting_patch.start()

    def tearDown(self) -> None:
        for setting_patch in reversed(self.setting_patches):
            setting_patch.stop()
        self.database_patch.stop()
        self.temporary.cleanup()

    def test_dry_run_is_read_only_and_apply_preserves_legacy_vectors(self) -> None:
        store = FakeMigrationStore()
        dry_run = migration.migrate_vectors(apply=False, store=store)
        self.assertEqual(dry_run.active_chunks, 2)
        self.assertEqual(dry_run.reused_sqlite_vectors, 1)
        self.assertEqual(dry_run.regenerated_vectors, 1)
        self.assertEqual(store.points, {})

        with patch.object(
            migration,
            "create_embeddings",
            return_value=[[0.0, 1.0, 0.0, 0.0]],
        ) as embed:
            report = migration.migrate_vectors(
                apply=True,
                upsert_batch_size=2,
                smoke_query_limit=3,
                store=store,
            )

        self.assertEqual(report.upserted_points, 2)
        self.assertEqual(report.verified_points, 2)
        self.assertEqual(report.smoke_queries, 1)
        self.assertTrue(report.legacy_vectors_preserved)
        embed.assert_called_once_with(["second"])
        expected_ids = {
            make_vector_point_id("org-a", self.version_id, index, "test-model")
            for index in (0, 1)
        }
        self.assertEqual(set(store.points), expected_ids)
        with database.get_connection() as connection:
            rows = connection.execute(
                """SELECT chunk_index, embedding, vector_point_id,
                          indexing_status, qdrant_indexed_at
                   FROM chunks ORDER BY chunk_index"""
            ).fetchall()
        self.assertEqual(
            [json.loads(row["embedding"]) for row in rows],
            [[1, 0, 0, 0], [1]],
        )
        self.assertEqual({row["vector_point_id"] for row in rows}, expected_ids)
        self.assertTrue(all(row["indexing_status"] == "completed" for row in rows))
        self.assertTrue(all(row["qdrant_indexed_at"] for row in rows))

    def test_upsert_failure_is_recorded_without_deleting_rollback_vectors(self) -> None:
        store = FakeMigrationStore()

        def fail_upsert(points) -> None:
            raise ConnectionError("Qdrant unavailable")

        store.upsert_chunks = fail_upsert
        with self.assertRaises(ConnectionError), patch.object(
            migration,
            "create_embeddings",
            return_value=[[0.0, 1.0, 0.0, 0.0]],
        ):
            migration.migrate_vectors(apply=True, store=store)

        with database.get_connection() as connection:
            rows = connection.execute(
                """SELECT embedding, indexing_status, qdrant_indexed_at
                   FROM chunks ORDER BY chunk_index"""
            ).fetchall()
        self.assertEqual(
            [json.loads(row["embedding"]) for row in rows],
            [[1, 0, 0, 0], [1]],
        )
        self.assertTrue(all(row["indexing_status"] == "failed" for row in rows))
        self.assertTrue(all(row["qdrant_indexed_at"] is None for row in rows))

    def test_partial_batch_failure_reruns_with_stable_ids(self) -> None:
        store = FakeMigrationStore()
        calls = {"count": 0}
        original_upsert = store.upsert_chunks

        def fail_second_batch(points) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise ConnectionError("second batch unavailable")
            original_upsert(points)

        store.upsert_chunks = fail_second_batch
        with self.assertRaises(ConnectionError), patch.object(
            migration,
            "create_embeddings",
            return_value=[[0.0, 1.0, 0.0, 0.0]],
        ):
            migration.migrate_vectors(
                apply=True,
                upsert_batch_size=1,
                store=store,
            )
        self.assertEqual(len(store.points), 1)

        store.upsert_chunks = original_upsert
        with patch.object(
            migration,
            "create_embeddings",
            return_value=[[0.0, 1.0, 0.0, 0.0]],
        ):
            report = migration.migrate_vectors(
                apply=True,
                upsert_batch_size=1,
                store=store,
            )
        self.assertEqual(report.verified_points, 2)
        self.assertEqual(len(store.points), 2)


if __name__ == "__main__":
    unittest.main()
