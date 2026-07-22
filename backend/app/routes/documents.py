"""Owner-filtered document listing and deletion routes."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import get_current_user
from app.database import UPLOAD_DIRECTORY, get_connection
from app.utils.audit import log_audit_event

router = APIRouter(prefix="/documents", tags=["documents"])

def _resolve_upload_path(stored_filename: str) -> Path | None:
    """Return the upload path only if it remains inside the upload directory."""
    upload_root = UPLOAD_DIRECTORY.resolve()
    candidate = (UPLOAD_DIRECTORY / stored_filename).resolve()

    try:
        candidate.relative_to(upload_root)
    except ValueError:
        return None

    return candidate


@router.get("")
def list_documents(
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """List documents owned by the authenticated user."""
    client_ip = request.client.host if request.client else ""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                documents.id,
                documents.display_filename,
                documents.uploaded_at,
                documents.is_duplicate_content,
                documents.collection_id,
                documents.upload_batch_id,
                documents.relative_path,
                document_collections.name AS collection_name,
                COUNT(chunks.id) AS chunk_count
            FROM documents
            JOIN document_contents ON document_contents.id = documents.content_id
            LEFT JOIN chunks ON chunks.content_id = document_contents.id
            LEFT JOIN document_collections ON document_collections.id = documents.collection_id
            WHERE documents.owner_id = ?
            GROUP BY documents.id
            ORDER BY documents.uploaded_at DESC, documents.id DESC
            """,
            (current_user["id"],),
        ).fetchall()

    documents = [
        {
            "id": row["id"],
            "filename": row["display_filename"],
            "display_filename": row["display_filename"],
            "created_at": row["uploaded_at"],
            "uploaded_at": row["uploaded_at"],
            "chunk_count": row["chunk_count"],
            "content_reused": bool(row["is_duplicate_content"]),
            "collection_id": row["collection_id"],
            "collection_name": row["collection_name"],
            "upload_batch_id": row["upload_batch_id"],
            "relative_path": row["relative_path"],
        }
        for row in rows
    ]

    log_audit_event(
        event_type="document.list",
        endpoint="documents",
        outcome="success",
        user_id=int(current_user["id"]),
        client_ip=client_ip,
        metadata={"document_count": len(documents)},
    )

    return {"documents": documents}


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    request: Request,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Delete one owned file reference and unreferenced processed content."""
    client_ip = request.client.host if request.client else ""

    with get_connection() as connection:
        document = connection.execute(
            """
            SELECT id, display_filename, stored_filename, content_id
            FROM documents
            WHERE id = ? AND owner_id = ?
            """,
            (document_id, current_user["id"]),
        ).fetchone()

        if document is None:
            log_audit_event(
                event_type="document.delete",
                endpoint="documents/{document_id}",
                outcome="not_found",
                user_id=int(current_user["id"]),
                client_ip=client_ip,
                metadata={"document_id": document_id},
            )
            raise HTTPException(status_code=404, detail="Document was not found.")

        stored_filename = str(document["stored_filename"] or "")
        content_id = int(document["content_id"])
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM documents WHERE id = ? AND owner_id = ?",
            (document_id, current_user["id"]),
        )
        remaining = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE content_id = ?",
            (content_id,),
        ).fetchone()[0]
        content_deleted = False
        if remaining == 0:
            connection.execute(
                "DELETE FROM document_contents WHERE id = ? AND owner_id = ?",
                (content_id, current_user["id"]),
            )
            content_deleted = True

    file_deleted = False
    file_note = "No stored file path is available for this record."
    if stored_filename:
        upload_path = _resolve_upload_path(stored_filename)
        if upload_path is None:
            file_note = "Stored file path was not safe to delete automatically."
        elif upload_path.exists():
            try:
                upload_path.unlink()
                file_deleted = True
                file_note = "Stored upload file was deleted."
            except OSError:
                file_note = "Document record was deleted, but its stored file could not be removed."
        else:
            file_note = "Stored upload file was already missing."

    log_audit_event(
        event_type="document.delete",
        endpoint="documents/{document_id}",
        outcome="success",
        user_id=int(current_user["id"]),
        client_ip=client_ip,
        metadata={"document_id": document_id, "file_deleted": file_deleted,
                  "content_deleted": content_deleted},
    )

    return {
        "message": "Document deleted successfully.",
        "document_id": document_id,
        "file_deleted": file_deleted,
        "file_note": file_note,
        "content_deleted": content_deleted,
    }
