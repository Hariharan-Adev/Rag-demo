"""Owner-scoped document collections and upload batches."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.auth import get_current_user
from app.config import settings
from app.database import get_connection
from app.utils.audit import log_audit_event
from app.services.folder_uploads import record_batch_result

router = APIRouter(tags=["collections"])


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        normalized = " ".join(value.split()).strip(" .")
        if not normalized or any(character in normalized for character in "/\\\x00"):
            raise ValueError("Collection name is not valid.")
        return normalized


class BatchCreate(BaseModel):
    collection_id: int = Field(gt=0)
    original_folder_name: str = Field(min_length=1, max_length=100)
    total_files: int = Field(gt=0)
    total_bytes: int = Field(default=0, ge=0)


class BatchSkip(BaseModel):
    count: int = Field(default=1, ge=1, le=100)


def _batch_dict(row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


@router.post("/collections")
def create_collection(payload: CollectionCreate, request: Request, current_user: dict[str, object] = Depends(get_current_user)):
    owner_id = int(current_user["id"])
    organization_id = str(current_user["organization_id"])
    with get_connection() as connection:
        existing = connection.execute(
            "SELECT * FROM document_collections WHERE organization_id = ? AND owner_id = ? AND name = ? COLLATE NOCASE",
            (organization_id, owner_id, payload.name),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        cursor = connection.execute(
            "INSERT INTO document_collections (owner_id, organization_id, name) VALUES (?, ?, ?)",
            (owner_id, organization_id, payload.name),
        )
        row = connection.execute("SELECT * FROM document_collections WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@router.get("/collections")
def list_collections(current_user: dict[str, object] = Depends(get_current_user)):
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT c.*, COUNT(d.id) AS document_count FROM document_collections c
               LEFT JOIN documents d ON d.collection_id = c.id
                    AND d.owner_id = c.owner_id
               WHERE c.organization_id = ? AND c.owner_id = ?
               GROUP BY c.id ORDER BY c.updated_at DESC, c.id DESC""",
            (current_user["organization_id"], current_user["id"]),
        ).fetchall()
    return {"collections": [dict(row) for row in rows]}


@router.get("/collections/{collection_id}")
def get_collection(collection_id: int, current_user: dict[str, object] = Depends(get_current_user)):
    owner_id = int(current_user["id"])
    with get_connection() as connection:
        collection = connection.execute(
            "SELECT * FROM document_collections WHERE id = ? AND organization_id = ? AND owner_id = ?",
            (collection_id, current_user["organization_id"], owner_id)
        ).fetchone()
        if collection is None:
            raise HTTPException(status_code=404, detail="Collection was not found.")
        documents = connection.execute(
            "SELECT id, display_filename AS filename, relative_path, uploaded_at FROM documents WHERE collection_id = ? AND organization_id = ? AND owner_id = ? AND deleted_at IS NULL ORDER BY relative_path, display_filename",
            (collection_id, current_user["organization_id"], owner_id),
        ).fetchall()
    return {"collection": dict(collection), "documents": [dict(row) for row in documents]}


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: int, request: Request, current_user: dict[str, object] = Depends(get_current_user)):
    owner_id = int(current_user["id"])
    with get_connection() as connection:
        exists = connection.execute(
            "SELECT id FROM document_collections WHERE id = ? AND organization_id = ? AND owner_id = ?",
            (collection_id, current_user["organization_id"], owner_id)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Collection was not found.")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE documents SET collection_id = NULL, upload_batch_id = NULL, relative_path = NULL WHERE collection_id = ? AND organization_id = ? AND owner_id = ?",
            (collection_id, current_user["organization_id"], owner_id),
        )
        connection.execute("DELETE FROM document_collections WHERE id = ? AND organization_id = ? AND owner_id = ?", (collection_id, current_user["organization_id"], owner_id))
    log_audit_event(event_type="folder.collection_deleted", endpoint="collections/{collection_id}", outcome="success", user_id=owner_id, client_ip=request.client.host if request.client else "", metadata={"collection_id": collection_id})
    return {"message": "Collection deleted; its documents remain available.", "collection_id": collection_id}


@router.post("/upload-batches")
def create_batch(payload: BatchCreate, request: Request, current_user: dict[str, object] = Depends(get_current_user)):
    owner_id = int(current_user["id"])
    if payload.total_files > settings.max_folder_files:
        raise HTTPException(status_code=400, detail=f"A folder may contain at most {settings.max_folder_files} files.")
    if payload.total_bytes > settings.max_folder_total_size_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Folder size may not exceed {settings.max_folder_total_size_mb} MB.")
    with get_connection() as connection:
        collection = connection.execute(
            "SELECT id FROM document_collections WHERE id = ? AND organization_id = ? AND owner_id = ?",
            (payload.collection_id, current_user["organization_id"], owner_id)
        ).fetchone()
        if collection is None:
            raise HTTPException(status_code=404, detail="Collection was not found.")
        cursor = connection.execute(
            """INSERT INTO upload_batches
               (owner_id, organization_id, collection_id, original_folder_name, total_files, total_bytes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (owner_id, current_user["organization_id"], payload.collection_id, payload.original_folder_name, payload.total_files, payload.total_bytes),
        )
        row = connection.execute("SELECT * FROM upload_batches WHERE id = ?", (cursor.lastrowid,)).fetchone()
    log_audit_event(event_type="folder.batch_created", endpoint="upload-batches", outcome="success", user_id=owner_id, client_ip=request.client.host if request.client else "", metadata={"batch_id": row["id"], "collection_id": payload.collection_id, "total_files": payload.total_files})
    return _batch_dict(row)


@router.get("/upload-batches/{batch_id}")
def get_batch(batch_id: int, current_user: dict[str, object] = Depends(get_current_user)):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM upload_batches WHERE id = ? AND organization_id = ? AND owner_id = ?",
            (batch_id, current_user["organization_id"], current_user["id"])
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload batch was not found.")
    return _batch_dict(row)


@router.post("/upload-batches/{batch_id}/skip")
def skip_batch_files(batch_id: int, payload: BatchSkip, current_user: dict[str, object] = Depends(get_current_user)):
    owner_id = int(current_user["id"])
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id FROM upload_batches WHERE id = ? AND organization_id = ? AND owner_id = ?",
            (batch_id, current_user["organization_id"], owner_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload batch was not found.")
    for _ in range(payload.count):
        record_batch_result(batch_id, owner_id, "skipped")
    return get_batch(batch_id, current_user)


@router.post("/upload-batches/{batch_id}/cancel")
def cancel_batch(batch_id: int, request: Request, current_user: dict[str, object] = Depends(get_current_user)):
    owner_id = int(current_user["id"])
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE upload_batches SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP WHERE id = ? AND organization_id = ? AND owner_id = ? AND status NOT IN ('completed', 'partially_completed', 'failed')",
            (batch_id, current_user["organization_id"], owner_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Active upload batch was not found.")
    log_audit_event(event_type="folder.batch_cancelled", endpoint="upload-batches/{batch_id}/cancel", outcome="success", user_id=owner_id, client_ip=request.client.host if request.client else "", metadata={"batch_id": batch_id})
    return {"status": "cancelled", "batch_id": batch_id}
