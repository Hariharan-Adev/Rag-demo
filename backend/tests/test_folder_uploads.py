"""Collection schema, path safety, batch accounting, and scoped retrieval tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import database
from app.services import vector_search
from app.services.folder_uploads import record_batch_result, sanitize_relative_path, validate_upload_context


class FolderUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_patch = patch.object(database, "DATABASE_PATH", Path(self.temporary.name) / "folder.db")
        self.database_patch.start()
        database.initialize_database()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'hash')",
                [(1, "one@example.com"), (2, "two@example.com")],
            )
            connection.execute("INSERT INTO document_collections (id, owner_id, name) VALUES (10, 1, 'Finance')")
            connection.execute("INSERT INTO document_collections (id, owner_id, name) VALUES (20, 2, 'Finance')")

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary.cleanup()

    def test_relative_path_is_metadata_safe(self) -> None:
        self.assertEqual(sanitize_relative_path("Finance/2026/report.txt", "report.txt"), "Finance/2026/report.txt")
        self.assertIsNone(sanitize_relative_path(None, "report.txt"))
        self.assertIsNone(sanitize_relative_path(object(), "report.txt"))
        for unsafe in ("../report.txt", "/root/report.txt", "C:/report.txt", "Finance\\..\\report.txt", "Finance/\x00.txt"):
            with self.subTest(path=unsafe), self.assertRaises(HTTPException):
                sanitize_relative_path(unsafe, "report.txt")

    def test_collection_and_batch_are_owner_scoped(self) -> None:
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO upload_batches (id, owner_id, collection_id, original_folder_name, total_files) VALUES (30, 1, 10, 'Finance', 2)"
            )
        validate_upload_context(1, 10, 30)
        with self.assertRaises(HTTPException):
            validate_upload_context(2, 10, 30)
        with self.assertRaises(HTTPException):
            validate_upload_context(1, 20, 30)

    def test_batch_counters_become_partially_completed(self) -> None:
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO upload_batches (id, owner_id, collection_id, original_folder_name, total_files) VALUES (31, 1, 10, 'Finance', 3)"
            )
        record_batch_result(31, 1, "successful")
        record_batch_result(31, 1, "duplicate")
        record_batch_result(31, 1, "failed")
        with database.get_connection() as connection:
            batch = connection.execute("SELECT * FROM upload_batches WHERE id = 31").fetchone()
        self.assertEqual(batch["processed_files"], 3)
        self.assertEqual(batch["successful_files"], 1)
        self.assertEqual(batch["duplicate_files"], 1)
        self.assertEqual(batch["failed_files"], 1)
        self.assertEqual(batch["status"], "partially_completed")

    def test_collection_scoped_retrieval_keeps_owner_filter(self) -> None:
        with database.get_connection() as connection:
            for content_id, owner_id, collection_id, text in ((1, 1, 10, "owned finance"), (2, 2, 20, "private finance")):
                connection.execute(
                    "INSERT INTO document_contents (id, owner_id, file_hash, normalized_content_hash, extracted_text, processing_status) VALUES (?, ?, ?, ?, ?, 'completed')",
                    (content_id, owner_id, f"file-{content_id}", f"content-{content_id}", text),
                )
                connection.execute(
                    "INSERT INTO documents (owner_id, original_filename, display_filename, stored_filename, file_hash, content_id, collection_id) VALUES (?, ?, ?, '', ?, ?, ?)",
                    (owner_id, f"{content_id}.txt", f"{content_id}.txt", f"file-{content_id}", content_id, collection_id),
                )
                connection.execute("INSERT INTO chunks (content_id, chunk_index, text, embedding) VALUES (?, 0, ?, '[1.0, 0.0]')", (content_id, text))
        with patch.object(vector_search, "create_embeddings", return_value=[[1.0, 0.0]]):
            results = vector_search.search_chunks("finance", 1, 10, collection_id=10)
        self.assertEqual([result["content"] for result in results], ["owned finance"])


if __name__ == "__main__":
    unittest.main()
