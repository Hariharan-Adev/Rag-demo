"""Tenant-scoped document lifecycle, version, delete, and restore APIs."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import get_current_user
from app.config import settings
from app.database import UPLOAD_DIRECTORY, get_connection
from app.services.storage import resolve_storage_key
from app.services.document_access import (
    MANAGEABLE_DOCUMENT_SQL,
    READABLE_DOCUMENT_SQL,
    Visibility,
    require_document,
)
from app.services.vector_store import get_vector_store
from app.utils.audit import log_audit_event

router = APIRouter(prefix="/documents", tags=["documents"])


_PREVIEW_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".xml": "application/xml",
    # HTML is intentionally served as source text, never as active content.
    ".html": "text/plain; charset=utf-8",
    ".htm": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
}


class VisibilityUpdate(BaseModel):
    visibility: Visibility


class ShareCreate(BaseModel):
    user_id: int
    permission: str = "read"


def _complete_user(user: dict[str, object]) -> dict[str, object]:
    """Populate tenant context for deprecated direct Python callers."""
    if "organization_id" in user and "role" in user:
        return user
    with get_connection() as connection:
        row = connection.execute(
            "SELECT organization_id, role, email FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="User was not found.")
    return {**user, **dict(row)}


def _version_dict(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "version_number": row["version_number"],
        "status": row["status"],
        "file_hash": row["file_hash"],
        "storage_key": row["storage_key"],
        "mime_type": row["mime_type"],
        "file_size": row["file_size"],
        "ingestion_status": row["ingestion_status"],
        "extraction_status": row["extraction_status"],
        "indexing_status": row["indexing_status"],
        "source_metadata": json.loads(row["source_metadata_json"] or "{}"),
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "is_current": bool(row["is_current"]),
        "deleted_at": row["deleted_at"],
        "error": (
            {
                "code": row["processing_error_code"],
                "message": row["processing_error_message"],
            }
            if row["processing_error_code"] or row["processing_error_message"]
            else None
        ),
    }


def _soft_delete_orphan_contents(
    connection,
    organization_id: str,
    deleted_by: int,
    *,
    deleted_with_document: bool,
) -> None:
    """Retire content only when no live version in the tenant still references it."""
    connection.execute(
        """UPDATE document_contents
           SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
               deleted_with_document = ?
           WHERE organization_id = ? AND deleted_at IS NULL
             AND NOT EXISTS (
                 SELECT 1
                 FROM document_versions dv
                 JOIN documents d ON d.id = dv.document_id
                 WHERE dv.content_id = document_contents.id
                   AND dv.organization_id = document_contents.organization_id
                   AND dv.deleted_at IS NULL
                   AND d.deleted_at IS NULL
             )""",
        (deleted_by, int(deleted_with_document), organization_id),
    )


@router.get("")
def list_documents(
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """List every non-deleted document readable by the authenticated principal."""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT d.id, d.display_filename, d.uploaded_at, d.visibility,
                   d.owner_id, d.collection_id, d.upload_batch_id, d.relative_path,
                   d.processing_status, d.current_version_id,
                   dc.name AS collection_name, dv.version_number,
                   dv.file_size, dv.mime_type,
                   COALESCE(COUNT(c.id), 0) AS chunk_count
            FROM documents d
            LEFT JOIN document_collections dc ON dc.id = d.collection_id
            LEFT JOIN document_versions dv ON dv.id = d.current_version_id
            LEFT JOIN chunks c ON c.version_id = dv.id AND c.deleted_at IS NULL
            WHERE {READABLE_DOCUMENT_SQL}
            GROUP BY d.id
            ORDER BY d.uploaded_at DESC, d.id DESC
            """,
            (
                current_user["organization_id"],
                current_user["id"],
                current_user["id"],
            ),
        ).fetchall()
    return {
        "documents": [
            {
                "id": row["id"],
                "filename": row["display_filename"],
                "display_filename": row["display_filename"],
                "created_at": row["uploaded_at"],
                "uploaded_at": row["uploaded_at"],
                "chunk_count": row["chunk_count"],
                "collection_id": row["collection_id"],
                "collection_name": row["collection_name"],
                "upload_batch_id": row["upload_batch_id"],
                "relative_path": row["relative_path"],
                "visibility": row["visibility"],
                "owner_id": row["owner_id"],
                "status": row["processing_status"],
                "current_version_id": row["current_version_id"],
                "current_version_number": row["version_number"],
                "file_size": row["file_size"],
                "mime_type": row["mime_type"],
            }
            for row in rows
        ]
    }


