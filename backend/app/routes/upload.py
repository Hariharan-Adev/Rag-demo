"""Secure, owner-scoped document upload with shared processed content."""

from __future__ import annotations

from hashlib import sha256
from json import dumps
from pathlib import Path
from sqlite3 import IntegrityError
from time import monotonic, sleep
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.config import settings
from app.database import UPLOAD_DIRECTORY, get_connection
from app.services.chunking import chunk_text
from app.services.document_loader import DocumentParseError, SUPPORTED_EXTENSIONS, extract_text
from app.services.embeddings import create_embeddings
from app.utils.audit import log_audit_event
from app.utils.document_content import (
    generate_unique_display_filename,
    normalize_extracted_text,
    sanitize_filename,
)
from app.utils.rate_limit import enforce_request_limit
from app.utils.security import SecurityValidationError, validate_chunks, validate_extracted_text

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_EXTENSIONS = set(SUPPORTED_EXTENSIONS)
MAX_FILE_SIZE = 10 * 1024 * 1024
CONTENT_WAIT_SECONDS = 30.0


def _chunk_count(content_id: int) -> int:
    with get_connection() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE content_id = ?", (content_id,)
        ).fetchone()[0])


def _same_filename_duplicate(owner_id: int, filename: str, hash_column: str, hash_value: str):
    if hash_column not in {"file_hash", "normalized_content_hash"}:
        raise ValueError("Unsupported duplicate hash column.")
    with get_connection() as connection:
        return connection.execute(
            f"""
            SELECT d.id, d.display_filename, d.content_id
            FROM documents d
            JOIN document_contents dc ON dc.id = d.content_id
            WHERE d.owner_id = ? AND d.original_filename = ?
              AND {'d.file_hash' if hash_column == 'file_hash' else 'dc.normalized_content_hash'} = ?
            ORDER BY d.id
            LIMIT 1
            """,
            (owner_id, filename, hash_value),
        ).fetchone()


def _completed_content_for_file(owner_id: int, file_hash: str):
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT dc.id, COUNT(c.id) AS chunk_count
            FROM documents d
            JOIN document_contents dc ON dc.id = d.content_id
            LEFT JOIN chunks c ON c.content_id = dc.id
            WHERE d.owner_id = ? AND d.file_hash = ? AND dc.processing_status = 'completed'
            GROUP BY dc.id
            ORDER BY dc.id
            LIMIT 1
            """,
            (owner_id, file_hash),
        ).fetchone()


def _insert_document(
    *, owner_id: int, original_filename: str, stored_filename: str,
    file_hash: str, content_id: int, duplicate: bool,
) -> tuple[int, str]:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        display_filename = generate_unique_display_filename(connection, owner_id, original_filename)
        cursor = connection.execute(
            """
            INSERT INTO documents
                (owner_id, original_filename, display_filename, stored_filename,
                 file_hash, content_id, is_duplicate_content)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (owner_id, original_filename, display_filename, stored_filename,
             file_hash, content_id, int(duplicate)),
        )
        return int(cursor.lastrowid), display_filename


