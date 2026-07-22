"""Uploaded file size and conservative signature validation."""

from pathlib import Path

from fastapi import HTTPException

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
