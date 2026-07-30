"""Organization-scoped upload storage with traversal-safe resolution."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from app import database


def organization_storage_prefix(organization_id: str) -> str:
    """Return an opaque, filesystem-safe tenant partition."""
    return sha256(organization_id.encode("utf-8")).hexdigest()


def storage_key_for(organization_id: str, stored_filename: str) -> str:
    filename = Path(stored_filename).name
    if not filename or filename != stored_filename:
        raise ValueError("Invalid stored filename.")
    return f"{organization_storage_prefix(organization_id)}/{filename}"


def resolve_storage_key(storage_key: str) -> Path:
    """Resolve a relative key beneath the upload root.

    Plain filenames remain readable for data migrated from the legacy flat layout.
    """
    root = database.UPLOAD_DIRECTORY.resolve()
    candidate = (root / storage_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Invalid storage key.") from error
    return candidate


def write_storage_bytes(storage_key: str, content: bytes) -> Path:
    path = resolve_storage_key(storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
