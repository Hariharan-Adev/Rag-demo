"""Secure, owner-scoped document upload with shared processed content."""

from __future__ import annotations

from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from sqlite3 import IntegrityError
from time import monotonic, perf_counter, sleep
from uuid import uuid4
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import get_current_user
from app.config import settings
from app.database import UPLOAD_DIRECTORY, get_connection
from app.services.chunking import chunk_text
from app.services.document_loader import DocumentParseError, SUPPORTED_EXTENSIONS, extract_text
from app.services.embeddings import create_embeddings
from app.services.folder_uploads import record_batch_result, sanitize_relative_path, validate_upload_context
from app.services.workbooks import WorkbookData, extract_workbook, workbook_chunks, workbook_text
from app.services.zip_archives import ArchiveValidationError, extract_member, inspect_archive, temporary_archive_directory
from app.utils.audit import log_audit_event
from app.utils.document_content import (
    generate_unique_display_filename,
    normalize_extracted_text,
    sanitize_filename,
)
from app.utils.file_validation import validate_file_signature
from app.utils.rate_limit import enforce_request_limit
from app.utils.security import SecurityValidationError, validate_chunks, validate_extracted_text

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_EXTENSIONS = set(SUPPORTED_EXTENSIONS)
CONTENT_WAIT_SECONDS = 30.0


def _chunk_count(content_id: int) -> int:
    with get_connection() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE content_id = ?", (content_id,)
        ).fetchone()[0])


def _workbook_processing_metadata(content_id: int) -> dict[str, object] | None:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT name, status, processing_error
            FROM workbook_sheets
            WHERE content_id = ?
            ORDER BY sheet_index
            """,
            (content_id,),
        ).fetchall()
    if not rows:
        return None
    return {
        "processed_sheets": [str(row["name"]) for row in rows if row["status"] == "processed"],
        "empty_sheets": [str(row["name"]) for row in rows if row["status"] == "empty"],
        "disabled_sheets": [str(row["name"]) for row in rows if row["status"] == "disabled"],
        "failed_sheets": [
            {"sheet": str(row["name"]), "reason": str(row["processing_error"])}
            for row in rows
            if row["status"] == "failed"
        ],
    }


def _replace_workbook_data(
    connection,
    content_id: int,
    owner_id: int,
    workbook: WorkbookData,
) -> None:
    connection.execute("DELETE FROM workbook_sheets WHERE content_id = ?", (content_id,))
    for sheet_index, sheet in enumerate(workbook.sheets):
        cursor = connection.execute(
            """
            INSERT INTO workbook_sheets
                (content_id, owner_id, sheet_index, name, visibility, status,
                 header_row, headers_json, processing_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                owner_id,
                sheet_index,
                sheet.name,
                sheet.state,
                sheet.status,
                sheet.header_row,
                dumps(sheet.headers, ensure_ascii=False),
                sheet.error,
            ),
        )
        sheet_id = int(cursor.lastrowid)
        connection.executemany(
            """
            INSERT INTO workbook_rows
                (sheet_id, content_id, owner_id, row_number, values_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    sheet_id,
                    content_id,
                    owner_id,
                    row.row_number,
                    dumps(row.values, ensure_ascii=False),
                )
                for row in sheet.rows
            ],
        )


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
    file_hash: str, content_id: int, duplicate: bool, collection_id: int | None = None,
    upload_batch_id: int | None = None, relative_path: str | None = None,
) -> tuple[int, str]:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        display_filename = generate_unique_display_filename(connection, owner_id, original_filename)
        cursor = connection.execute(
            """
            INSERT INTO documents
                (owner_id, original_filename, display_filename, stored_filename,
                file_hash, content_id, is_duplicate_content, collection_id,
                upload_batch_id, relative_path, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
            """,
            (owner_id, original_filename, display_filename, stored_filename,
             file_hash, content_id, int(duplicate), collection_id, upload_batch_id,
             relative_path),
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


async def _process_document_upload(
    request: Request,
    file: UploadFile,
    current_user: dict[str, object],
    collection_id: int | None = None,
    upload_batch_id: int | None = None,
    relative_path: str | None = None,
    enforce_upload_limit: bool = True,
):
    """Store one upload while reusing identical processed content for the same owner."""
    owner_id = int(current_user["id"])
    client_ip = request.client.host if request.client else "unknown"
    if enforce_upload_limit and upload_batch_id is None:
        enforce_request_limit(owner_id, client_ip, "upload", settings.uploads_per_hour)

    try:
        original_filename = sanitize_filename(file.filename or "", ALLOWED_EXTENSIONS)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Maximum file size is {settings.max_file_size_mb} MB.")
    validate_file_signature(original_filename, content)

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
                collection_id=collection_id, upload_batch_id=upload_batch_id,
                relative_path=relative_path,
            )
        except Exception:
            saved_path.unlink(missing_ok=True)
            raise
        chunk_count = int(exact_content["chunk_count"])
        response = {
            "message": "Document accepted; existing processed content was reused.",
            "status": "accepted", "document_id": document_id, "filename": display_filename,
            "display_filename": display_filename, "content_reused": True,
            "existing_content_id": int(exact_content["id"]), "chunk_count": chunk_count,
        }
        workbook_metadata = _workbook_processing_metadata(int(exact_content["id"]))
        if workbook_metadata is not None:
            response["workbook"] = workbook_metadata
        return response

    workbook_data: WorkbookData | None = None
    try:
        if extension in {".xlsx", ".xls"}:
            workbook_data = extract_workbook(
                saved_path,
                include_hidden=settings.include_hidden_worksheets,
            )
            extracted_text = workbook_text(workbook_data, original_filename)
        else:
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
            if workbook_data is None:
                chunk_records = [(chunk, None, None) for chunk in chunk_text(normalized_text)]
            else:
                chunk_records = workbook_chunks(workbook_data, original_filename)
            chunk_values = [record[0] for record in chunk_records]
            validate_chunks(chunk_values)
            embeddings = create_embeddings(chunk_values)
            if len(embeddings) != len(chunk_records):
                raise RuntimeError("Embedding count did not match the chunk count.")
            with get_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))
                connection.executemany(
                    """
                    INSERT INTO chunks
                        (content_id, chunk_index, text, embedding, sheet_name, row_number)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            content_id,
                            index,
                            record[0],
                            dumps(embedding),
                            record[1],
                            record[2],
                        )
                        for index, (record, embedding) in enumerate(zip(chunk_records, embeddings))
                    ],
                )
                if workbook_data is not None:
                    _replace_workbook_data(connection, content_id, owner_id, workbook_data)
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
            duplicate=not should_process, collection_id=collection_id,
            upload_batch_id=upload_batch_id, relative_path=relative_path,
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
    workbook_metadata = _workbook_processing_metadata(content_id)
    if workbook_metadata is not None:
        response["workbook"] = workbook_metadata
    return response


