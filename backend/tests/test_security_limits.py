"""Security boundary tests for organization storage and Office containers."""

from __future__ import annotations

import unittest
from io import BytesIO
import queue
import tempfile
from unittest.mock import patch
import zipfile
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.services.document_loader import DocumentParseError
from app.services import ingestion_jobs
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
        class TimeoutQueue:
            def get(self, timeout):
                raise queue.Empty()

            def close(self):
                return None

        class TimeoutProcess:
            def __init__(self, *args, **kwargs):
                self.terminated = False

            def start(self):
                return None

            def is_alive(self):
                return not self.terminated

            def terminate(self):
                self.terminated = True

            def join(self, timeout=None):
                return None

        class TimeoutContext:
            def Queue(self, maxsize):
                return TimeoutQueue()

            def Process(self, *args, **kwargs):
                return TimeoutProcess()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.txt"
            path.write_text("bounded parser input", encoding="utf-8")
            with (
                patch.object(settings, "app_environment", "production"),
                patch.object(settings, "parser_timeout_seconds", 0.001),
                patch.object(
                    ingestion_jobs.multiprocessing,
                    "get_context",
                    return_value=TimeoutContext(),
                ),
                self.assertRaises(DocumentParseError) as raised,
            ):
                _extract_bundle(path)
        self.assertIn("time limit", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
