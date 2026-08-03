"""Duplicate upload, shared-content, deletion, and migration tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import database
from app.auth import get_current_user
from app.main import app
from app.routes import documents, upload
from app.services import vector_search
from app.services.vector_store import reset_vector_store_for_tests
from app.services import vector_store
from app.utils.document_content import generate_unique_display_filename, normalize_extracted_text, sanitize_filename


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("test", 1)})


def response(result) -> tuple[int, dict[str, object]]:
    if isinstance(result, JSONResponse):
        return result.status_code, json.loads(result.body)
    return 200, result


class DocumentDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db_path = root / "rag.db"
        self.upload_path = root / "uploads"
        self.patchers = [
            patch.object(database, "DATABASE_PATH", self.db_path),
            patch.object(database, "UPLOAD_DIRECTORY", self.upload_path),
            patch.object(upload, "UPLOAD_DIRECTORY", self.upload_path),
            patch.object(documents, "UPLOAD_DIRECTORY", self.upload_path),
            patch.object(upload, "enforce_request_limit", lambda *args, **kwargs: None),
            patch.object(upload, "log_audit_event", lambda **kwargs: None),
            patch.object(documents, "log_audit_event", lambda **kwargs: None),
            patch.object(vector_store.settings, "vector_store", "sqlite"),
            patch.object(vector_store.settings, "vector_store_provider", "sqlite"),
            patch.object(vector_store.settings, "qdrant_local_path", ""),
            patch.object(upload, "extract_text", lambda path: path.read_text(encoding="utf-8")),
            patch.object(upload, "create_embeddings", lambda chunks: [[1.0] + [0.0] * 383 for _ in chunks]),
        ]
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()
        reset_vector_store_for_tests()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'hash')",
                [(1, "one@example.com"), (2, "two@example.com")],
            )

    def tearDown(self) -> None:
        reset_vector_store_for_tests()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def upload(self, filename: str, content: bytes, owner_id: int = 1):
        file = UploadFile(file=BytesIO(content), filename=filename)
        return asyncio.run(upload.upload_document(request(), file, {"id": owner_id}))

    def counts(self) -> tuple[int, int, int]:
        with database.get_connection() as connection:
            return tuple(int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("documents", "document_contents", "chunks"))

    def test_different_filename_and_different_content(self):
        self.assertFalse(response(self.upload("n1.txt", b"alpha document"))[1]["content_reused"])
        self.assertFalse(response(self.upload("n2.txt", b"beta document"))[1]["content_reused"])
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_same_filename_different_content_gets_suffix(self):
        self.upload("n1.txt", b"alpha")
        body = response(self.upload("n1.txt", b"beta"))[1]
        self.assertEqual(body["display_filename"], "n1(1).txt")

    def test_different_filename_identical_bytes_skips_extraction(self):
        self.upload("n1.txt", b"same bytes")
        with patch.object(upload, "extract_text", side_effect=AssertionError("extracted twice")):
            body = response(self.upload("n2.txt", b"same bytes"))[1]
        self.assertTrue(body["content_reused"])
        self.assertEqual(self.counts(), (2, 1, 2))

    def test_different_filename_same_normalized_text_reuses_content(self):
        self.upload("n1.txt", b"same   words\n\n\nparagraph")
        body = response(self.upload("n2.csv", b"same\twords\r\n\r\nparagraph"))[1]
        self.assertTrue(body["content_reused"])
        self.assertEqual(self.counts(), (2, 1, 2))

    def test_same_filename_identical_bytes_returns_conflict(self):
        first = response(self.upload("n1.txt", b"same"))[1]
        status, body = response(self.upload("n1.txt", b"same"))
        self.assertEqual(status, 409)
        self.assertEqual(body["existing_document_id"], first["document_id"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_same_filename_same_normalized_text_returns_conflict(self):
        self.upload("n1.txt", b"same   text")
        self.assertEqual(response(self.upload("n1.txt", b"same\ttext"))[0], 409)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_duplicate_checks_are_isolated_between_users(self):
        self.upload("same.txt", b"same bytes", 1)
        body = response(self.upload("same.txt", b"same bytes", 2))[1]
        self.assertFalse(body["content_reused"])
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_filename_suffix_generation(self):
        self.upload("report.txt", b"one")
        self.upload("report.txt", b"two")
        with database.get_connection() as connection:
            self.assertEqual(generate_unique_display_filename(connection, 1, "report.txt"), "report(2).txt")

    def test_filename_sanitization_blocks_path_traversal(self):
        cases = [("../../secret.txt", "secret.txt"), ("..\\..\\bad<>name.txt", "bad_name.txt"), ("?.txt", "document.txt")]
        for unsafe, safe in cases:
            with self.subTest(unsafe=unsafe):
                self.assertEqual(sanitize_filename(unsafe, upload.ALLOWED_EXTENSIONS), safe)

    def test_normalization_is_deterministic(self):
        left = "  Hello\t world\r\n\r\n\r\nNext\x00 line  "
        right = "Hello world\n\nNext line"
        self.assertEqual(normalize_extracted_text(left), normalize_extracted_text(right))

    def test_failed_extraction_creates_no_records_or_files(self):
        with patch.object(upload, "extract_text", side_effect=ValueError("broken")):
            with self.assertRaises(HTTPException) as raised:
                self.upload("broken.txt", b"content")
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(list(self.upload_path.glob("*")), [])

    def test_failed_embedding_marks_content_failed_and_removes_chunks(self):
        with patch.object(upload, "create_embeddings", side_effect=RuntimeError("provider")):
            with self.assertRaises(HTTPException):
                self.upload("broken.txt", b"content")
        self.assertEqual(self.counts(), (0, 1, 0))
        with database.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT processing_status FROM document_contents").fetchone()[0], "failed")

    def test_deleting_one_shared_reference_preserves_content(self):
        first = response(self.upload("one.txt", b"shared"))[1]
        self.upload("two.txt", b"shared")
        result = documents.delete_document(int(first["document_id"]), request(), {"id": 1})
        self.assertTrue(result["soft_deleted"])
        self.assertEqual(self.counts(), (2, 1, 2))
        with database.get_connection() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT deleted_at FROM documents WHERE id = ?",
                (first["document_id"],),
            ).fetchone()[0])

    def test_deleting_final_reference_removes_content_and_chunks(self):
        first = response(self.upload("one.txt", b"shared"))[1]
        result = documents.delete_document(int(first["document_id"]), request(), {"id": 1})
        self.assertTrue(result["soft_deleted"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_retrieval_returns_shared_chunk_once(self):
        self.upload("one.txt", b"shared")
        self.upload("two.txt", b"shared")
        with patch.object(vector_search, "create_embeddings", return_value=[[1.0] + [0.0] * 383]):
            results = vector_search.search_chunks("question", 1, limit=10)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {result["filename"] for result in results},
            {"one.txt", "two.txt"},
        )

    def test_concurrent_duplicate_uploads_create_one_content(self):
        calls = 0
        lock = threading.Lock()

        def embeddings(chunks):
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.15)
            return [[1.0] + [0.0] * 383 for _ in chunks]

        results: list[object] = []
        with patch.object(upload, "create_embeddings", side_effect=embeddings):
            threads = [threading.Thread(target=lambda name=name: results.append(self.upload(name, b"shared content"))) for name in ("one.txt", "two.txt")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(calls, 1)
        self.assertEqual(self.counts(), (2, 1, 2))

    def test_upload_api_exercises_all_four_duplicate_cases(self):
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "one@example.com"}
        try:
            with TestClient(app) as client:
                first = client.post("/documents/upload-legacy", files={"file": ("n1.txt", b"alpha", "text/plain")})
                same_name_new_content = client.post("/documents/upload-legacy", files={"file": ("n1.txt", b"beta", "text/plain")})
                new_name_same_content = client.post("/documents/upload-legacy", files={"file": ("n2.txt", b"alpha", "text/plain")})
                exact_duplicate = client.post("/documents/upload-legacy", files={"file": ("n1.txt", b"alpha", "text/plain")})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["status"], "processed")
            self.assertEqual(same_name_new_content.json()["display_filename"], "n1(1).txt")
            self.assertTrue(new_name_same_content.json()["content_reused"])
            self.assertEqual(exact_duplicate.status_code, 409)
            self.assertEqual(exact_duplicate.json()["duplicate_type"], "same_filename_same_content")
        finally:
            app.dependency_overrides.clear()

    def test_legacy_database_migration_preserves_documents(self):
        legacy_path = Path(self.temporary.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, password_hash TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO users (id, email, password_hash) VALUES (1, 'legacy@example.com', 'hash');
            CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, stored_filename TEXT, owner_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL, content TEXT NOT NULL, chunk_index INTEGER NOT NULL, embedding TEXT);
            INSERT INTO documents (id, filename, stored_filename, owner_id) VALUES (1, 'legacy.txt', 'legacy.txt', 1);
            INSERT INTO chunks (document_id, content, chunk_index, embedding) VALUES (1, 'legacy content', 0, '[1.0, 0.0]');
            """
        )
        connection.commit()
        connection.close()
        with patch.object(database, "DATABASE_PATH", legacy_path), patch.object(database, "UPLOAD_DIRECTORY", Path(self.temporary.name) / "legacy_uploads"):
            database.initialize_database()
            with database.get_connection() as migrated:
                self.assertEqual(migrated.execute("SELECT display_filename FROM documents WHERE id = 1").fetchone()[0], "legacy.txt")
                self.assertEqual(migrated.execute("SELECT text FROM chunks").fetchone()[0], "legacy content")
                self.assertEqual(migrated.execute("SELECT processing_status FROM document_contents").fetchone()[0], "completed")
        self.assertTrue(legacy_path.with_suffix(".db.pre_content_refactor.bak").exists())


if __name__ == "__main__":
    unittest.main()
