"""Unit tests for one-document structured CSV reindexing."""

from __future__ import annotations

from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import database
from app.services.document_loader import DocumentParseError
from app.services.storage import storage_key_for, write_storage_bytes
from app.services.structured_ingestion import (
    StructuredCsvReindexError,
    reindex_existing_csv_document,
)


class StructuredCsvReindexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", root / "reindex.db")
        )
        self.stack.enter_context(
            patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
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

    def _document(
        self,
        *,
        extension: str = ".csv",
        write_file: bool = True,
    ) -> tuple[int, int, Path]:
        content = (
            b"Equipment,Price\nPump,75000\nTractor,550000\n"
            if extension == ".csv"
            else b"plain text"
        )
        stored_filename = f"stored{extension}"
        storage_key = storage_key_for("org-a", stored_filename)
        path = (
            write_storage_bytes(storage_key, content)
            if write_file
            else database.UPLOAD_DIRECTORY / storage_key
        )
        file_hash = sha256(content).hexdigest()
        with database.get_connection() as connection:
            content_cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text, processing_status)
                   VALUES (10, 'org-a', ?, ?, 'legacy', 'completed')""",
                (file_hash, f"normalized-{extension}"),
            )
            content_id = int(content_cursor.lastrowid)
            document_cursor = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    visibility, processing_status, updated_at)
                   VALUES (10, 'org-a', ?, ?, ?, ?, ?, 'private',
                           'completed', CURRENT_TIMESTAMP)""",
                (
                    f"legacy{extension}",
                    f"legacy{extension}",
                    stored_filename,
                    file_hash,
                    content_id,
                ),
            )
            document_id = int(document_cursor.lastrowid)
            version_cursor = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, mime_type, file_size,
                    file_hash, status, ingestion_status, extraction_status,
                    indexing_status, created_by)
                   VALUES ('org-a', ?, 1, ?, ?, ?, ?, ?, ?, 'completed',
                           'completed', 'completed', 'completed', 10)""",
                (
                    document_id,
                    content_id,
                    stored_filename,
                    storage_key,
                    "text/csv" if extension == ".csv" else "text/plain",
                    len(content),
                    file_hash,
                ),
            )
            version_id = int(version_cursor.lastrowid)
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (version_id, document_id),
            )
        return document_id, content_id, path

    @staticmethod
    def _reindex(document_id: int):
        return reindex_existing_csv_document(
            document_id=document_id,
            owner_id=10,
            organization_id="org-a",
        )

    def test_successful_reindex_preserves_document_and_content(self) -> None:
        document_id, content_id, _ = self._document()
        result = self._reindex(document_id)

        with database.get_connection() as connection:
            document = connection.execute(
                "SELECT id, content_id FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            content = connection.execute(
                """SELECT structured_index_status, structured_index_error
                   FROM document_contents WHERE id = ?""",
                (content_id,),
            ).fetchone()
            counts = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM workbook_sheets
                      WHERE content_id = ?) AS sheets,
                     (SELECT COUNT(*) FROM workbook_rows
                      WHERE content_id = ?) AS rows""",
                (content_id, content_id),
            ).fetchone()
        self.assertEqual(result.status, "completed")
        self.assertEqual((document["id"], document["content_id"]), (
            document_id,
            content_id,
        ))
        self.assertEqual(content["structured_index_status"], "completed")
        self.assertIsNone(content["structured_index_error"])
        self.assertEqual((counts["sheets"], counts["rows"]), (1, 2))

    def test_deleted_document_is_skipped(self) -> None:
        document_id, content_id, _ = self._document()
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE documents SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                (document_id,),
            )
        result = self._reindex(document_id)
        with database.get_connection() as connection:
            sheet_count = connection.execute(
                "SELECT COUNT(*) FROM workbook_sheets WHERE content_id = ?",
                (content_id,),
            ).fetchone()[0]
        self.assertEqual(result.status, "skipped_deleted")
        self.assertEqual(sheet_count, 0)

    def test_non_csv_is_rejected(self) -> None:
        document_id, content_id, _ = self._document(extension=".txt")
        with self.assertRaisesRegex(StructuredCsvReindexError, "not a CSV"):
            self._reindex(document_id)
        with database.get_connection() as connection:
            status = connection.execute(
                """SELECT structured_index_status FROM document_contents
                   WHERE id = ?""",
                (content_id,),
            ).fetchone()[0]
        self.assertEqual(status, "pending")

    def test_missing_stored_file_records_retryable_failure(self) -> None:
        document_id, content_id, _ = self._document(write_file=False)
        with self.assertRaisesRegex(
            StructuredCsvReindexError,
            "Stored CSV file is unavailable",
        ):
            self._reindex(document_id)
        with database.get_connection() as connection:
            row = connection.execute(
                """SELECT structured_index_status, structured_index_error
                   FROM document_contents WHERE id = ?""",
                (content_id,),
            ).fetchone()
        self.assertEqual(row["structured_index_status"], "failed")
        self.assertEqual(
            row["structured_index_error"],
            "Stored CSV file is unavailable.",
        )

    def test_parser_failure_rolls_back_structured_records(self) -> None:
        document_id, content_id, _ = self._document()
        self._reindex(document_id)
        with database.get_connection() as connection:
            before = connection.execute(
                """SELECT ws.name, wr.row_number, wr.values_json
                   FROM workbook_sheets ws
                   JOIN workbook_rows wr ON wr.sheet_id = ws.id
                   WHERE ws.content_id = ? ORDER BY wr.row_number""",
                (content_id,),
            ).fetchall()
        with patch(
            "app.services.structured_ingestion.extract_workbook",
            side_effect=DocumentParseError("The CSV file could not be read."),
        ):
            with self.assertRaisesRegex(
                StructuredCsvReindexError,
                "CSV file could not be read",
            ):
                self._reindex(document_id)
        with database.get_connection() as connection:
            after = connection.execute(
                """SELECT ws.name, wr.row_number, wr.values_json
                   FROM workbook_sheets ws
                   JOIN workbook_rows wr ON wr.sheet_id = ws.id
                   WHERE ws.content_id = ? ORDER BY wr.row_number""",
                (content_id,),
            ).fetchall()
            status = connection.execute(
                """SELECT structured_index_status FROM document_contents
                   WHERE id = ?""",
                (content_id,),
            ).fetchone()[0]
        self.assertEqual(
            [tuple(row) for row in after],
            [tuple(row) for row in before],
        )
        self.assertEqual(status, "failed")

    def test_rerun_replaces_rows_without_duplication(self) -> None:
        document_id, content_id, _ = self._document()
        first = self._reindex(document_id)
        second = self._reindex(document_id)
        with database.get_connection() as connection:
            counts = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM workbook_sheets
                      WHERE content_id = ?) AS sheets,
                     (SELECT COUNT(*) FROM workbook_rows
                      WHERE content_id = ?) AS rows""",
                (content_id, content_id),
            ).fetchone()
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual((counts["sheets"], counts["rows"]), (1, 2))

    def test_qdrant_point_count_is_unchanged(self) -> None:
        document_id, _, _ = self._document()
        fake_points = {"stable-point": [1.0, 0.0]}
        before = len(fake_points)
        with patch(
            "app.services.vector_store.get_vector_store",
            side_effect=AssertionError("Qdrant must not be accessed"),
        ) as get_store, patch(
            "app.services.embeddings.create_embeddings",
            side_effect=AssertionError("Embeddings must not be generated"),
        ) as create_embeddings:
            self._reindex(document_id)
        self.assertEqual(len(fake_points), before)
        get_store.assert_not_called()
        create_embeddings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