def _claim_content(owner_id: int, file_hash: str, content_hash: str, text: str) -> tuple[int, bool, str]:
    """Atomically claim normalized content; uniqueness protects concurrent uploads."""
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT id, processing_status FROM document_contents
            WHERE owner_id = ? AND normalized_content_hash = ?
            """,
            (owner_id, content_hash),
        ).fetchone()
        if existing is not None:
            if existing["processing_status"] == "failed":
                connection.execute(
                    """
                    UPDATE document_contents
                    SET file_hash = ?, extracted_text = ?, processing_status = 'processing'
                    WHERE id = ? AND processing_status = 'failed'
                    """,
                    (file_hash, text, existing["id"]),
                )
                return int(existing["id"]), True, "processing"
            return int(existing["id"]), False, str(existing["processing_status"])
        try:
            cursor = connection.execute(
                """
                INSERT INTO document_contents
                    (owner_id, file_hash, normalized_content_hash, extracted_text, processing_status)
                VALUES (?, ?, ?, ?, 'processing')
                """,
                (owner_id, file_hash, content_hash, text),
            )
            return int(cursor.lastrowid), True, "processing"
        except IntegrityError:
            existing = connection.execute(
                """
                SELECT id, processing_status FROM document_contents
                WHERE owner_id = ? AND normalized_content_hash = ?
                """,
                (owner_id, content_hash),
            ).fetchone()
            if existing is None:
                raise
            return int(existing["id"]), False, str(existing["processing_status"])


def _wait_for_content(content_id: int) -> str:
    deadline = monotonic() + CONTENT_WAIT_SECONDS
    while monotonic() < deadline:
        with get_connection() as connection:
            row = connection.execute(
                "SELECT processing_status FROM document_contents WHERE id = ?", (content_id,)
            ).fetchone()
        if row is None:
            return "missing"
        status = str(row["processing_status"])
        if status != "processing":
            return status
        sleep(0.05)
    return "processing"


def _conflict(existing) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "Document already exists.",
            "duplicate_type": "same_filename_same_content",
            "existing_document_id": int(existing["id"]),
            "display_filename": str(existing["display_filename"]),
        },
    )


@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict[str, object] = Depends(get_current_user),
):
    """Store one upload while reusing identical processed content for the same owner."""
    owner_id = int(current_user["id"])
    client_ip = request.client.host if request.client else "unknown"
    enforce_request_limit(owner_id, client_ip, "upload", settings.uploads_per_hour)

    try:
        original_filename = sanitize_filename(file.filename or "", ALLOWED_EXTENSIONS)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Maximum file size is 10 MB.")

    file_hash = sha256(content).hexdigest()
    existing = _same_filename_duplicate(owner_id, original_filename, "file_hash", file_hash)
    if existing is not None:
        log_audit_event(event_type="document.upload", endpoint="documents/upload", outcome="duplicate",
                        user_id=owner_id, client_ip=client_ip,
                        metadata={"duplicate_type": "same_filename_same_content", "document_id": existing["id"]})
        return _conflict(existing)

    UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    extension = Path(original_filename).suffix.lower()
    stored_name = f"{uuid4().hex}{extension}"
    saved_path = UPLOAD_DIRECTORY / stored_name
    saved_path.write_bytes(content)

    exact_content = _completed_content_for_file(owner_id, file_hash)
    if exact_content is not None:
        try:
            document_id, display_filename = _insert_document(
                owner_id=owner_id, original_filename=original_filename,
                stored_filename=stored_name, file_hash=file_hash,
                content_id=int(exact_content["id"]), duplicate=True,
            )
        except Exception:
            saved_path.unlink(missing_ok=True)
            raise
        chunk_count = int(exact_content["chunk_count"])
        return {
            "message": "Document accepted; existing processed content was reused.",
            "status": "accepted", "document_id": document_id, "filename": display_filename,
            "display_filename": display_filename, "content_reused": True,
            "existing_content_id": int(exact_content["id"]), "chunk_count": chunk_count,
        }

    try:
        extracted_text = extract_text(saved_path)
        validate_extracted_text(extracted_text)
        normalized_text = normalize_extracted_text(extracted_text)
        validate_extracted_text(normalized_text)
    except (SecurityValidationError, DocumentParseError) as error:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The document text could not be extracted.") from error

    content_hash = sha256(normalized_text.encode("utf-8")).hexdigest()
    existing = _same_filename_duplicate(owner_id, original_filename, "normalized_content_hash", content_hash)
    if existing is not None:
        saved_path.unlink(missing_ok=True)
        return _conflict(existing)

    content_id, should_process, status = _claim_content(
        owner_id, file_hash, content_hash, normalized_text
    )
    if not should_process and status == "processing":
        status = _wait_for_content(content_id)
    if not should_process and status != "completed":
        saved_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail="Matching document content is still being processed. Please retry.")

    if should_process:
        try:
            chunks = chunk_text(normalized_text)
            validate_chunks(chunks)
            embeddings = create_embeddings(chunks)
            if len(embeddings) != len(chunks):
                raise RuntimeError("Embedding count did not match the chunk count.")
            with get_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))
                connection.executemany(
                    "INSERT INTO chunks (content_id, chunk_index, text, embedding) VALUES (?, ?, ?, ?)",
                    [(content_id, index, chunk, dumps(embedding))
                     for index, (chunk, embedding) in enumerate(zip(chunks, embeddings))],
                )
                connection.execute(
                    "UPDATE document_contents SET processing_status = 'completed' WHERE id = ?",
                    (content_id,),
                )
        except Exception as error:
            with get_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))
                connection.execute(
                    "UPDATE document_contents SET processing_status = 'failed' WHERE id = ?",
                    (content_id,),
                )
            saved_path.unlink(missing_ok=True)
            if isinstance(error, SecurityValidationError):
                raise HTTPException(status_code=400, detail=str(error)) from error
            raise HTTPException(status_code=400, detail="The document could not be processed.") from error

    # Re-check after waiting so concurrent same-name uploads still resolve to one 409.
    existing = _same_filename_duplicate(owner_id, original_filename, "normalized_content_hash", content_hash)
    if existing is not None:
        saved_path.unlink(missing_ok=True)
        return _conflict(existing)

    try:
        document_id, display_filename = _insert_document(
            owner_id=owner_id, original_filename=original_filename,
            stored_filename=stored_name, file_hash=file_hash, content_id=content_id,
            duplicate=not should_process,
        )
    except Exception:
        saved_path.unlink(missing_ok=True)
        raise

    chunk_count = _chunk_count(content_id)
    reused = not should_process
    log_audit_event(event_type="document.upload", endpoint="documents/upload", outcome="accepted",
                    user_id=owner_id, client_ip=client_ip,
                    metadata={"document_id": document_id, "content_id": content_id,
                              "content_reused": reused, "chunk_count": chunk_count})
    response = {
        "message": "Document accepted; existing processed content was reused." if reused else "Document processed successfully.",
        "status": "accepted" if reused else "processed",
        "document_id": document_id, "filename": display_filename,
        "display_filename": display_filename, "content_reused": reused,
        "chunk_count": chunk_count,
    }
    if reused:
        response["existing_content_id"] = content_id
    return response