@router.get("/upload-config")
def upload_config(current_user: dict[str, object] = Depends(get_current_user)) -> dict[str, object]:
    """Expose non-secret upload constraints so folder previews match backend validation."""
    return {
        "supported_extensions": sorted(ALLOWED_EXTENSIONS),
        "archive_extensions": [".zip"],
        "max_file_size_mb": settings.max_file_size_mb,
        "max_zip_upload_mb": settings.max_zip_upload_mb,
        "max_folder_files": settings.max_folder_files,
        "max_folder_total_size_mb": settings.max_folder_total_size_mb,
        "max_concurrent_uploads": settings.max_concurrent_file_processing,
    }


@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict[str, object] = Depends(get_current_user),
    collection_id: int | None = Form(default=None),
    upload_batch_id: int | None = Form(default=None),
    relative_path: str | None = Form(default=None),
):
    """Run a single file through the existing pipeline with optional folder metadata."""
    owner_id = int(current_user["id"])
    safe_filename = sanitize_filename(file.filename or "", ALLOWED_EXTENSIONS)
    relative_path_value = relative_path if isinstance(relative_path, str) else None
    collection_id_value = collection_id if isinstance(collection_id, int) and not isinstance(collection_id, bool) else None
    batch_id_value = upload_batch_id if isinstance(upload_batch_id, int) and not isinstance(upload_batch_id, bool) else None
    safe_relative_path = sanitize_relative_path(relative_path_value, safe_filename)
    validate_upload_context(owner_id, collection_id_value, batch_id_value)
    try:
        result = await _process_document_upload(
            request, file, current_user, collection_id_value, batch_id_value, safe_relative_path
        )
        if isinstance(result, JSONResponse):
            record_batch_result(batch_id_value, owner_id, "duplicate")
            log_audit_event(event_type="folder.file_duplicate", endpoint="documents/upload", outcome="duplicate", user_id=owner_id, client_ip=request.client.host if request.client else "", metadata={"batch_id": batch_id_value})
            return result
        reused = bool(result.get("content_reused"))
        result.update({
            "relative_path": safe_relative_path,
            "duplicate_type": "same_content_different_filename" if reused else None,
        })
        record_batch_result(batch_id_value, owner_id, "duplicate" if reused else "successful")
        if batch_id_value is not None:
            log_audit_event(event_type="folder.file_duplicate" if reused else "folder.file_uploaded", endpoint="documents/upload", outcome="duplicate" if reused else "success", user_id=owner_id, client_ip=request.client.host if request.client else "", metadata={"batch_id": batch_id_value, "document_id": result["document_id"], "content_reused": reused})
        return result
    except HTTPException:
        record_batch_result(batch_id_value, owner_id, "failed")
        if batch_id_value is not None:
            log_audit_event(event_type="folder.file_failed", endpoint="documents/upload", outcome="failed", user_id=owner_id, client_ip=request.client.host if request.client else "", metadata={"batch_id": batch_id_value})
        raise