@router.get("/trash")
def list_deleted_documents(
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    predicate = MANAGEABLE_DOCUMENT_SQL.replace("AND d.deleted_at IS NULL", "")
    with get_connection() as connection:
        rows = connection.execute(
            f"""SELECT d.id, d.display_filename, d.deleted_at, d.current_version_id
                FROM documents d
                WHERE d.deleted_at IS NOT NULL AND {predicate}
                ORDER BY d.deleted_at DESC""",
            (
                current_user["organization_id"], current_user["id"],
                current_user["role"], current_user["id"],
            ),
        ).fetchall()
    return {"documents": [dict(row) for row in rows]}


@router.get("/{document_id}")
def get_document(
    document_id: int,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        document = require_document(connection, document_id, current_user)
        version = connection.execute(
            """SELECT dv.*, 1 AS is_current FROM document_versions dv
               WHERE dv.id = ? AND dv.organization_id = ? AND dv.deleted_at IS NULL""",
            (document["current_version_id"], current_user["organization_id"]),
        ).fetchone()
    return {
        "document": {
            "id": document["id"],
            "filename": document["display_filename"],
            "visibility": document["visibility"],
            "owner_id": document["owner_id"],
            "created_at": document["uploaded_at"],
            "current_version": _version_dict(version) if version else None,
        }
    }


@router.get("/{document_id}/content", response_class=FileResponse)
def get_document_content(
    document_id: int,
    download: bool = False,
    current_user: dict[str, object] = Depends(get_current_user),
) -> FileResponse:
    """Stream an authorized document's current file for preview or download."""
    with get_connection() as connection:
        document = require_document(connection, document_id, current_user)
        version = connection.execute(
            """SELECT storage_key, stored_filename
               FROM document_versions
               WHERE id = ? AND document_id = ? AND organization_id = ?
                 AND deleted_at IS NULL""",
            (
                document["current_version_id"], document_id,
                current_user["organization_id"],
            ),
        ).fetchone()

    if version is None:
        raise HTTPException(status_code=404, detail="Document content was not found.")

    suffix = Path(str(document["display_filename"])).suffix.lower()
    media_type = _PREVIEW_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(status_code=415, detail="This document type cannot be previewed.")

    storage_key = str(version["storage_key"] or version["stored_filename"] or "")
    try:
        path = resolve_storage_key(storage_key)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Document content was not found.") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document content was not found.")

    return FileResponse(
        path,
        media_type=media_type,
        filename=str(document["display_filename"]),
        content_disposition_type="attachment" if download else "inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/{document_id}/versions")
def list_versions(
    document_id: int,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        document = require_document(connection, document_id, current_user)
        rows = connection.execute(
            """SELECT dv.*, dv.id = ? AS is_current
               FROM document_versions dv
               WHERE dv.organization_id = ? AND dv.document_id = ?
                 AND dv.deleted_at IS NULL
               ORDER BY dv.version_number DESC""",
            (
                document["current_version_id"],
                current_user["organization_id"],
                document_id,
            ),
        ).fetchall()
    return {"versions": [_version_dict(row) for row in rows]}


@router.get("/{document_id}/versions/{version_id}")
def get_version(
    document_id: int,
    version_id: int,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        document = require_document(connection, document_id, current_user)
        row = connection.execute(
            """SELECT dv.*, dv.id = ? AS is_current
               FROM document_versions dv
               WHERE dv.id = ? AND dv.document_id = ?
                 AND dv.organization_id = ? AND dv.deleted_at IS NULL""",
            (
                document["current_version_id"], version_id, document_id,
                current_user["organization_id"],
            ),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document version was not found.")
    return {"version": _version_dict(row)}


@router.post("/{document_id}/versions/{version_id}/make-current")
def make_version_current(
    document_id: int,
    version_id: int,
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        require_document(connection, document_id, current_user, manage=True)
        version = connection.execute(
            """SELECT id, content_id FROM document_versions
               WHERE id = ? AND document_id = ? AND organization_id = ?
                 AND status = 'completed' AND deleted_at IS NULL""",
            (version_id, document_id, current_user["organization_id"]),
        ).fetchone()
        if version is None:
            raise HTTPException(
                status_code=409,
                detail="Only a completed, active version can be made current.",
            )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE documents SET current_version_id = ?, content_id = ?,
               processing_status = 'completed', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND organization_id = ?""",
            (
                version_id, version["content_id"], document_id,
                current_user["organization_id"],
            ),
        )
    log_audit_event(
        event_type="document.version.make_current",
        endpoint="documents/{document_id}/versions/{version_id}/make-current",
        outcome="success",
        user_id=int(current_user["id"]),
        organization_id=str(current_user["organization_id"]),
        client_ip=request.client.host if request.client else "",
        metadata={"document_id": document_id, "version_id": version_id},
    )
    return {"document_id": document_id, "current_version_id": version_id}


@router.delete("/{document_id}/versions/{version_id}")
def delete_version(
    document_id: int,
    version_id: int,
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Soft-delete a non-current version without mutating its retained content."""
    current_user = _complete_user(current_user)
    organization_id = str(current_user["organization_id"])
    user_id = int(current_user["id"])
    with get_connection() as connection:
        document = require_document(
            connection, document_id, current_user, manage=True
        )
        version = connection.execute(
            """SELECT id FROM document_versions
               WHERE id = ? AND document_id = ? AND organization_id = ?
                 AND deleted_at IS NULL""",
            (version_id, document_id, organization_id),
        ).fetchone()
        if version is None:
            raise HTTPException(status_code=404, detail="Document version was not found.")
        if int(document["current_version_id"] or 0) == version_id:
            raise HTTPException(
                status_code=409,
                detail="Make another completed version current before deleting this version.",
            )

    vector_store = get_vector_store()
    vector_store.set_version_deleted(
        organization_id, document_id, version_id, True
    )
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE document_versions
                   SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                       deleted_with_document = 0
                   WHERE id = ? AND document_id = ? AND organization_id = ?""",
                (user_id, version_id, document_id, organization_id),
            )
            connection.execute(
                """UPDATE chunks
                   SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                       deleted_with_document = 0
                   WHERE version_id = ? AND document_id = ? AND organization_id = ?
                     AND deleted_at IS NULL""",
                (user_id, version_id, document_id, organization_id),
            )
            _soft_delete_orphan_contents(
                connection,
                organization_id,
                user_id,
                deleted_with_document=False,
            )
    except Exception:
        vector_store.set_version_deleted(
            organization_id, document_id, version_id, False
        )
        raise
    log_audit_event(
        event_type="document.version.delete",
        endpoint="documents/{document_id}/versions/{version_id}",
        outcome="soft_deleted",
        user_id=user_id,
        organization_id=organization_id,
        client_ip=request.client.host if request.client else "",
        metadata={"document_id": document_id, "version_id": version_id},
    )
    return {
        "document_id": document_id,
        "version_id": version_id,
        "soft_deleted": True,
    }


@router.patch("/{document_id}/visibility")
def update_visibility(
    document_id: int,
    payload: VisibilityUpdate,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    organization_id = str(current_user["organization_id"])
    with get_connection() as connection:
        require_document(connection, document_id, current_user, manage=True)
    vector_store = get_vector_store()
    vector_store.set_document_visibility(
        organization_id, document_id, payload.visibility.value
    )
    try:
        with get_connection() as connection:
            connection.execute(
                """UPDATE documents
                   SET visibility = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND organization_id = ?""",
                (payload.visibility.value, document_id, organization_id),
            )
    except Exception:
        # Recover the previous value if the relational update fails.
        with get_connection() as connection:
            previous = connection.execute(
                "SELECT visibility FROM documents WHERE id = ? AND organization_id = ?",
                (document_id, organization_id),
            ).fetchone()
        if previous:
            vector_store.set_document_visibility(
                organization_id, document_id, str(previous["visibility"])
            )
        raise
    return {"document_id": document_id, "visibility": payload.visibility.value}


@router.post("/{document_id}/shares", status_code=201)
def share_document(
    document_id: int,
    payload: ShareCreate,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    if payload.permission not in {"read", "manage"}:
        raise HTTPException(status_code=422, detail="Permission must be read or manage.")
    organization_id = str(current_user["organization_id"])
    with get_connection() as connection:
        require_document(connection, document_id, current_user, manage=True)
        target = connection.execute(
            """SELECT id FROM users
               WHERE id = ? AND organization_id = ? AND deleted_at IS NULL""",
            (payload.user_id, organization_id),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User was not found.")
        connection.execute(
            """INSERT INTO document_permissions
               (organization_id, document_id, user_id, permission)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(organization_id, document_id, user_id, permission)
               DO NOTHING""",
            (organization_id, document_id, payload.user_id, payload.permission),
        )
    return {
        "document_id": document_id,
        "user_id": payload.user_id,
        "permission": payload.permission,
    }


@router.delete("/{document_id}/shares/{user_id}")
def revoke_document_share(
    document_id: int,
    user_id: int,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    organization_id = str(current_user["organization_id"])
    with get_connection() as connection:
        require_document(connection, document_id, current_user, manage=True)
        connection.execute(
            """DELETE FROM document_permissions
               WHERE organization_id = ? AND document_id = ? AND user_id = ?""",
            (organization_id, document_id, user_id),
        )
    return {"document_id": document_id, "user_id": user_id, "revoked": True}


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Soft-delete a document and immediately exclude all of its vector points."""
    current_user = _complete_user(current_user)
    organization_id = str(current_user["organization_id"])
    vector_store = get_vector_store()
    with get_connection() as connection:
        require_document(connection, document_id, current_user, manage=True)
    vector_updated = False
    try:
        vector_store.set_document_deleted(organization_id, document_id, True)
        vector_updated = True
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE documents
                   SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND organization_id = ?""",
                (int(current_user["id"]), document_id, organization_id),
            )
            connection.execute(
                """UPDATE document_versions
                   SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                       deleted_with_document = 1
                   WHERE document_id = ? AND organization_id = ? AND deleted_at IS NULL""",
                (int(current_user["id"]), document_id, organization_id),
            )
            connection.execute(
                """UPDATE chunks
                   SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                       deleted_with_document = 1
                   WHERE document_id = ? AND organization_id = ? AND deleted_at IS NULL""",
                (int(current_user["id"]), document_id, organization_id),
            )
            connection.execute(
                """UPDATE ingestion_jobs SET status = 'cancelled',
                   completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                   WHERE document_id = ? AND organization_id = ?
                     AND status IN ('queued','retry_scheduled')""",
                (document_id, organization_id),
            )
            _soft_delete_orphan_contents(
                connection,
                organization_id,
                int(current_user["id"]),
                deleted_with_document=True,
            )
            connection.execute(
                """UPDATE document_versions SET status = 'cancelled'
                   WHERE document_id = ? AND organization_id = ?
                     AND status = 'queued'""",
                (document_id, organization_id),
            )
    except Exception:
        if vector_updated:
            vector_store.set_document_deleted(organization_id, document_id, False)
        raise
    log_audit_event(
        event_type="document.delete",
        endpoint="documents/{document_id}",
        outcome="soft_deleted",
        user_id=int(current_user["id"]),
        organization_id=organization_id,
        client_ip=request.client.host if request.client else "",
        metadata={"document_id": document_id},
    )
    return {
        "message": "Document deleted successfully.",
        "document_id": document_id,
        "soft_deleted": True,
        "file_deleted": False,
        "file_note": "Stored files are retained until an authorized hard delete.",
    }


@router.post("/{document_id}/restore")
def restore_document(
    document_id: int,
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    organization_id = str(current_user["organization_id"])
    vector_store = get_vector_store()
    with get_connection() as connection:
        document = require_document(
            connection, document_id, current_user, manage=True, include_deleted=True
        )
        valid_current = connection.execute(
            """SELECT dv.id, dv.content_id, dc.normalized_content_hash
               FROM document_versions dv
               JOIN document_contents dc ON dc.id = dv.content_id
               WHERE dv.id = ? AND dv.document_id = ? AND dv.organization_id = ?
                 AND dv.status = 'completed'""",
            (document["current_version_id"], document_id, organization_id),
        ).fetchone()
        if valid_current is None:
            raise HTTPException(
                status_code=409,
                detail="The document has no successfully indexed version to restore.",
            )
        active_duplicate = connection.execute(
            """SELECT 1
               FROM documents d
               JOIN document_versions dv ON dv.id = d.current_version_id
               JOIN document_contents dc ON dc.id = dv.content_id
               WHERE d.id <> ? AND d.organization_id = ? AND d.owner_id = ?
                 AND d.deleted_at IS NULL AND dv.deleted_at IS NULL
                 AND dv.status = 'completed' AND dc.deleted_at IS NULL
                 AND dc.normalized_content_hash = ?
               LIMIT 1""",
            (
                document_id, organization_id, document["owner_id"],
                valid_current["normalized_content_hash"],
            ),
        ).fetchone()
        if active_duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DOCUMENT_ALREADY_EXISTS",
                    "message": "An identical active document already exists.",
                    "retryable": False,
                },
            )
    vector_store.set_document_deleted(organization_id, document_id, False)
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE documents
                   SET deleted_at = NULL, deleted_by = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND organization_id = ?""",
                (document_id, organization_id),
            )
            connection.execute(
                """UPDATE document_versions
                   SET deleted_at = NULL, deleted_by = NULL,
                       deleted_with_document = 0
                   WHERE document_id = ? AND organization_id = ?
                     AND status = 'completed' AND deleted_with_document = 1""",
                (document_id, organization_id),
            )
            connection.execute(
                """UPDATE chunks
                   SET deleted_at = NULL, deleted_by = NULL,
                       deleted_with_document = 0
                   WHERE document_id = ? AND organization_id = ?
                     AND deleted_with_document = 1
                     AND version_id IN (
                         SELECT id FROM document_versions
                         WHERE document_id = ? AND organization_id = ?
                           AND status = 'completed' AND deleted_at IS NULL
                     )""",
                (document_id, organization_id, document_id, organization_id),
            )
            connection.execute(
                """UPDATE document_contents
                   SET deleted_at = NULL, deleted_by = NULL,
                       deleted_with_document = 0
                   WHERE organization_id = ? AND deleted_with_document = 1
                     AND id IN (
                       SELECT content_id FROM document_versions
                       WHERE document_id = ? AND organization_id = ?
                         AND status = 'completed' AND deleted_at IS NULL
                   )""",
                (organization_id, document_id, organization_id),
            )
    except Exception:
        vector_store.set_document_deleted(organization_id, document_id, True)
        raise
    log_audit_event(
        event_type="document.restore",
        endpoint="documents/{document_id}/restore",
        outcome="success",
        user_id=int(current_user["id"]),
        organization_id=str(current_user["organization_id"]),
        client_ip=request.client.host if request.client else "",
        metadata={"document_id": document_id},
    )
    return {"document_id": document_id, "restored": True}


@router.delete("/{document_id}/hard")
def hard_delete_document(
    document_id: int,
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    if not settings.hard_delete_enabled or current_user["role"] != "organization_admin":
        raise HTTPException(status_code=404, detail="Document was not found.")
    organization_id = str(current_user["organization_id"])
    with get_connection() as connection:
        require_document(
            connection, document_id, current_user, manage=True, include_deleted=True
        )
        versions = connection.execute(
            """SELECT stored_filename, storage_key, content_id FROM document_versions
               WHERE document_id = ? AND organization_id = ?""",
            (document_id, organization_id),
        ).fetchall()
    get_vector_store().delete_document(organization_id, document_id)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM chunks WHERE document_id = ? AND organization_id = ?",
            (document_id, organization_id),
        )
        connection.execute(
            "DELETE FROM ingestion_jobs WHERE document_id = ? AND organization_id = ?",
            (document_id, organization_id),
        )
        connection.execute(
            "DELETE FROM document_permissions WHERE document_id = ? AND organization_id = ?",
            (document_id, organization_id),
        )
        connection.execute(
            "DELETE FROM document_versions WHERE document_id = ? AND organization_id = ?",
            (document_id, organization_id),
        )
        connection.execute(
            "DELETE FROM documents WHERE id = ? AND organization_id = ?",
            (document_id, organization_id),
        )
        for version in versions:
            connection.execute(
                """DELETE FROM document_contents
                   WHERE id = ? AND organization_id = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM document_versions WHERE content_id = ?
                     )""",
                (version["content_id"], organization_id, version["content_id"]),
            )
    deleted_files = 0
    upload_root = UPLOAD_DIRECTORY.resolve()
    for version in versions:
        candidate = resolve_storage_key(
            str(version["storage_key"] or version["stored_filename"])
        )
        try:
            candidate.relative_to(upload_root)
        except ValueError:
            continue
        if candidate.is_file():
            candidate.unlink()
            deleted_files += 1
    log_audit_event(
        event_type="document.hard_delete",
        endpoint="documents/{document_id}/hard",
        outcome="hard_deleted",
        user_id=int(current_user["id"]),
        organization_id=organization_id,
        client_ip=request.client.host if request.client else "",
        metadata={"document_id": document_id, "deleted_files": deleted_files},
    )
    return {
        "document_id": document_id,
        "hard_deleted": True,
        "deleted_files": deleted_files,
    }
