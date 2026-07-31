"""Structured indexing schema migration and persistence tests."""

from __future__ import annotations

from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import database
from app.services.storage import storage_key_for, write_storage_bytes
from app.services.structured_ingestion import (
    StructuredDocumentContext,
    ingest_structured_csv,
)
from app.services.workbooks import STRUCTURED_INDEX_VERSION


class StructuredIndexMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.database_path = Path(self.temporary.name) / "structured.db"
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", self.database_path)
        )
        self.stack.enter_context(
            patch.object(
                database,
                "UPLOAD_DIRECTORY",
                Path(self.temporary.name) / "uploads",
            )
        )
        database.initialize_database()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES ('org-a', 'A')"
            )
            connection.execute(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (10, 'owner@example.com', 'hash', 'org-a', 'member')"""
            )

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    @staticmethod
    def _insert_content(*, deleted: bool = False) -> int:
        with database.get_connection() as connection:
            cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text,
                    processing_status, deleted_at)
                   VALUES (10, 'org-a', ?, ?, 'legacy csv', 'completed',
                           CASE WHEN ? THEN CURRENT_TIMESTAMP END)""",
                (
                    "deleted-file" if deleted else "active-file",
                    "deleted-content" if deleted else "active-content",
                    deleted,
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _create_csv_context(
        content_id: int,
    ) -> tuple[StructuredDocumentContext, Path]:
        stored_filename = "legacy-structured.csv"
        storage_key = storage_key_for("org-a", stored_filename)
        content = b"Equipment,Price\nPump,75000\nTractor,550000\n"
        path = write_storage_bytes(storage_key, content)
        file_hash = sha256(content).hexdigest()
        with database.get_connection() as connection:
            document_cursor = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    visibility, processing_status, updated_at)
                   VALUES (10, 'org-a', 'legacy.csv', 'legacy.csv', ?, ?,
                           ?, 'private', 'completed', CURRENT_TIMESTAMP)""",
                (stored_filename, file_hash, content_id),
            )
            document_id = int(document_cursor.lastrowid)
            version_cursor = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, mime_type, file_size,
                    file_hash, status, ingestion_status, extraction_status,
                    indexing_status, created_by)
                   VALUES ('org-a', ?, 1, ?, ?, ?, 'text/csv', ?, ?,
                           'completed', 'completed', 'completed', 'completed',
                           10)""",
                (
                    document_id,
                    content_id,
                    stored_filename,
                    storage_key,
                    len(content),
                    file_hash,
                ),
            )
            version_id = int(version_cursor.lastrowid)
            connection.execute(
                """UPDATE documents SET current_version_id = ?
                   WHERE id = ?""",
                (version_id, document_id),
            )
        return (
            StructuredDocumentContext(
                document_id=document_id,
                version_id=version_id,
                content_id=content_id,
                owner_id=10,
                organization_id="org-a",
            ),
            path,
        )

    def test_schema_defaults_constraints_and_retry_index(self) -> None:
        content_id = self._insert_content()
        with database.get_connection() as connection:
            row = connection.execute(
                """SELECT structured_index_status, structured_index_version,
                          structured_indexed_at, structured_index_error
                   FROM document_contents WHERE id = ?""",
                (content_id,),
            ).fetchone()
            self.assertEqual(row["structured_index_status"], "pending")
            self.assertIsNone(row["structured_index_version"])
            self.assertIsNone(row["structured_indexed_at"])
            self.assertIsNone(row["structured_index_error"])
            index = connection.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type = 'index'
                     AND name = 'idx_contents_structured_retry'"""
            ).fetchone()
            self.assertIn("WHERE deleted_at IS NULL", index["sql"])
            self.assertIn("'pending','failed'", index["sql"])
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM schema_migrations
                   WHERE version = '010_structured_csv_indexing'"""
            ).fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE document_contents
                       SET structured_index_status = 'unknown' WHERE id = ?""",
                    (content_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE document_contents
                       SET structured_index_error = ? WHERE id = ?""",
                    ("x" * 501, content_id),
                )

    def test_legacy_rows_remain_pending_and_lifecycle_is_preserved(self) -> None:
        active_id = self._insert_content()
        deleted_id = self._insert_content(deleted=True)
        with database.get_connection() as connection:
            connection.execute("DROP INDEX idx_contents_structured_retry")
            connection.execute(
                """DELETE FROM schema_migrations
                   WHERE version = '010_structured_csv_indexing'"""
            )
            for column in (
                "structured_index_error",
                "structured_indexed_at",
                "structured_index_version",
                "structured_index_status",
            ):
                connection.execute(
                    f"ALTER TABLE document_contents DROP COLUMN {column}"
                )

        database.initialize_database()
        database.initialize_database()

        with database.get_connection() as connection:
            rows = connection.execute(
                """SELECT id, structured_index_status, deleted_at
                   FROM document_contents ORDER BY id"""
            ).fetchall()
            by_id = {int(row["id"]): row for row in rows}
            self.assertEqual(
                by_id[active_id]["structured_index_status"],
                "pending",
            )
            self.assertEqual(
                by_id[deleted_id]["structured_index_status"],
                "pending",
            )
            self.assertIsNone(by_id[active_id]["deleted_at"])
            self.assertIsNotNone(by_id[deleted_id]["deleted_at"])
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_shared_csv_service_replaces_incomplete_rows_transactionally(self) -> None:
        content_id = self._insert_content()
        context, path = self._create_csv_context(content_id)
        with database.get_connection() as connection:
            with self.assertRaisesRegex(RuntimeError, "active transaction"):
                ingest_structured_csv(
                    connection,
                    context=context,
                    validated_path=path,
                )

        with database.get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ingest_structured_csv(
                connection,
                context=context,
                validated_path=path,
            )
        with database.get_connection() as connection:
            connection.execute(
                "DELETE FROM workbook_rows WHERE content_id = ? AND row_number = 3",
                (content_id,),
            )
            connection.execute(
                """UPDATE document_contents
                   SET structured_index_status = 'failed',
                       structured_index_error = 'incomplete'
                   WHERE id = ?""",
                (content_id,),
            )
        with database.get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ingest_structured_csv(
                connection,
                context=context,
                validated_path=path,
            )

        with database.get_connection() as connection:
            content = connection.execute(
                """SELECT structured_index_status, structured_index_version,
                          structured_indexed_at, structured_index_error
                   FROM document_contents WHERE id = ?""",
                (content_id,),
            ).fetchone()
            sheet = connection.execute(
                """SELECT content_id, owner_id, organization_id
                   FROM workbook_sheets WHERE content_id = ?""",
                (content_id,),
            ).fetchone()
            row = connection.execute(
                """SELECT content_id, owner_id, organization_id
                   FROM workbook_rows WHERE content_id = ?""",
                (content_id,),
            ).fetchone()
            row_count = connection.execute(
                "SELECT COUNT(*) FROM workbook_rows WHERE content_id = ?",
                (content_id,),
            ).fetchone()[0]
            chunk_count = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE content_id = ?",
                (content_id,),
            ).fetchone()[0]
        self.assertEqual(content["structured_index_status"], "completed")
        self.assertEqual(
            content["structured_index_version"],
            STRUCTURED_INDEX_VERSION,
        )
        self.assertIsNotNone(content["structured_indexed_at"])
        self.assertIsNone(content["structured_index_error"])
        self.assertEqual(
            tuple(sheet),
            (content_id, 10, "org-a"),
        )
        self.assertEqual(
            tuple(row),
            (content_id, 10, "org-a"),
        )
        self.assertEqual(row_count, 2)
        self.assertEqual(chunk_count, 0)

    def test_error_sanitizer_is_single_line_and_bounded(self) -> None:
        message = " parser\x00failed\r\n" + ("x" * 700)
        sanitized = database.sanitize_structured_index_error(message)
        self.assertNotIn("\x00", sanitized)
        self.assertNotIn("\r", sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertLessEqual(
            len(sanitized),
            database.STRUCTURED_INDEX_ERROR_MAX_LENGTH,
        )
        self.assertEqual(
            database.sanitize_structured_index_error(" \n "),
            "Structured document indexing failed.",
        )


if __name__ == "__main__":
    unittest.main()
