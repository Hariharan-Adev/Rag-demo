"""Deterministic document naming and content-normalization helpers."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._() -]+")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HORIZONTAL_WHITESPACE = re.compile(r"[ \t]+")
_EXCESS_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def sanitize_filename(filename: str, allowed_extensions: set[str]) -> str:
    """Return a path-free, conservative display filename with a valid extension."""
    client_basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    normalized = unicodedata.normalize("NFKC", client_basename).strip()
    extension = Path(normalized).suffix.lower()
    if extension not in allowed_extensions:
        raise ValueError("Unsupported file type.")

    stem = normalized[: -len(extension)] if extension else normalized
    stem = _CONTROL_CHARACTERS.sub("", stem)
    stem = _UNSAFE_FILENAME.sub("_", stem).strip(" ._")
    if not stem:
        stem = "document"
    return f"{stem[:180]}{extension}"


def generate_unique_display_filename(
    connection: sqlite3.Connection,
    owner_id: int,
    sanitized_filename: str,
) -> str:
    """Generate the next available per-owner display name while preserving its suffix."""
    path = Path(sanitized_filename)
    stem, extension = path.stem, path.suffix
    candidate = sanitized_filename
    suffix = 0
    while connection.execute(
        "SELECT 1 FROM documents WHERE owner_id = ? AND display_filename = ?",
        (owner_id, candidate),
    ).fetchone():
        suffix += 1
        candidate = f"{stem}({suffix}){extension}"
    return candidate


def normalize_extracted_text(text: str) -> str:
    """Normalize extraction noise without destroying paragraph boundaries."""
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_CHARACTERS.sub("", value)
    lines = [_HORIZONTAL_WHITESPACE.sub(" ", line).strip() for line in value.split("\n")]
    value = "\n".join(lines).strip()
    return _EXCESS_BLANK_LINES.sub("\n\n", value)
