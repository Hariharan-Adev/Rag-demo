"""Combine local OCR and optional visual understanding into RAG-ready text."""

from __future__ import annotations

from pathlib import Path

from app.services.chunking import chunk_text
from app.services.image_processor.ocr import OcrError, extract_ocr
from app.services.image_processor.vision import describe_image, vision_is_configured

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
)
IMAGE_CHUNK_WORDS = 200
IMAGE_CHUNK_OVERLAP = 40


class ImageProcessingError(ValueError):
    """Raised when neither OCR nor vision can extract useful image information."""

    def __init__(self, message: str, code: str = "image_processing_failed") -> None:
        super().__init__(message)
        self.code = code


def chunk_image_text(text: str) -> list[str]:
    """Use focused chunks so details in dense screenshots remain retrievable."""
    return chunk_text(
        text,
        chunk_size=IMAGE_CHUNK_WORDS,
        overlap=IMAGE_CHUNK_OVERLAP,
    )


def extract_image_text(file_path: Path) -> str:
    """Produce one metadata-rich text document for the shared RAG pipeline."""
    ocr_result = None
    ocr_error: OcrError | None = None
    try:
        ocr_result = extract_ocr(file_path)
    except OcrError as error:
        ocr_error = error

    vision_text = ""
    if vision_is_configured():
        try:
            vision_text = describe_image(
                file_path,
                ocr_result.text if ocr_result is not None else "",
            )
        except Exception:
            # Vision is an enhancement. Local OCR must remain usable during provider outages.
            vision_text = ""

    ocr_text = ocr_result.text if ocr_result is not None else ""
    if not ocr_text and not vision_text:
        if ocr_error is not None:
            raise ImageProcessingError(ocr_error.public_message, code=ocr_error.code) from ocr_error
        raise ImageProcessingError("No readable text was detected in the image.", code="ocr_no_text")

    metadata = [f"Image: {file_path.stem}", "Document type: screenshot"]
    if ocr_result is not None:
        metadata.extend(
            [
                f"File type: image/{file_path.suffix.lower().lstrip('.')}",
                f"Image format: {ocr_result.image_format}",
                f"Dimensions: {ocr_result.width} x {ocr_result.height}",
                f"Frames: {ocr_result.frame_count}",
            ]
        )

    sections = ["\n".join(metadata)]
    if ocr_text:
        sections.append(f"OCR text:\n{ocr_text}")
    if vision_text:
        sections.append(f"Visual analysis:\n{vision_text}")
    return "\n\n".join(sections)
