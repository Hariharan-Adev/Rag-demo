"""Security and transport tests for document preview/download streaming."""

from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.auth import get_current_user
from app.main import app
from app.services.storage import storage_key_for, write_storage_bytes


class DocumentPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        root = Path(self.temporary.name)
        self.db_patch = patch.object(database, "DATABASE_PATH", root / "rag.db")
        self.upload_patch = patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
        self.db_patch.start()
        self.upload_patch.start()
        database.initialize_database()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                [("org-a", "Organization A"), ("org-b", "Organization B")],
            )
            connection.executemany(
                """INSERT INTO users (id, email, password_hash, organization_id, role)
                   VALUES (?, ?, 'hash', ?, 'member')""",
                [(1, "one@example.com", "org-a"), (2, "two@example.com", "org-b")],
            )
        self.current_user = {
            "id": 1,
            "email": "one@example.com",
            "organization_id": "org-a",
            "role": "member",
        }
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.upload_patch.stop()
        self.db_patch.stop()
        self.temporary.cleanup()

    def add_document(
        self,
        *,
        filename: str = "report.pdf",
        content: bytes = b"%PDF-preview-bytes",
        storage_key: str | None = None,
    ) -> int:
        file_hash = sha256(content).hexdigest()
        effective_key = storage_key or storage_key_for("org-a", f"stored-{filename}")
        if storage_key is None:
            write_storage_bytes(effective_key, content)
        with database.get_connection() as connection:
            content_id = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash, normalized_content_hash,
                    extracted_text, processing_status)
                   VALUES (1, 'org-a', ?, ?, 'preview', 'completed')""",
                (file_hash, sha256(content + filename.encode()).hexdigest()),
            ).lastrowid
            document_id = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename, display_filename,
                    stored_filename, file_hash, content_id, processing_status)
                   VALUES (1, 'org-a', ?, ?, ?, ?, ?, 'completed')""",
                (filename, filename, f"stored-{filename}", file_hash, content_id),
            ).lastrowid
            version_id = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, file_hash, status, created_by,
                    file_size, ingestion_status, extraction_status, indexing_status)
                   VALUES ('org-a', ?, 1, ?, ?, ?, ?, 'completed', 1, ?,
                           'completed', 'completed', 'completed')""",
                (
                    document_id, content_id, f"stored-{filename}", effective_key,
                    file_hash, len(content),
                ),
            ).lastrowid
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (version_id, document_id),
            )
        return int(document_id)

    def test_inline_preview_and_range_request(self) -> None:
        document_id = self.add_document()

        response = self.client.get(f"/documents/{document_id}/content")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-preview-bytes")
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "private, no-store")

        partial = self.client.get(
            f"/documents/{document_id}/content", headers={"Range": "bytes=0-3"}
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"%PDF")
        self.assertEqual(partial.headers["content-range"], "bytes 0-3/18")

    def test_download_uses_attachment_disposition(self) -> None:
        document_id = self.add_document(filename="annual report.pdf")
        response = self.client.get(
            f"/documents/{document_id}/content", params={"download": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_cross_tenant_access_returns_safe_not_found(self) -> None:
        document_id = self.add_document()
        self.current_user = {
            "id": 2,
            "email": "two@example.com",
            "organization_id": "org-b",
            "role": "member",
        }
        response = self.client.get(f"/documents/{document_id}/content")
        self.assertEqual(response.status_code, 404)

    def test_traversal_key_and_missing_file_are_not_exposed(self) -> None:
        traversal_id = self.add_document(filename="traversal.pdf", storage_key="../../outside.pdf")
        self.assertEqual(
            self.client.get(f"/documents/{traversal_id}/content").status_code, 404
        )
        missing_id = self.add_document(filename="missing.pdf", storage_key="org-a/missing.pdf")
        self.assertEqual(
            self.client.get(f"/documents/{missing_id}/content").status_code, 404
        )

    def test_unsupported_type_returns_415(self) -> None:
        document_id = self.add_document(filename="payload.exe", content=b"not executable")
        response = self.client.get(f"/documents/{document_id}/content")
        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()
