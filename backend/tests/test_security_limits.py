"""Security boundary tests for organization storage and Office containers."""

from __future__ import annotations

import unittest
from io import BytesIO
import tempfile
from unittest.mock import patch
import zipfile
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.services.document_loader import DocumentParseError
from app.services.ingestion_jobs import _extract_bundle
from app.services.storage import resolve_storage_key, storage_key_for
from app.utils.file_validation import validate_file_signature


class SecurityLimitTests(unittest.TestCase):
    @staticmethod
    def office_archive(name: str, content: bytes = b"value") -> bytes:
        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, content)
        return output.getvalue()

    def test_office_archive_traversal_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            validate_file_signature(
                "unsafe.docx", self.office_archive("../outside.xml")
            )
        self.assertIn("unsafe archive path", raised.exception.detail)

    def test_office_archive_expansion_and_ratio_are_bounded(self) -> None:
        payload = self.office_archive("word/document.xml", b"A" * 10000)
        with (
            patch.object(settings, "max_office_uncompressed_mb", 1),
            patch.object(settings, "max_office_compression_ratio", 2.0),
            self.assertRaises(HTTPException) as raised,
        ):
            validate_file_signature("compressed.docx", payload)
        self.assertIn("compression ratio", raised.exception.detail)

    def test_storage_keys_are_tenant_partitioned_and_traversal_safe(self) -> None:
        self.assertNotEqual(
            storage_key_for("org-a", "file.txt").split("/", 1)[0],
            storage_key_for("org-b", "file.txt").split("/", 1)[0],
        )
        with self.assertRaises(ValueError):
            resolve_storage_key("../outside.txt")

    def test_production_parser_timeout_terminates_isolated_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.txt"
            path.write_text("bounded parser input", encoding="utf-8")
            with (
                patch.object(settings, "app_environment", "production"),
                patch.object(settings, "parser_timeout_seconds", 0.001),
                self.assertRaises(DocumentParseError) as raised,
            ):
                _extract_bundle(path)
        self.assertIn("time limit", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
