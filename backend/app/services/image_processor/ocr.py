"""Local OCR extraction without coupling image handling to document routing."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MAX_IMAGE_PIXELS = 40_000_000
OCR_TIMEOUT_SECONDS = 45


class OcrError(ValueError):
    """Raised when a supported image cannot be processed by local OCR."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    image_format: str
    width: int
    height: int
    frame_count: int


def _configure_tesseract(pytesseract) -> None:
    """Find common Windows installs when the current process has a stale PATH."""
    configured = str(pytesseract.pytesseract.tesseract_cmd)
    if shutil.which(configured):
        return

    candidates = [
        os.getenv("TESSERACT_CMD"),
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
                raise OcrError(
                    "OCR is unavailable because the Tesseract service is not installed."
                ) from error
            if isinstance(error, RuntimeError) and "timeout" in str(error).lower():
                raise OcrError("OCR processing timed out.") from error
        except ImportError:
            pass
        raise OcrError("The image could not be read or OCR processing failed.") from error
