"""Local OCR extraction without coupling image handling to document routing."""

from __future__ import annotations

import os
import shutil
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

MAX_IMAGE_PIXELS = 40_000_000
OCR_TIMEOUT_SECONDS = 45
OCR_UNAVAILABLE_MESSAGE = "OCR is currently unavailable. Please contact the administrator or try again later."
OCR_FAILED_MESSAGE = "The file could not be processed because image text extraction failed."
OCR_TIMEOUT_MESSAGE = "Image text extraction exceeded the processing limit."

logger = logging.getLogger(__name__)


class OcrError(ValueError):
    """Raised when a supported image cannot be processed by local OCR."""

    code = "ocr_processing_failed"
    public_message = OCR_FAILED_MESSAGE


class OcrUnavailableError(OcrError):
    """Raised when the local Tesseract executable or language data is unavailable."""

    code = "ocr_unavailable"
    public_message = OCR_UNAVAILABLE_MESSAGE


class OcrTimeoutError(OcrError):
    """Raised when local OCR exceeds the bounded processing timeout."""

    code = "ocr_timeout"
    public_message = OCR_TIMEOUT_MESSAGE


@dataclass(frozen=True)
class OcrResult:
    text: str
    image_format: str
    width: int
    height: int
    frame_count: int


def _configure_tesseract(pytesseract) -> None:
    """Find common Windows installs when the current process has a stale PATH."""
    explicit = settings.tesseract_cmd.strip()
    if explicit:
        pytesseract.pytesseract.tesseract_cmd = explicit
        return
    configured = str(pytesseract.pytesseract.tesseract_cmd)
    if shutil.which(configured):
        return

    candidates = [
        str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
        if os.getenv("LOCALAPPDATA")
        else None,
        str(Path(os.environ["ProgramFiles"]) / "Tesseract-OCR" / "tesseract.exe")
        if os.getenv("ProgramFiles")
        else None,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


def ocr_health() -> dict[str, str]:
    """Return a safe OCR readiness status without leaking executable paths."""
    try:
        import pytesseract

        _configure_tesseract(pytesseract)
        pytesseract.get_tesseract_version()
        required = [
            value.strip()
            for value in settings.ocr_required_languages.split(",")
            if value.strip()
        ]
        if required:
            available = set(pytesseract.get_languages(config=""))
            missing = sorted(set(required) - available)
            if missing:
                logger.error("OCR language data is unavailable", extra={"missing_languages": missing})
                return {"status": "unavailable"}
    except Exception as error:
        logger.error("OCR readiness check failed", extra={"error_type": type(error).__name__})
        return {"status": "unavailable"}
    return {"status": "ready"}


def require_ocr_ready_for_startup() -> None:
    """Fail fast in production when image OCR cannot run for OCR-dependent uploads."""
    if settings.app_environment != "production":
        return
    if ocr_health()["status"] != "ready":
        raise RuntimeError("OCR dependency is unavailable.")


def extract_ocr(file_path: Path) -> OcrResult:
    """Extract visible text and safe image metadata from every image frame."""
    try:
        import pytesseract
        from PIL import Image, ImageOps, ImageSequence

        _configure_tesseract(pytesseract)
        output: list[str] = []
        with Image.open(file_path) as image:
            width, height = image.size
            frame_count = int(getattr(image, "n_frames", 1))
            image_format = str(image.format or file_path.suffix.lstrip(".")).upper()
            for index, frame in enumerate(ImageSequence.Iterator(image), start=1):
                frame_width, frame_height = frame.size
                if frame_width * frame_height > MAX_IMAGE_PIXELS:
                    raise OcrError("The image dimensions are too large to process safely.")
                prepared = ImageOps.exif_transpose(frame.copy()).convert("RGB")
                prepared = ImageOps.autocontrast(ImageOps.grayscale(prepared))
                text = pytesseract.image_to_string(
                    prepared,
                    timeout=OCR_TIMEOUT_SECONDS,
                ).strip()
                if text:
                    if frame_count > 1:
                        output.append(f"Frame {index}")
                    output.append(text)
        return OcrResult(
            text="\n".join(output),
            image_format=image_format,
            width=width,
            height=height,
            frame_count=frame_count,
        )
    except OcrError:
        raise
    except Exception as error:
        try:
            import pytesseract

            if isinstance(error, pytesseract.TesseractNotFoundError):
                logger.error("Tesseract executable is unavailable", extra={"error_type": type(error).__name__})
                raise OcrUnavailableError(OCR_UNAVAILABLE_MESSAGE) from error
            if isinstance(error, RuntimeError) and "timeout" in str(error).lower():
                raise OcrTimeoutError(OCR_TIMEOUT_MESSAGE) from error
        except ImportError:
            pass
        raise OcrError(OCR_FAILED_MESSAGE) from error
