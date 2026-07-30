"""Image understanding and dedicated upload endpoint regression tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from starlette.requests import Request

from app.routes.images import upload_image
from app.services.image_processor.image_parser import chunk_image_text, extract_image_text
from app.services.image_processor.ocr import OcrResult
from app.services.image_processor.vision import _clean_vision_output


class ImagePipelineTests(unittest.TestCase):
    def test_dense_screenshot_text_is_split_into_focused_chunks(self):
        text = " ".join(f"word-{index}" for index in range(500))
        chunks = chunk_image_text(text)
        self.assertEqual(len(chunks), 4)
        self.assertLessEqual(max(len(chunk.split()) for chunk in chunks), 200)
        self.assertIn("word-160", chunks[0])
        self.assertIn("word-160", chunks[1])

    def test_internal_vision_reasoning_is_not_indexed(self):
        content = "<think>private reasoning</think>\nSubject: Connection error"
        self.assertEqual(
            _clean_vision_output(content),
            "Subject: Connection error",
        )
        self.assertEqual(_clean_vision_output("<think>truncated reasoning"), "")

    def test_combines_ocr_metadata_and_visual_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "error.png"
            path.touch()
            ocr = OcrResult(
                text="Connection failed\nError Code: 1433",
                image_format="PNG",
                width=1280,
                height=720,
                frame_count=1,
            )
            with (
                patch(
                    "app.services.image_processor.image_parser.extract_ocr",
                    return_value=ocr,
                ),
                patch(
                    "app.services.image_processor.image_parser.vision_is_configured",
                    return_value=True,
                ),
                patch(
                    "app.services.image_processor.image_parser.describe_image",
                    return_value="A database connection error dialog is visible.",
                ),
            ):
                text = extract_image_text(path)

        self.assertIn("Document type: screenshot", text)
        self.assertIn("Dimensions: 1280 x 720", text)
        self.assertIn("Error Code: 1433", text)
        self.assertIn("database connection error dialog", text)

    def test_vision_failure_falls_back_to_local_ocr(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.jpg"
            path.touch()
            ocr = OcrResult("24 leave days", "JPEG", 800, 600, 1)
            with (
                patch(
                    "app.services.image_processor.image_parser.extract_ocr",
                    return_value=ocr,
                ),
                patch(
                    "app.services.image_processor.image_parser.vision_is_configured",
                    return_value=True,
                ),
                patch(
                    "app.services.image_processor.image_parser.describe_image",
                    side_effect=RuntimeError("provider unavailable"),
                ),
            ):
                text = extract_image_text(path)

        self.assertIn("24 leave days", text)
        self.assertNotIn("Visual analysis:", text)

    def test_image_endpoint_reuses_document_ingestion(self):
        image = UploadFile(
            file=BytesIO(b"png"),
            filename="error.png",
            headers=Headers({"content-type": "image/png"}),
        )
        request = Request({"type": "http", "client": ("127.0.0.1", 5000)})
        expected = {
            "message": "Document processed successfully.",
            "document_id": 7,
            "filename": "error.png",
            "chunk_count": 2,
            "status": "processed",
        }
        with patch(
            "app.routes.images.upload_document",
            new=AsyncMock(return_value=expected),
        ) as ingestion:
            result = asyncio.run(
                upload_image(
                    request=request,
                    image=image,
                    current_user={"id": 3},
                    document_type="screenshot",
                    collection_id=None,
                    upload_batch_id=None,
                    relative_path=None,
                )
            )

        ingestion.assert_awaited_once()
        self.assertEqual(result["document_id"], 7)
        self.assertEqual(result["file_type"], "image/png")
        self.assertEqual(result["document_type"], "screenshot")
        self.assertEqual(result["extraction"], "ocr+vision-with-ocr-fallback")

    def test_image_endpoint_rejects_non_image_extension(self):
        image = UploadFile(file=BytesIO(b"text"), filename="notes.txt")
        request = Request({"type": "http", "client": ("127.0.0.1", 5000)})
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                upload_image(
                    request=request,
                    image=image,
                    current_user={"id": 3},
                    document_type="screenshot",
                    collection_id=None,
                    upload_batch_id=None,
                    relative_path=None,
                )
            )
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
