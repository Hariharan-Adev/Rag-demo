"""ZIP security validation, pipeline reuse, partial success, and cleanup tests."""

from __future__ import annotations

import asyncio
import stat
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app import database
from app.routes import documents, upload
from app.services import zip_archives
from app.config import settings


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/documents/upload-zip", "headers": [], "client": ("test", 1)})


def make_zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def mark_encrypted(data: bytes) -> bytes:
    value = bytearray(data)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (index := value.find(signature, start)) >= 0:
            flags = int.from_bytes(value[index + flag_offset:index + flag_offset + 2], "little") | 1
            value[index + flag_offset:index + flag_offset + 2] = flags.to_bytes(2, "little")
            start = index + 4
    return bytes(value)


class ZipUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db_path = root / "rag.db"
        self.upload_path = root / "uploads"
        self.archive_temp = root / "archive-temp"
        self.patchers = [
            patch.object(database, "DATABASE_PATH", self.db_path),
            patch.object(database, "UPLOAD_DIRECTORY", self.upload_path),
            patch.object(upload, "UPLOAD_DIRECTORY", self.upload_path),
            patch.object(documents, "UPLOAD_DIRECTORY", self.upload_path),
            patch.object(zip_archives, "ARCHIVE_TEMP_ROOT", self.archive_temp),
            patch.object(upload, "enforce_request_limit", lambda *args, **kwargs: None),
            patch.object(upload, "log_audit_event", lambda **kwargs: None),
            patch.object(upload, "extract_text", lambda path: path.read_text(encoding="utf-8")),
            patch.object(upload, "create_embeddings", lambda chunks: [[1.0, 0.0] for _ in chunks]),
        ]
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()
        with database.get_connection() as connection:
            connection.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'one@example.com', 'hash')")

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def upload_zip(self, content: bytes, filename: str = "documents.zip"):
        archive = UploadFile(file=BytesIO(content), filename=filename)
        return asyncio.run(upload.upload_zip_archive(request(), archive, {"id": 1}))

    def assert_temp_cleaned(self) -> None:
        self.assertFalse(self.archive_temp.exists() and any(self.archive_temp.iterdir()))

    def test_valid_and_unsupported_files_return_partial_success(self) -> None:
        result = self.upload_zip(make_zip([
            ("Finance/notes.txt", b"quarterly notes"),
            ("Finance/setup.exe", b"MZ dangerous"),
        ]))
        self.assertEqual(result["status"], "partially_completed")
        self.assertEqual(result["summary"], {"total_entries": 2, "uploaded": 1, "duplicates": 0, "failed": 1})
        self.assertEqual(result["files"][0]["filename"], "notes.txt")
        self.assertEqual(result["files"][1]["status"], "rejected")
        self.assert_temp_cleaned()

    def test_duplicate_content_reuses_existing_pipeline(self) -> None:
        result = self.upload_zip(make_zip([
            ("one.txt", b"shared content"),
            ("two.txt", b"shared content"),
        ]))
        self.assertEqual(result["summary"]["uploaded"], 1)
        self.assertEqual(result["summary"]["duplicates"], 1)
        self.assertEqual(result["files"][1]["status"], "duplicate_content_reused")
        with database.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_contents").fetchone()[0], 1)
        self.assert_temp_cleaned()

    def test_path_traversal_rejects_entire_archive_and_cleans_up(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.upload_zip(make_zip([("../../config.py", b"bad")]))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Archive failed security validation.")
        self.assert_temp_cleaned()

    def test_nested_archive_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.upload_zip(make_zip([("nested.zip", make_zip([("a.txt", b"a")]))]))
        self.assertEqual(raised.exception.detail, "Nested archives are not supported.")
        self.assert_temp_cleaned()

    def test_password_protected_flag_is_rejected(self) -> None:
        encrypted = mark_encrypted(make_zip([("secret.txt", b"secret")]))
        with self.assertRaises(HTTPException) as raised:
            self.upload_zip(encrypted)
        self.assertEqual(raised.exception.detail, "Password protected archives are not supported.")
        self.assert_temp_cleaned()

    def test_symbolic_link_is_rejected(self) -> None:
        link = zipfile.ZipInfo("link.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(HTTPException) as raised:
            self.upload_zip(make_zip([(link, b"target.txt")]))
        self.assertEqual(raised.exception.detail, "Archive links are not supported.")
        self.assert_temp_cleaned()

    def test_excessive_compression_ratio_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.upload_zip(make_zip([("zeros.txt", b"0" * 100_000)]))
        self.assertEqual(raised.exception.detail, "Archive compression ratio exceeds the safe limit.")
        self.assert_temp_cleaned()

    def test_signature_mismatch_fails_only_that_member(self) -> None:
        result = self.upload_zip(make_zip([
            ("fake.pdf", b"not a pdf"),
            ("valid.txt", b"valid text"),
        ]))
        self.assertEqual(result["summary"]["uploaded"], 1)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["files"][0]["status"], "rejected")
        self.assert_temp_cleaned()

    def test_archive_file_count_limit_is_enforced(self) -> None:
        with patch.object(settings, "max_zip_files", 1):
            with self.assertRaises(HTTPException) as raised:
                self.upload_zip(make_zip([("one.txt", b"one"), ("two.txt", b"two")]))
        self.assertEqual(raised.exception.detail, "Too many files in archive.")
        self.assert_temp_cleaned()

    def test_archive_upload_size_limit_is_enforced_while_streaming(self) -> None:
        with patch.object(settings, "max_zip_upload_mb", 0):
            with self.assertRaises(HTTPException) as raised:
                self.upload_zip(make_zip([("one.txt", b"one")]))
        self.assertEqual(raised.exception.detail, "Archive exceeds maximum allowed size.")
        self.assert_temp_cleaned()

    def test_archive_extracted_size_limit_is_enforced(self) -> None:
        with patch.object(settings, "max_zip_extracted_mb", 0):
            with self.assertRaises(HTTPException) as raised:
                self.upload_zip(make_zip([("one.txt", b"one")]))
        self.assertEqual(raised.exception.detail, "Archive exceeds maximum extracted size.")
        self.assert_temp_cleaned()


if __name__ == "__main__":
    unittest.main()