@router.post("/upload-zip")
async def upload_zip_archive(
    request: Request,
    archive: UploadFile = File(...),
    current_user: dict[str, object] = Depends(get_current_user),
    collection_id: int | None = Form(default=None),
):
    """Securely validate a ZIP, then process approved members through normal upload logic."""
    owner_id = int(current_user["id"])
    client_ip = request.client.host if request.client else "unknown"
    collection_id_value = collection_id if isinstance(collection_id, int) and not isinstance(collection_id, bool) else None
    validate_upload_context(owner_id, collection_id_value, None)
    enforce_request_limit(owner_id, client_ip, "upload", settings.uploads_per_hour)
    try:
        archive_name = sanitize_filename(archive.filename or "", {".zip"})
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Only ZIP archives are supported.") from error

    started = perf_counter()
    archive_digest = sha256()
    archive_size = 0
    plan = None
    results: list[dict[str, object]] = []

    try:
        with temporary_archive_directory() as temporary:
            workspace = Path(temporary)
            archive_path = workspace / f"{uuid4().hex}.zip"
            with archive_path.open("xb") as output:
                while chunk := await archive.read(1024 * 1024):
                    archive_size += len(chunk)
                    if archive_size > settings.max_zip_upload_mb * 1024 * 1024:
                        raise ArchiveValidationError("Archive exceeds maximum allowed size.")
                    archive_digest.update(chunk)
                    output.write(chunk)
            if archive_size == 0:
                raise ArchiveValidationError("The uploaded archive is empty.")

            plan = await run_in_threadpool(inspect_archive, archive_path)
            extraction_directory = workspace / "extracted"
            extraction_directory.mkdir()
            with zipfile.ZipFile(archive_path) as zip_file:
                for member in plan.members:
                    if member.rejection_reason is not None:
                        results.append({
                            "filename": member.filename,
                            "status": "rejected",
                            "document_id": None,
                            "reason": member.rejection_reason,
                        })
                        continue
                    extracted_path: Path | None = None
                    try:
                        extracted_path = await run_in_threadpool(extract_member, zip_file, member, extraction_directory)
                        with extracted_path.open("rb") as handle:
                            extracted_upload = UploadFile(file=handle, filename=member.filename)
                            result = await _process_document_upload(
                                request,
                                extracted_upload,
                                current_user,
                                collection_id=collection_id_value,
                                enforce_upload_limit=False,
                            )
                        if isinstance(result, JSONResponse):
                            duplicate = loads(result.body)
                            results.append({
                                "filename": member.filename,
                                "status": "duplicate",
                                "document_id": duplicate.get("existing_document_id"),
                                "reason": duplicate.get("detail", "Document already exists."),
                            })
                        else:
                            reused = bool(result.get("content_reused"))
                            results.append({
                                "filename": member.filename,
                                "status": "duplicate_content_reused" if reused else "uploaded",
                                "document_id": result.get("document_id"),
                                "display_filename": result.get("display_filename"),
                                "message": result.get("message"),
                            })
                    except HTTPException as error:
                        results.append({
                            "filename": member.filename,
                            "status": "rejected" if error.status_code == 400 else "failed",
                            "document_id": None,
                            "reason": str(error.detail),
                        })
                    except (ArchiveValidationError, zipfile.BadZipFile, RuntimeError, OSError):
                        results.append({
                            "filename": member.filename,
                            "status": "failed",
                            "document_id": None,
                            "reason": "The archived document could not be processed.",
                        })
                    finally:
                        if extracted_path is not None:
                            extracted_path.unlink(missing_ok=True)
    except ArchiveValidationError as error:
        log_audit_event(
            event_type="archive.upload",
            endpoint="documents/upload-zip",
            outcome="security_rejected",
            user_id=owner_id,
            client_ip=client_ip,
            metadata={
                "archive_filename": archive_name,
                "archive_hash": archive_digest.hexdigest(),
                "archive_size": archive_size,
                "security_validation_failure": str(error),
                "processing_duration_ms": round((perf_counter() - started) * 1000),
            },
        )
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (zipfile.BadZipFile, OSError) as error:
        raise HTTPException(status_code=400, detail="Archive failed security validation.") from error

    uploaded = sum(item["status"] == "uploaded" for item in results)
    duplicates = sum(item["status"] in {"duplicate", "duplicate_content_reused"} for item in results)
    failed = len(results) - uploaded - duplicates
    status = "completed" if failed == 0 else "partially_completed"
    rejection_reasons = sorted({str(item.get("reason")) for item in results if item.get("reason")})
    log_audit_event(
        event_type="archive.upload",
        endpoint="documents/upload-zip",
        outcome=status,
        user_id=owner_id,
        client_ip=client_ip,
        metadata={
            "archive_filename": archive_name,
            "archive_hash": archive_digest.hexdigest(),
            "total_entries": plan.total_entries if plan else 0,
            "uploaded_count": uploaded,
            "duplicate_count": duplicates,
            "rejected_count": failed,
            "rejection_reasons": rejection_reasons,
            "processing_duration_ms": round((perf_counter() - started) * 1000),
            "total_extracted_size": plan.total_extracted_size if plan else 0,
        },
    )
    return {
        "archive": archive_name,
        "status": status,
        "summary": {
            "total_entries": len(results),
            "uploaded": uploaded,
            "duplicates": duplicates,
            "failed": failed,
        },
        "files": results,
    }
