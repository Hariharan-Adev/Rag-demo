"""Uploaded file size and conservative signature validation."""

from io import BytesIO
from pathlib import Path, PurePosixPath
import zipfile

from fastapi import HTTPException
from app.config import settings

_SIGNATURES = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    ".webp": (b"RIFF",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".doc": (b"\xd0\xcf\x11\xe0",),
    ".xls": (b"\xd0\xcf\x11\xe0",),
    ".ppt": (b"\xd0\xcf\x11\xe0",),
}


def validate_file_signature(filename: str, content: bytes) -> None:
    """Reject obvious executable masquerades and malformed binary formats."""
    if content.startswith(b"MZ"):
        raise HTTPException(status_code=400, detail="Executable files are not supported.")
    suffix = Path(filename).suffix.lower()
    signatures = _SIGNATURES.get(suffix)
    if signatures and not any(content.startswith(signature) for signature in signatures):
        raise HTTPException(status_code=400, detail="The file content does not match its extension.")
    if suffix == ".webp" and (len(content) < 12 or content[8:12] != b"WEBP"):
        raise HTTPException(status_code=400, detail="The file content does not match its extension.")
    if suffix in {".docx", ".xlsx", ".pptx"}:
        _validate_office_archive(content)


def _validate_office_archive(content: bytes) -> None:
    """Reject traversal, encryption, oversized expansion, and archive bombs."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > settings.max_office_archive_entries:
                raise ValueError("Office document contains too many archive entries.")
            total_uncompressed = 0
            total_compressed = 0
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Office document contains an unsafe archive path.")
                if entry.flag_bits & 0x1:
                    raise ValueError("Encrypted Office documents are not supported.")
                total_uncompressed += entry.file_size
                total_compressed += entry.compress_size
            if total_uncompressed > settings.max_office_uncompressed_mb * 1024 * 1024:
                raise ValueError("Office document expands beyond the allowed size.")
            ratio = total_uncompressed / max(total_compressed, 1)
            if ratio > settings.max_office_compression_ratio:
                raise ValueError("Office document compression ratio is unsafe.")
    except (zipfile.BadZipFile, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
