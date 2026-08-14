"""Asynchronous, idempotent document ingestion and job-control endpoints."""

from __future__ import annotations

from hashlib import sha256
import mimetypes
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status

from app.auth import get_current_user
from app.config import settings
from app.database import UPLOAD_DIRECTORY, get_connection
from app.services.document_access import Visibility, require_document
from app.services.document_loader import SUPPORTED_EXTENSIONS
from app.services.ingestion_jobs import enqueue_archive_job, enqueue_job
from app.services.folder_uploads import sanitize_relative_path, validate_upload_context
from app.utils.audit import log_audit_event
from app.utils.document_content import generate_unique_display_filename, sanitize_filename
from app.utils.file_validation import validate_file_signature
from app.utils.rate_limit import enforce_request_limit
from app.services.storage import storage_key_for, write_storage_bytes
from app.utils.observability import log_event

router = APIRouter(prefix="/api", tags=["ingestion-jobs"])
public_router = APIRouter(tags=["ingestion-jobs"])


class _ExistingUploadRequest(Exception):
    def __init__(self, job: dict[str, object]) -> None:
        self.job = job


def _job_response(row) -> dict[str, object]:
    return {
        "job_id": row["id"],
        "status": row["status"],
        "document_id": row["document_id"],
        "version_id": row["version_id"],
        "attempt_count": row["attempt_count"],
        "max_attempts": row["max_attempts"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "next_retry_at": row["next_retry_at"],
        "pipeline_version": row["pipeline_version"],
        "error": (
            {
                "code": row["last_error_code"] or row["error_code"],
                "message": row["last_error_message"] or row["error_message"],
                "retryable": row["status"] == "retry_scheduled",
            }
            if (
                row["last_error_code"] or row["error_code"]
                or row["last_error_message"] or row["error_message"]
            )
            else None
        ),
        "result": (
            __import__("json").loads(row["result_json"])
            if row["result_json"] else None
        ),
    }


@public_router.post("/documents/upload", status_code=status.HTTP_202_ACCEPTED)
@router.post("/documents/upload", status_code=status.HTTP_202_ACCEPTED)
async def queue_document_upload(
    request: Request,
    file: UploadFile = File(...),
    visibility: Visibility = Form(default=Visibility.PRIVATE),
    document_id: int | None = Form(default=None),
    collection_id: int | None = Form(default=None),
    upload_batch_id: int | None = Form(default=None),
    relative_path: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    """Persist the upload and transactionally enqueue ingestion; never process inline."""
    owner_id = int(current_user["id"])
    organization_id = str(current_user["organization_id"])
    explicit_version = request.url.path.rstrip("/").endswith("/versions")
    client_ip = request.client.host if request.client else "unknown"
    # The batch itself is rate-limited at creation; counting every member would
    # make the configured folder limit unreachable under the default quota.
    if upload_batch_id is None:
        enforce_request_limit(owner_id, client_ip, "upload", settings.uploads_per_hour)
    try:
        original_filename = sanitize_filename(
            file.filename or "", set(SUPPORTED_EXTENSIONS)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum file size is {settings.max_file_size_mb} MB.",
        )
    validate_file_signature(original_filename, content)
    safe_relative_path = sanitize_relative_path(relative_path, original_filename)
    validate_upload_context(owner_id, collection_id, upload_batch_id)
    file_hash = sha256(content).hexdigest()
    mime_type = file.content_type or mimetypes.guess_type(original_filename)[0]
    supplied_key = (idempotency_key or "").strip()
    # Only an explicit request key denotes a transport retry. A fresh request
    # with identical content must still pass through duplicate policy checks.
    effective_key = supplied_key or str(uuid4())
    with get_connection() as connection:
        existing_job = connection.execute(
            """SELECT * FROM ingestion_jobs
               WHERE organization_id = ? AND request_idempotency_key = ?""",
            (organization_id, effective_key),
        ).fetchone()
        if existing_job:
            return {
                "message": "Upload was already accepted.",
                **_job_response(existing_job),
            }

    stored_filename = f"{uuid4().hex}{Path(original_filename).suffix.lower()}"
    storage_key = storage_key_for(organization_id, stored_filename)
    saved_path = write_storage_bytes(storage_key, content)
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if collection_id is not None:
                collection = connection.execute(
                    """SELECT id FROM document_collections
                       WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                    (collection_id, organization_id, owner_id),
                ).fetchone()
                if collection is None:
                    raise HTTPException(status_code=404, detail="Collection was not found.")

            target = None
            if document_id is not None:
                target = require_document(
                    connection, document_id, current_user, manage=True
                )
            else:
                # A same-name upload by the same owner is the next version of that
                # logical document. Cross-owner matches never implicitly mutate it.
                target = connection.execute(
                    """SELECT * FROM documents
                       WHERE organization_id = ? AND owner_id = ?
                         AND deleted_at IS NULL
                         AND (LOWER(display_filename) = LOWER(?)
                              OR LOWER(original_filename) = LOWER(?))
                       ORDER BY updated_at DESC, id DESC LIMIT 1""",
                    (organization_id, owner_id, original_filename, original_filename),
                ).fetchone()

            if target is not None:
                document_id = int(target["id"])
                identical = connection.execute(
                    """SELECT id FROM document_versions
                       WHERE document_id = ? AND organization_id = ?
                         AND file_hash = ? AND deleted_at IS NULL
                         AND status <> 'cancelled'
                       ORDER BY version_number DESC LIMIT 1""",
                    (document_id, organization_id, file_hash),
                ).fetchone()
                if identical and not explicit_version:
                    saved_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "DOCUMENT_ALREADY_EXISTS",
                            "message": "An identical active document already exists.",
                            "retryable": False,
                        },
                    )
                next_version = int(connection.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 FROM document_versions WHERE document_id = ? AND organization_id = ?",
                    (document_id, organization_id),
                ).fetchone()[0])
            else:
                next_version = 1

            placeholder_hash = f"queued:{uuid4()}"
            content_cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash, normalized_content_hash,
                    extracted_text, processing_status)
                   VALUES (?, ?, ?, ?, '', 'pending')""",
                (owner_id, organization_id, file_hash, placeholder_hash),
            )
            content_id = int(content_cursor.lastrowid)
            if target is None:
                display_filename = generate_unique_display_filename(
                    connection, owner_id, original_filename
                )
                document_cursor = connection.execute(
                    """INSERT INTO documents
                       (owner_id, organization_id, original_filename, display_filename,
                        stored_filename, file_hash, content_id, visibility,
                        collection_id, upload_batch_id, relative_path,
                        processing_status, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued',
                               CURRENT_TIMESTAMP)""",
                    (
                        owner_id, organization_id, original_filename, display_filename,
                        stored_filename, file_hash, content_id, visibility.value,
                        collection_id, upload_batch_id, safe_relative_path,
                    ),
                )
                document_id = int(document_cursor.lastrowid)
            else:
                # A new version is pending; the prior current version stays live.
                pass
            version_cursor = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, mime_type, file_size, file_hash,
                    status, ingestion_status, extraction_status, indexing_status,
                    created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued',
                           'queued', 'queued', ?)""",
                (
                    organization_id, document_id, next_version, content_id,
                    stored_filename, storage_key, mime_type, len(content),
                    file_hash, owner_id,
                ),
            )
            version_id = int(version_cursor.lastrowid)
            job_id = enqueue_job(
                organization_id=organization_id,
                owner_id=owner_id,
                document_id=int(document_id),
                version_id=version_id,
                storage_key=storage_key,
                idempotency_key=effective_key,
                allow_active_content_reuse=explicit_version,
                connection=connection,
            )
            accepted_job = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                accepted_job is not None
                and accepted_job["version_id"] is not None
                and int(accepted_job["version_id"]) != version_id
            ):
                raise _ExistingUploadRequest(dict(accepted_job))
    except _ExistingUploadRequest as duplicate:
        saved_path.unlink(missing_ok=True)
        return {
            "message": "Upload was already accepted.",
            **_job_response(duplicate.job),
        }
    except Exception:
        saved_path.unlink(missing_ok=True)
        raise
    log_audit_event(
        event_type="document.upload.queued",
        endpoint="api/documents/upload",
        outcome="queued",
        user_id=owner_id,
        organization_id=organization_id,
        client_ip=client_ip,
        job_id=job_id,
        metadata={"document_id": document_id, "version_id": version_id},
    )
    log_event(
        "ingestion.job.queued",
        request_id=getattr(request.state, "request_id", None),
        job_id=job_id,
        organization_id=organization_id,
        user_id=owner_id,
        document_id=document_id,
        version_id=version_id,
    )
    return {
        "message": "Document upload accepted for processing.",
        "status": "queued",
        "job_id": job_id,
        "document_id": document_id,
        "version_id": version_id,
    }


@router.post("/documents/upload-zip", status_code=status.HTTP_202_ACCEPTED)
async def queue_zip_upload(
    request: Request,
    archive: UploadFile = File(...),
    collection_id: int | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    owner_id = int(current_user["id"])
    organization_id = str(current_user["organization_id"])
    client_ip = request.client.host if request.client else "unknown"
    enforce_request_limit(owner_id, client_ip, "upload", settings.uploads_per_hour)
    try:
        archive_name = sanitize_filename(archive.filename or "", {".zip"})
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Only ZIP archives are supported.") from error
    content = await archive.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded archive is empty.")
    if len(content) > settings.max_zip_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Archive exceeds maximum allowed size.")
    validate_file_signature(archive_name, content)
    if collection_id is not None:
        with get_connection() as connection:
            collection = connection.execute(
                """SELECT id FROM document_collections
                   WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                (collection_id, organization_id, owner_id),
            ).fetchone()
        if collection is None:
            raise HTTPException(status_code=404, detail="Collection was not found.")
    archive_hash = sha256(content).hexdigest()
    effective_key = (idempotency_key or "").strip() or sha256(
        f"{organization_id}:{owner_id}:archive:{archive_hash}".encode()
    ).hexdigest()
    stored_filename = f"{uuid4().hex}.zip"
    storage_key = storage_key_for(organization_id, stored_filename)
    path = write_storage_bytes(storage_key, content)
    try:
        job_id = enqueue_archive_job(
            organization_id=organization_id,
            owner_id=owner_id,
            storage_key=storage_key,
            archive_filename=archive_name,
            collection_id=collection_id,
            idempotency_key=effective_key,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {
        "message": "Archive accepted for secure extraction.",
        "status": "queued",
        "job_id": job_id,
        "document_id": None,
        "version_id": None,
    }


@public_router.post(
    "/documents/{document_id}/versions",
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/documents/{document_id}/versions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_document_version(
    document_id: int,
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    return await queue_document_upload(
        request=request,
        file=file,
        visibility=Visibility.PRIVATE,
        document_id=document_id,
        collection_id=None,
        upload_batch_id=None,
        relative_path=None,
        idempotency_key=idempotency_key,
        current_user=current_user,
    )


@public_router.get("/ingestion-jobs/{job_id}")
@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        row = connection.execute(
            """SELECT * FROM ingestion_jobs
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (job_id, current_user["organization_id"], current_user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion job was not found.")
    return _job_response(row)


@public_router.post(
    "/ingestion-jobs/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_job(
    job_id: str,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        row = connection.execute(
            """SELECT * FROM ingestion_jobs
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (job_id, current_user["organization_id"], current_user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Ingestion job was not found.")
        if row["status"] != "failed":
            raise HTTPException(status_code=409, detail="Only failed jobs can be retried.")
        if (row["last_error_code"] or row["error_code"]) in {
            "DOCUMENT_ALREADY_EXISTS",
            "DOCUMENT_REUPLOAD_FAILED",
        }:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": row["last_error_code"] or row["error_code"],
                    "message": row["last_error_message"] or row["error_message"],
                    "retryable": False,
                },
            )
        connection.execute(
            """UPDATE ingestion_jobs SET status = 'queued', attempt_count = 0,
               available_at = CURRENT_TIMESTAMP, completed_at = NULL,
               next_retry_at = NULL, error_code = NULL, last_error_code = NULL,
               error_message = NULL, last_error_message = NULL,
               updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (job_id,),
        )
        connection.execute(
            """UPDATE document_versions
               SET status = 'queued', ingestion_status = 'queued',
                   extraction_status = 'queued', indexing_status = 'queued',
                   failure_reason = NULL
               WHERE id = ?""",
            (row["version_id"],),
        )
    return {"job_id": job_id, "status": "queued"}


@public_router.post("/ingestion-jobs/{job_id}/cancel")
@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    current_user: dict[str, object] = Depends(get_current_user),
) -> dict[str, object]:
    with get_connection() as connection:
        row = connection.execute(
            """SELECT * FROM ingestion_jobs
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (job_id, current_user["organization_id"], current_user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Ingestion job was not found.")
        cursor = connection.execute(
            """UPDATE ingestion_jobs SET status = 'cancelled',
               completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status IN ('queued','retry_scheduled')""",
            (job_id,),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="This job can no longer be cancelled.")
        connection.execute(
            """UPDATE document_versions
               SET status = 'cancelled', ingestion_status = 'cancelled',
                   extraction_status = CASE
                       WHEN extraction_status = 'completed' THEN extraction_status
                       ELSE 'cancelled'
                   END,
                   indexing_status = CASE
                       WHEN indexing_status = 'completed' THEN indexing_status
                       ELSE 'cancelled'
                   END
               WHERE id = ?""",
            (row["version_id"],),
        )
    return {"job_id": job_id, "status": "cancelled"}
