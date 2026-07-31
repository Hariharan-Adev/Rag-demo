"""Durable ingestion job creation, claiming, retry, and processing."""

from __future__ import annotations

import json
import logging
import mimetypes
import multiprocessing
import queue
import random
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.config import settings
from app.database import UPLOAD_DIRECTORY, get_connection
from app.services.embeddings import create_embeddings
from app.services.document_loader import DocumentParseError
from app.services.source_extraction import extract_source_chunks, extract_source_metadata
from app.services.vector_store import (
    VectorPoint,
    get_vector_store,
    make_vector_point_id,
)
from app.utils.audit import log_audit_event
from app.utils.document_content import normalize_extracted_text
from app.utils.document_content import generate_unique_display_filename
from app.utils.file_validation import validate_file_signature
from app.services.zip_archives import (
    ArchiveValidationError,
    extract_member,
    inspect_archive,
    temporary_archive_directory,
)
from app.services.folder_uploads import record_batch_result
from app.services.structured_ingestion import (
    StructuredDocumentContext,
    ingest_structured_csv,
    replace_workbook_content,
)
from app.services.workbooks import extract_workbook
from app.utils.security import validate_chunks, validate_extracted_text
from app.utils.observability import log_event
from app.services.storage import resolve_storage_key, storage_key_for, write_storage_bytes

logger = logging.getLogger(__name__)


class IngestionPolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ContentResolution:
    mode: str
    content_id: int | None = None
    reusable_chunks: tuple[sqlite3.Row, ...] = ()


def _extract_bundle_child(
    path_value: str,
    include_hidden: bool,
    include_very_hidden: bool,
    output,
) -> None:
    """Run untrusted parser libraries outside the long-lived worker process."""
    path = Path(path_value)
    failure_stage = "resume"
    try:
        source_chunks = extract_source_chunks(
            path,
            include_hidden=include_hidden,
            include_very_hidden=include_very_hidden,
        )
        source_metadata = extract_source_metadata(
            path,
            include_hidden=include_hidden,
            include_very_hidden=include_very_hidden,
        )
        workbook = (
            extract_workbook(
                path,
                include_hidden=include_hidden,
                include_very_hidden=include_very_hidden,
            )
            if path.suffix.lower() in {".xlsx", ".xls"}
            else None
        )
        output.put(("ok", source_chunks, source_metadata, workbook))
    except Exception as error:
        output.put(("error", type(error).__name__, str(error)))


def _extract_bundle(path: Path):
    """Extract with a hard process timeout in production."""
    hidden = settings.include_hidden_worksheets
    very_hidden = settings.include_very_hidden_worksheets
    if settings.app_environment != "production":
        source_chunks = extract_source_chunks(
            path,
            include_hidden=hidden,
            include_very_hidden=very_hidden,
        )
        source_metadata = extract_source_metadata(
            path,
            include_hidden=hidden,
            include_very_hidden=very_hidden,
        )
        workbook = (
            extract_workbook(
                path,
                include_hidden=hidden,
                include_very_hidden=very_hidden,
            )
            if path.suffix.lower() in {".xlsx", ".xls"}
            else None
        )
        return source_chunks, source_metadata, workbook

    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_extract_bundle_child,
        args=(str(path), hidden, very_hidden, output),
        daemon=True,
    )
    process.start()
    try:
        result = output.get(timeout=settings.parser_timeout_seconds)
    except queue.Empty as error:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        raise DocumentParseError(
            "Document parsing exceeded the configured time limit."
        ) from error
    finally:
        output.close()
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    if result[0] == "error":
        raise DocumentParseError(result[2] or "Document parsing failed.")
    return result[1], result[2], result[3]


def _pipeline_job_key(
    organization_id: str,
    version_id: int,
    job_type: str = "document_ingestion",
) -> str:
    return (
        f"{organization_id}:{version_id}:{job_type}:"
        f"{settings.ingestion_pipeline_version}"
    )


def _classify_error(error: Exception) -> tuple[str, str, bool]:
    """Return a stable public code/message and whether the failure is transient."""
    if isinstance(error, IngestionPolicyError):
        return error.code, str(error), False
    if isinstance(error, DocumentParseError):
        return "document_parse_failed", str(error), False
    if isinstance(error, ArchiveValidationError):
        return "archive_validation_failed", str(error), False
    if isinstance(error, FileNotFoundError):
        return "stored_file_unavailable", "Stored upload is unavailable.", False
    if isinstance(error, ValueError):
        return "validation_failed", str(error) or "Document validation failed.", False
    if isinstance(error, TimeoutError):
        return "provider_timeout", "A processing dependency timed out.", True
    if isinstance(error, ConnectionError):
        return "dependency_unavailable", "A processing dependency is unavailable.", True
    module = type(error).__module__.lower()
    if any(name in module for name in ("httpx", "qdrant", "openai", "groq")):
        raw_status = getattr(error, "status_code", None)
        try:
            status_code = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status_code = None
        transient = status_code is None or status_code == 429 or status_code >= 500
        return (
            "dependency_unavailable" if transient else "provider_rejected_request",
            (
                "A processing dependency is temporarily unavailable."
                if transient else "A processing dependency rejected the request."
            ),
            transient,
        )
    return "internal_processing_error", "Document processing failed.", False


def _matching_content_chunks(
    connection: sqlite3.Connection,
    *,
    content_id: int,
    owner_id: int,
    organization_id: str,
    source_chunks,
    deleted: bool,
) -> tuple[sqlite3.Row, ...]:
    lifecycle = "IS NOT NULL" if deleted else "IS NULL"
    versions = connection.execute(
        f"""SELECT dv.id
           FROM document_versions dv
           JOIN documents d ON d.id = dv.document_id
           WHERE dv.content_id = ? AND dv.organization_id = ?
             AND dv.status = 'completed' AND dv.deleted_at {lifecycle}
             AND d.owner_id = ? AND d.deleted_at {lifecycle}
           ORDER BY dv.completed_at DESC, dv.id DESC""",
        (content_id, organization_id, owner_id),
    ).fetchall()
    for version in versions:
        rows = connection.execute(
            f"""SELECT text, source_type, source_location_json,
                      vector_point_id, embedding_model, embedding_dimension
               FROM chunks
               WHERE content_id = ? AND version_id = ? AND organization_id = ?
                 AND deleted_at {lifecycle}
               ORDER BY chunk_index""",
            (content_id, version["id"], organization_id),
        ).fetchall()
        if len(rows) != len(source_chunks):
            continue
        compatible = True
        for row, source in zip(rows, source_chunks):
            if (
                row["text"] != source.text
                or str(row["source_type"] or "text") != source.source_type
            ):
                compatible = False
                break
        if compatible:
            return tuple(rows)
    return ()


def _embeddings_are_compatible(rows: tuple[sqlite3.Row, ...]) -> bool:
    return bool(rows) and all(
        row["embedding_model"] == settings.embedding_model_version
        and int(row["embedding_dimension"] or 0) == settings.embedding_dimension
        and bool(row["vector_point_id"])
        for row in rows
    )


def _resolve_content(
    connection: sqlite3.Connection,
    *,
    job,
    placeholder_content_id: int,
    normalized_hash: str,
    source_chunks,
    allow_active_reuse: bool = False,
) -> ContentResolution:
    """Resolve one owner's active/deleted content before embedding generation."""
    active = connection.execute(
        """SELECT dc.id
           FROM document_contents dc
           WHERE dc.organization_id = ? AND dc.owner_id = ?
             AND dc.normalized_content_hash = ? AND dc.deleted_at IS NULL
             AND dc.processing_status = 'completed' AND dc.id <> ?
             AND EXISTS (
               SELECT 1
               FROM document_versions dv
               JOIN documents d ON d.id = dv.document_id
               WHERE dv.content_id = dc.id AND dv.organization_id = ?
                 AND dv.status = 'completed' AND dv.deleted_at IS NULL
                 AND d.owner_id = ? AND d.deleted_at IS NULL
             )
           ORDER BY dc.id LIMIT 1""",
        (
            job["organization_id"], job["owner_id"], normalized_hash,
            placeholder_content_id, job["organization_id"], job["owner_id"],
        ),
    ).fetchone()
    if active:
        if allow_active_reuse:
            reusable = _matching_content_chunks(
                connection,
                content_id=int(active["id"]),
                owner_id=int(job["owner_id"]),
                organization_id=str(job["organization_id"]),
                source_chunks=source_chunks,
                deleted=False,
            )
            if _embeddings_are_compatible(reusable):
                return ContentResolution(
                    "active_reuse", int(active["id"]), reusable
                )
        return ContentResolution("active_duplicate", int(active["id"]))

    deleted = connection.execute(
        """SELECT dc.id
           FROM document_contents dc
           WHERE dc.organization_id = ? AND dc.owner_id = ?
             AND dc.normalized_content_hash = ? AND dc.deleted_at IS NOT NULL
             AND dc.processing_status = 'completed'
             AND EXISTS (
               SELECT 1
               FROM document_versions dv
               JOIN documents d ON d.id = dv.document_id
               WHERE dv.content_id = dc.id AND dv.organization_id = ?
                 AND dv.status = 'completed' AND dv.deleted_at IS NOT NULL
                 AND d.owner_id = ? AND d.deleted_at IS NOT NULL
             )
           ORDER BY dc.id LIMIT 1""",
        (
            job["organization_id"], job["owner_id"], normalized_hash,
            job["organization_id"], job["owner_id"],
        ),
    ).fetchone()
    if deleted:
        reusable = _matching_content_chunks(
            connection,
            content_id=int(deleted["id"]),
            owner_id=int(job["owner_id"]),
            organization_id=str(job["organization_id"]),
            source_chunks=source_chunks,
            deleted=True,
        )
        if _embeddings_are_compatible(reusable):
            return ContentResolution("deleted_reuse", int(deleted["id"]), reusable)
        if reusable:
            return ContentResolution("deleted_rebuild", int(deleted["id"]))
    return ContentResolution("new")


def enqueue_job(
    *,
    organization_id: str,
    owner_id: int,
    document_id: int,
    version_id: int,
    storage_key: str,
    idempotency_key: str,
    allow_active_content_reuse: bool = False,
    connection: sqlite3.Connection | None = None,
) -> str:
    job_id = str(uuid4())
    pipeline_key = _pipeline_job_key(organization_id, version_id)
    def insert(target: sqlite3.Connection) -> str:
        existing = target.execute(
            """SELECT id FROM ingestion_jobs
               WHERE organization_id = ?
                 AND (idempotency_key = ? OR request_idempotency_key = ?)""",
            (organization_id, pipeline_key, idempotency_key),
        ).fetchone()
        if existing:
            return str(existing["id"])
        cursor = target.execute(
            """INSERT INTO ingestion_jobs
               (id, organization_id, owner_id, document_id, version_id,
                idempotency_key, request_idempotency_key, pipeline_version,
                payload_json, max_attempts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO NOTHING""",
            (
                job_id, organization_id, owner_id, document_id, version_id,
                pipeline_key, idempotency_key, settings.ingestion_pipeline_version,
                json.dumps({
                    "storage_key": storage_key,
                    "pipeline_version": settings.ingestion_pipeline_version,
                    "embedding_model": settings.embedding_model_version,
                    "allow_active_content_reuse": allow_active_content_reuse,
                }),
                settings.ingestion_max_attempts,
            ),
        )
        if cursor.rowcount != 1:
            existing = target.execute(
                """SELECT id FROM ingestion_jobs
                   WHERE organization_id = ?
                     AND (idempotency_key = ? OR request_idempotency_key = ?)""",
                (organization_id, pipeline_key, idempotency_key),
            ).fetchone()
            if existing:
                return str(existing["id"])
            raise sqlite3.IntegrityError("Ingestion job idempotency conflict.")
        return job_id
    if connection is not None:
        return insert(connection)
    with get_connection() as owned_connection:
        return insert(owned_connection)


def enqueue_archive_job(
    *,
    organization_id: str,
    owner_id: int,
    storage_key: str,
    archive_filename: str,
    collection_id: int | None,
    idempotency_key: str,
) -> str:
    job_id = str(uuid4())
    pipeline_key = (
        f"{organization_id}:archive_ingestion:{idempotency_key}:"
        f"{settings.ingestion_pipeline_version}"
    )
    with get_connection() as connection:
        connection.execute(
            """INSERT INTO ingestion_jobs
               (id, organization_id, owner_id, idempotency_key, job_type,
                request_idempotency_key, pipeline_version, payload_json, max_attempts)
               VALUES (?, ?, ?, ?, 'archive_ingestion', ?, ?, ?, ?)
               ON CONFLICT(organization_id, idempotency_key) DO NOTHING""",
            (
                job_id, organization_id, owner_id, pipeline_key, idempotency_key,
                settings.ingestion_pipeline_version,
                json.dumps({
                    "storage_key": storage_key,
                    "archive_filename": archive_filename,
                    "collection_id": collection_id,
                }),
                settings.ingestion_max_attempts,
            ),
        )
        row = connection.execute(
            """SELECT id FROM ingestion_jobs
               WHERE organization_id = ?
                 AND (idempotency_key = ? OR request_idempotency_key = ?)""",
            (organization_id, pipeline_key, idempotency_key),
        ).fetchone()
    return str(row["id"])


def claim_next_job(worker_id: str) -> str | None:
    """Claim one due job using an immediate transaction and compare-and-set update."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stale_before = (
            datetime.now(timezone.utc)
            - timedelta(seconds=settings.ingestion_lock_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute(
            """UPDATE ingestion_jobs
               SET status = 'retry_scheduled', available_at = ?,
                   next_retry_at = ?,
                   locked_by = NULL, locked_at = NULL,
                   error_code = 'stale_lock',
                   last_error_code = 'stale_lock',
                   error_message = 'Worker lock expired; job was safely requeued.',
                   last_error_message = 'Worker lock expired; job was safely requeued.',
                   updated_at = ?
               WHERE status = 'processing' AND locked_at < ?""",
            (now, now, now, stale_before),
        )
        row = connection.execute(
            """SELECT id FROM ingestion_jobs
               WHERE status IN ('queued','retry_scheduled')
                 AND available_at <= ?
               ORDER BY created_at, id LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            """UPDATE ingestion_jobs
               SET status = 'processing', locked_by = ?, locked_at = ?,
                   started_at = COALESCE(started_at, ?),
                   attempt_count = attempt_count + 1, updated_at = ?
               WHERE id = ? AND status IN ('queued','retry_scheduled')""",
            (worker_id, now, now, now, row["id"]),
        )
        if cursor.rowcount != 1:
            return None
        job = connection.execute(
            "SELECT version_id, document_id FROM ingestion_jobs WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if job["version_id"] is not None:
            connection.execute(
                """UPDATE document_versions
                   SET status = 'processing', ingestion_status = 'processing',
                       extraction_status = 'processing'
                   WHERE id = ?""",
                (job["version_id"],),
            )
        if job["document_id"] is not None:
            connection.execute(
                """UPDATE documents SET processing_status = 'processing'
                   WHERE id = ? AND current_version_id IS NULL""",
                (job["document_id"],),
            )
        return str(row["id"])


def _fail_or_retry(
    job_id: str,
    code: str,
    message: str,
    *,
    transient: bool,
) -> None:
    safe_message = message[:500] or "Document processing failed."
    with get_connection() as connection:
        job = connection.execute(
            """SELECT attempt_count, max_attempts, version_id, document_id,
                      owner_id, organization_id
               FROM ingestion_jobs WHERE id = ?""",
            (job_id,),
        ).fetchone()
        if job is None:
            return
        terminal = (
            not transient
            or int(job["attempt_count"]) >= int(job["max_attempts"])
        )
        if terminal:
            connection.execute(
                """UPDATE ingestion_jobs SET status = 'failed', error_code = ?,
                   last_error_code = ?, error_message = ?,
                   last_error_message = ?, completed_at = CURRENT_TIMESTAMP,
                   locked_by = NULL, locked_at = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (code, code, safe_message, safe_message, job_id),
            )
            if job["version_id"] is not None:
                connection.execute(
                    """UPDATE document_versions SET status = 'failed',
                       ingestion_status = 'failed',
                       extraction_status = CASE
                           WHEN extraction_status = 'completed' THEN extraction_status
                           ELSE 'failed'
                       END,
                       indexing_status = CASE
                           WHEN indexing_status = 'completed' THEN indexing_status
                           ELSE 'failed'
                       END,
                       processing_error_code = ?, processing_error_message = ?,
                       failure_reason = ?
                       WHERE id = ?""",
                    (code, safe_message, safe_message, job["version_id"]),
                )
            if job["document_id"] is not None:
                connection.execute(
                    """UPDATE documents SET processing_status = 'failed',
                       processing_error = ?
                       WHERE id = ? AND current_version_id IS NULL""",
                    (safe_message, job["document_id"]),
                )
                if code == "DOCUMENT_ALREADY_EXISTS":
                    version = connection.execute(
                        "SELECT content_id FROM document_versions WHERE id = ?",
                        (job["version_id"],),
                    ).fetchone()
                    connection.execute(
                        """UPDATE document_versions
                           SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                               deleted_with_document = 0
                           WHERE id = ? AND organization_id = ?""",
                        (job["owner_id"], job["version_id"], job["organization_id"]),
                    )
                    connection.execute(
                        """UPDATE documents
                           SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                               updated_at = CURRENT_TIMESTAMP
                           WHERE id = ? AND organization_id = ?
                             AND current_version_id IS NULL""",
                        (job["owner_id"], job["document_id"], job["organization_id"]),
                    )
                    if version and version["content_id"] is not None:
                        connection.execute(
                            """UPDATE document_contents
                               SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ?,
                                   deleted_with_document = 0,
                                   processing_status = 'failed'
                               WHERE id = ? AND organization_id = ?
                                 AND NOT EXISTS (
                                   SELECT 1
                                   FROM document_versions dv
                                   JOIN documents d ON d.id = dv.document_id
                                   WHERE dv.content_id = document_contents.id
                                     AND dv.deleted_at IS NULL
                                     AND d.deleted_at IS NULL
                                 )""",
                            (
                                job["owner_id"], version["content_id"],
                                job["organization_id"],
                            ),
                        )
                batch = connection.execute(
                    "SELECT upload_batch_id, owner_id FROM documents WHERE id = ?",
                    (job["document_id"],),
                ).fetchone()
        else:
            seconds = min(
                settings.ingestion_backoff_max_seconds,
                settings.ingestion_backoff_base_seconds
                * (2 ** int(job["attempt_count"]))
                + random.uniform(0, settings.ingestion_backoff_base_seconds),
            )
            available = (
                datetime.now(timezone.utc) + timedelta(seconds=seconds)
            ).strftime("%Y-%m-%d %H:%M:%S")
            connection.execute(
                """UPDATE ingestion_jobs SET status = 'retry_scheduled',
                   available_at = ?, next_retry_at = ?,
                   error_code = ?, last_error_code = ?,
                   error_message = ?, last_error_message = ?,
                   locked_by = NULL, locked_at = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    available, available, code, code,
                    safe_message, safe_message, job_id,
                ),
            )
        job_context = connection.execute(
            """SELECT organization_id, owner_id, document_id, version_id, status
               FROM ingestion_jobs WHERE id = ?""",
            (job_id,),
        ).fetchone()
    log_audit_event(
        event_type="ingestion.job.transition",
        endpoint="worker",
        outcome=str(job_context["status"]),
        user_id=int(job_context["owner_id"]),
        organization_id=str(job_context["organization_id"]),
        job_id=job_id,
        metadata={
            "document_id": (
                int(job_context["document_id"])
                if job_context["document_id"] is not None else None
            ),
            "error_code": code,
        },
    )
    log_event(
        "ingestion.job.transition",
        job_id=job_id,
        organization_id=job_context["organization_id"],
        document_id=job_context["document_id"],
        version_id=job_context["version_id"],
        status=job_context["status"],
        error_code=code,
    )
    if terminal and job["document_id"] is not None and batch and batch["upload_batch_id"]:
        record_batch_result(
            int(batch["upload_batch_id"]), int(batch["owner_id"]), "failed"
        )


def _insert_version_chunks(
    connection: sqlite3.Connection,
    *,
    job,
    content_id: int,
    source_chunks,
) -> list[int]:
    connection.execute(
        "DELETE FROM chunks WHERE version_id = ? AND organization_id = ?",
        (job["version_id"], job["organization_id"]),
    )
    chunk_ids: list[int] = []
    for index, source in enumerate(source_chunks):
        point_id = make_vector_point_id(
            str(job["organization_id"]),
            int(job["version_id"]),
            index,
            settings.embedding_model_version,
        )
        cursor = connection.execute(
            """INSERT INTO chunks
               (content_id, chunk_index, text, embedding, organization_id,
                document_id, version_id, source_type, source_location_json,
                vector_point_id, embedding_model, embedding_dimension,
                indexing_status)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                content_id, index, source.text,
                job["organization_id"], job["document_id"], job["version_id"],
                source.source_type, json.dumps(source.location),
                point_id, settings.embedding_model_version,
                settings.embedding_dimension,
            ),
        )
        chunk_ids.append(int(cursor.lastrowid))
    return chunk_ids


def _resume_existing_chunks(job) -> bool:
    """Finish an interrupted upsert without extracting or embedding a second time."""
    if job["version_id"] is None or job["document_id"] is None:
        return False
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT id, content_id, chunk_index, text, source_type,
                      source_location_json, vector_point_id, embedding_model,
                      embedding_dimension
               FROM chunks
               WHERE organization_id = ? AND document_id = ? AND version_id = ?
                 AND deleted_at IS NULL
               ORDER BY chunk_index""",
            (job["organization_id"], job["document_id"], job["version_id"]),
        ).fetchall()
    if not rows or any(
        not row["vector_point_id"]
        or row["embedding_model"] != settings.embedding_model_version
        or int(row["embedding_dimension"] or 0) != settings.embedding_dimension
        for row in rows
    ):
        return False
    point_ids = [str(row["vector_point_id"]) for row in rows]
    stored_vectors = get_vector_store().get_vectors(point_ids)
    if not all(point_id in stored_vectors for point_id in point_ids):
        return False
    points: list[VectorPoint] = []
    try:
        for row, point_id in zip(rows, point_ids):
            vector = stored_vectors[point_id]
            if len(vector) != settings.embedding_dimension:
                return False
            points.append(VectorPoint(
                organization_id=str(job["organization_id"]),
                owner_id=int(job["owner_id"]),
                document_id=int(job["document_id"]),
                version_id=int(job["version_id"]),
                content_id=int(row["content_id"]),
                chunk_id=int(row["id"]),
                chunk_index=int(row["chunk_index"]),
                vector=vector,
                text=str(row["text"]),
                filename=str(job["display_filename"]),
                visibility=str(job["visibility"]),
                source_type=str(row["source_type"] or "text"),
                source_location=json.loads(row["source_location_json"] or "{}"),
                embedding_model=settings.embedding_model_version,
            ))
    except (TypeError, ValueError):
        return False

    store = get_vector_store()
    if not store.contains_points(point_ids):
        store.upsert_chunks(points)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute(
            """SELECT content_id, normalized_content_hash
               FROM document_versions WHERE id = ? AND organization_id = ?""",
            (job["version_id"], job["organization_id"]),
        ).fetchone()
        if version is None:
            return False
        if settings.vector_store_rollback_dual_write:
            connection.executemany(
                """UPDATE chunks SET embedding = ?
                   WHERE id = ? AND organization_id = ?""",
                [
                    (
                        json.dumps(stored_vectors[point_id]),
                        int(row["id"]),
                        job["organization_id"],
                    )
                    for row, point_id in zip(rows, point_ids)
                ],
            )
        connection.execute(
            """UPDATE chunks
               SET indexing_status = 'completed',
                   qdrant_indexed_at = CURRENT_TIMESTAMP
               WHERE organization_id = ? AND document_id = ? AND version_id = ?
                 AND deleted_at IS NULL""",
            (job["organization_id"], job["document_id"], job["version_id"]),
        )
        connection.execute(
            """UPDATE document_versions
               SET status = 'completed', ingestion_status = 'completed',
                   extraction_status = 'completed', indexing_status = 'completed',
                   completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                   processing_error_code = NULL, processing_error_message = NULL,
                   failure_reason = NULL
               WHERE id = ? AND organization_id = ?""",
            (job["version_id"], job["organization_id"]),
        )
        connection.execute(
            """UPDATE document_contents SET processing_status = 'completed'
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (
                version["content_id"],
                job["organization_id"],
                job["owner_id"],
            ),
        )
        connection.execute(
            """UPDATE documents
               SET current_version_id = ?, content_id = ?,
                   processing_status = 'completed', processing_error = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND organization_id = ?""",
            (
                job["version_id"], version["content_id"], job["document_id"],
                job["organization_id"],
            ),
        )
        connection.execute(
            """UPDATE ingestion_jobs
               SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                   locked_by = NULL, locked_at = NULL, next_retry_at = NULL,
                   error_code = NULL, last_error_code = NULL,
                   error_message = NULL, last_error_message = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'processing'""",
            (job["id"],),
        )
    return True


def process_job(job_id: str) -> None:
    """Process an already-claimed job idempotently and publish deterministic points."""
    with get_connection() as connection:
        job = connection.execute(
            """SELECT j.*, d.display_filename, d.visibility, d.current_version_id,
                      dv.file_hash AS expected_file_hash
               FROM ingestion_jobs j
               LEFT JOIN documents d ON d.id = j.document_id
               LEFT JOIN document_versions dv ON dv.id = j.version_id
               WHERE j.id = ? AND j.status = 'processing'""",
            (job_id,),
        ).fetchone()
    if job is None:
        return
    if job["job_type"] == "archive_ingestion":
        _process_archive_job(job)
        return
    try:
        if _resume_existing_chunks(job):
            log_event(
                "ingestion.job.resumed",
                job_id=job_id,
                organization_id=job["organization_id"],
                document_id=job["document_id"],
                version_id=job["version_id"],
            )
            return
    except Exception as error:
        logger.exception(
            "Interrupted ingestion recovery failed",
            extra={
                "job_id": job_id,
                "owner_id": job["owner_id"],
                "document_id": job["document_id"],
                "version_id": job["version_id"],
                "failure_stage": failure_stage,
                "error_type": type(error).__name__,
            },
        )
        code, public_message, transient = _classify_error(error)
        _fail_or_retry(job_id, code, public_message, transient=transient)
        return
    failure_stage = "stored_upload_validation"
    try:
        payload = json.loads(job["payload_json"])
        allow_active_content_reuse = bool(
            payload.get("allow_active_content_reuse", False)
        ) or job["current_version_id"] is not None
        path = resolve_storage_key(
            payload.get("storage_key") or payload["stored_filename"]
        )
        if not path.is_file():
            raise FileNotFoundError("Stored upload is unavailable.")
        stored_bytes = path.read_bytes()
        if sha256(stored_bytes).hexdigest() != str(job["expected_file_hash"]):
            raise ValueError("Stored upload hash does not match the accepted version.")
        validate_file_signature(path.name, stored_bytes)
        failure_stage = "extraction"
        extraction_started = perf_counter()
        source_chunks, source_metadata, workbook = _extract_bundle(path)
        extraction_duration_ms = (perf_counter() - extraction_started) * 1000
        failure_stage = "duplicate_resolution"
        with get_connection() as connection:
            connection.execute(
                """UPDATE ingestion_jobs SET extraction_duration_ms = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (extraction_duration_ms, job_id),
            )
        texts = [chunk.text for chunk in source_chunks]
        validate_chunks(texts)
        extracted_text = normalize_extracted_text("\n\n".join(texts))
        validate_extracted_text(extracted_text)
        normalized_hash = sha256(extracted_text.encode("utf-8")).hexdigest()
        reused_existing_content = False
        reused_deleted_content = False
        embeddings: list[list[float]] = []
        chunk_ids: list[int] = []
        reusable_point_ids: list[str] = []
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute(
                """SELECT content_id FROM document_versions
                   WHERE id = ? AND organization_id = ? AND document_id = ?""",
                (job["version_id"], job["organization_id"], job["document_id"]),
            ).fetchone()
            if version is None or version["content_id"] is None:
                raise IngestionPolicyError(
                    "DOCUMENT_REUPLOAD_FAILED",
                    "The deleted document could not be restored.",
                )
            placeholder_content_id = int(version["content_id"])
            resolution = _resolve_content(
                connection,
                job=job,
                placeholder_content_id=placeholder_content_id,
                normalized_hash=normalized_hash,
                source_chunks=source_chunks,
                allow_active_reuse=allow_active_content_reuse,
            )
            if resolution.mode == "active_duplicate":
                raise IngestionPolicyError(
                    "DOCUMENT_ALREADY_EXISTS",
                    "An identical active document already exists.",
                )
            content_id = placeholder_content_id
            if resolution.mode in {
                "active_reuse", "deleted_rebuild", "deleted_reuse"
            }:
                content_id = int(resolution.content_id)
                if resolution.mode.startswith("deleted_"):
                    connection.execute(
                        """UPDATE document_contents
                           SET deleted_at = NULL, deleted_by = NULL,
                               deleted_with_document = 0,
                               processing_status = 'processing'
                           WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                        (content_id, job["organization_id"], job["owner_id"]),
                    )
                    reused_deleted_content = True
                connection.execute(
                    """UPDATE documents SET content_id = ?
                       WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                    (
                        content_id, job["document_id"], job["organization_id"],
                        job["owner_id"],
                    ),
                )
                connection.execute(
                    """UPDATE document_versions
                       SET content_id = ?
                       WHERE id = ? AND organization_id = ?""",
                    (content_id, job["version_id"], job["organization_id"]),
                )
                connection.execute(
                    """DELETE FROM document_contents
                       WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                    (
                        placeholder_content_id, job["organization_id"],
                        job["owner_id"],
                    ),
                )
            try:
                connection.execute(
                    """UPDATE document_contents SET extracted_text = ?,
                       normalized_content_hash = ?, processing_status = 'processing'
                       WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                    (
                        extracted_text, normalized_hash, content_id,
                        job["organization_id"], job["owner_id"],
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise IngestionPolicyError(
                    "DOCUMENT_ALREADY_EXISTS",
                    "An identical active document already exists.",
                ) from error
            connection.execute(
                """UPDATE document_versions
                   SET content_id = ?, normalized_content_hash = ?,
                       source_metadata_json = ?, extraction_status = 'completed',
                       indexing_status = 'queued'
                   WHERE id = ? AND organization_id = ?""",
                (
                    content_id, normalized_hash, json.dumps(source_metadata),
                    job["version_id"], job["organization_id"],
                ),
            )
            chunk_ids = _insert_version_chunks(
                connection,
                job=job,
                content_id=content_id,
                source_chunks=source_chunks,
            )
            if not reused_deleted_content:
                structured_context = StructuredDocumentContext(
                    document_id=int(job["document_id"]),
                    version_id=int(job["version_id"]),
                    content_id=content_id,
                    owner_id=int(job["owner_id"]),
                    organization_id=str(job["organization_id"]),
                )
                if path.suffix.lower() == ".csv":
                    ingest_structured_csv(
                        connection,
                        context=structured_context,
                        validated_path=path,
                    )
                elif workbook is not None:
                    replace_workbook_content(
                        connection,
                        context=structured_context,
                        workbook=workbook,
                    )
            reusable_point_ids = [
                str(row["vector_point_id"])
                for row in resolution.reusable_chunks
            ]

        if reusable_point_ids:
            stored_vectors = get_vector_store().get_vectors(reusable_point_ids)
            if all(point_id in stored_vectors for point_id in reusable_point_ids):
                candidate_embeddings = [
                    stored_vectors[point_id] for point_id in reusable_point_ids
                ]
                if all(
                    len(vector) == settings.embedding_dimension
                    for vector in candidate_embeddings
                ):
                    embeddings = candidate_embeddings
                    reused_existing_content = True

        if not reused_existing_content:
            failure_stage = "embedding"
            embedding_started = perf_counter()
            for offset in range(0, len(texts), settings.embedding_batch_size):
                embeddings.extend(
                    create_embeddings(texts[offset:offset + settings.embedding_batch_size])
                )
            if len(embeddings) != len(source_chunks):
                raise RuntimeError("Embedding count did not match extracted chunks.")
            embedding_duration_ms = (perf_counter() - embedding_started) * 1000
            with get_connection() as connection:
                connection.execute(
                    """UPDATE ingestion_jobs SET embedding_duration_ms = ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (embedding_duration_ms, job_id),
                )
        else:
            with get_connection() as connection:
                connection.execute(
                    """UPDATE ingestion_jobs SET embedding_duration_ms = 0,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (job_id,),
                )

        with get_connection() as connection:
            connection.execute(
                """UPDATE document_versions
                   SET extraction_status = 'completed', indexing_status = 'processing'
                   WHERE id = ?""",
                (job["version_id"],),
            )

        points = [
            VectorPoint(
                organization_id=str(job["organization_id"]),
                owner_id=int(job["owner_id"]),
                document_id=int(job["document_id"]),
                version_id=int(job["version_id"]),
                content_id=content_id,
                chunk_id=chunk_ids[index],
                chunk_index=index,
                vector=embeddings[index],
                text=source.text,
                filename=str(job["display_filename"]),
                visibility=str(job["visibility"]),
                source_type=source.source_type,
                source_location=source.location,
                embedding_model=settings.embedding_model_version,
            )
            for index, source in enumerate(source_chunks)
        ]
        with get_connection() as connection:
            connection.executemany(
                """UPDATE chunks
                   SET token_count = ?, vector_point_id = ?,
                       indexing_status = 'pending', qdrant_indexed_at = NULL
                   WHERE id = ? AND organization_id = ?""",
                [
                    (
                        len(source.text.split()),
                        points[index].point_id,
                        chunk_ids[index],
                        job["organization_id"],
                    )
                    for index, source in enumerate(source_chunks)
                ],
            )
        failure_stage = "vector_indexing"
        indexing_started = perf_counter()
        try:
            get_vector_store().upsert_chunks(points)
        except Exception:
            indexing_duration_ms = (perf_counter() - indexing_started) * 1000
            with get_connection() as connection:
                connection.execute(
                    """UPDATE chunks SET indexing_status = 'failed'
                       WHERE organization_id = ? AND document_id = ?
                         AND version_id = ? AND deleted_at IS NULL""",
                    (
                        job["organization_id"], job["document_id"],
                        job["version_id"],
                    ),
                )
                connection.execute(
                    """UPDATE ingestion_jobs SET indexing_duration_ms = ?,
                       vector_upsert_failures = vector_upsert_failures + 1,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (indexing_duration_ms, job_id),
                )
            raise
        indexing_duration_ms = (perf_counter() - indexing_started) * 1000
        failure_stage = "finalization"
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if settings.vector_store_rollback_dual_write:
                connection.executemany(
                    """UPDATE chunks SET embedding = ?
                       WHERE id = ? AND organization_id = ?""",
                    [
                        (
                            json.dumps(embeddings[index]),
                            chunk_ids[index],
                            job["organization_id"],
                        )
                        for index in range(len(chunk_ids))
                    ],
                )
            connection.execute(
                """UPDATE chunks
                   SET indexing_status = 'completed',
                       qdrant_indexed_at = CURRENT_TIMESTAMP
                   WHERE organization_id = ? AND document_id = ? AND version_id = ?
                     AND deleted_at IS NULL""",
                (
                    job["organization_id"], job["document_id"],
                    job["version_id"],
                ),
            )
            connection.execute(
                """UPDATE document_versions SET status = 'completed',
                   ingestion_status = 'completed',
                   extraction_status = 'completed', indexing_status = 'completed',
                   normalized_content_hash = ?, completed_at = CURRENT_TIMESTAMP,
                   processing_error_code = NULL, processing_error_message = NULL,
                   failure_reason = NULL, source_metadata_json = ?
                   WHERE id = ?""",
                (normalized_hash, json.dumps(source_metadata), job["version_id"]),
            )
            connection.execute(
                """UPDATE document_contents SET processing_status = 'completed'
                   WHERE id = ? AND organization_id = ? AND owner_id = ?""",
                (content_id, job["organization_id"], job["owner_id"]),
            )
            connection.execute(
                """UPDATE documents SET current_version_id = ?,
                   content_id = ?, processing_status = 'completed',
                   processing_error = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (job["version_id"], content_id, job["document_id"]),
            )
            connection.execute(
                """UPDATE ingestion_jobs SET status = 'completed',
                   completed_at = CURRENT_TIMESTAMP, locked_by = NULL, locked_at = NULL,
                   error_code = NULL, last_error_code = NULL,
                    error_message = NULL, last_error_message = NULL,
                    next_retry_at = NULL, indexing_duration_ms = ?,
                    chunks_created = ?, result_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    indexing_duration_ms,
                    len(source_chunks),
                    json.dumps({
                        "content_reused": (
                            reused_existing_content or reused_deleted_content
                        ),
                        "reused_deleted_content": reused_deleted_content,
                        "message": (
                            "Deleted document content was re-uploaded successfully."
                            if reused_deleted_content
                            else (
                                "Document version reused existing indexed content."
                                if reused_existing_content
                                else "Document processed successfully."
                            )
                        ),
                    }),
                    job_id,
                ),
            )
            deleted = connection.execute(
                "SELECT deleted_at IS NOT NULL FROM documents WHERE id = ?",
                (job["document_id"],),
            ).fetchone()[0]
            if deleted:
                connection.execute(
                    """UPDATE chunks SET deleted_at = CURRENT_TIMESTAMP
                       WHERE document_id = ? AND version_id = ?""",
                    (job["document_id"], job["version_id"]),
                )
        if deleted:
            get_vector_store().set_document_deleted(
                str(job["organization_id"]), int(job["document_id"]), True
            )
        log_audit_event(
            event_type="ingestion.job.transition",
            endpoint="worker",
            outcome="completed",
            user_id=int(job["owner_id"]),
            organization_id=str(job["organization_id"]),
            job_id=job_id,
            metadata={
                "document_id": int(job["document_id"]),
                "version_id": int(job["version_id"]),
                "chunk_count": len(source_chunks),
            },
        )
        if reused_deleted_content:
            log_audit_event(
                event_type="document.reupload.reused",
                endpoint="worker",
                outcome="success",
                user_id=int(job["owner_id"]),
                organization_id=str(job["organization_id"]),
                job_id=job_id,
                metadata={
                    "document_id": int(job["document_id"]),
                    "version_id": int(job["version_id"]),
                    "content_reused": True,
                },
            )
        log_event(
            "ingestion.job.transition",
            job_id=job_id,
            organization_id=job["organization_id"],
            status="completed",
            document_id=job["document_id"],
            version_id=job["version_id"],
            chunk_count=len(source_chunks),
        )
        with get_connection() as connection:
            batch = connection.execute(
                "SELECT upload_batch_id, owner_id FROM documents WHERE id = ?",
                (job["document_id"],),
            ).fetchone()
        if batch and batch["upload_batch_id"]:
            record_batch_result(
                int(batch["upload_batch_id"]), int(batch["owner_id"]), "successful"
            )
    except Exception as error:
        logger.exception(
            "Document ingestion failed",
            extra={
                "job_id": job_id,
                "owner_id": job["owner_id"],
                "document_id": job["document_id"],
                "version_id": job["version_id"],
                "failure_stage": failure_stage,
                "error_type": type(error).__name__,
            },
        )
        code, public_message, transient = _classify_error(error)
        _fail_or_retry(
            job_id, code, public_message, transient=transient
        )


def _create_archive_member_job(
    connection: sqlite3.Connection,
    *,
    parent_job,
    filename: str,
    stored_filename: str,
    storage_key: str,
    file_hash: str,
    collection_id: int | None,
    member_index: int,
) -> dict[str, object]:
    owner_id = int(parent_job["owner_id"])
    organization_id = str(parent_job["organization_id"])
    version_number = 1
    content_cursor = connection.execute(
        """INSERT INTO document_contents
           (owner_id, organization_id, file_hash, normalized_content_hash,
            extracted_text, processing_status)
           VALUES (?, ?, ?, ?, '', 'pending')""",
        (owner_id, organization_id, file_hash, f"queued:{uuid4()}"),
    )
    content_id = int(content_cursor.lastrowid)
    display = generate_unique_display_filename(connection, owner_id, filename)
    document_id = int(connection.execute(
        """INSERT INTO documents
           (owner_id, organization_id, original_filename, display_filename,
            stored_filename, file_hash, content_id, collection_id,
            visibility, processing_status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'private', 'queued',
                   CURRENT_TIMESTAMP)""",
        (
            owner_id, organization_id, filename, display, stored_filename,
            file_hash, content_id, collection_id,
        ),
    ).lastrowid)
    version_id = int(connection.execute(
        """INSERT INTO document_versions
           (organization_id, document_id, version_number, content_id,
            stored_filename, storage_key, mime_type, file_size, file_hash,
            status, ingestion_status, extraction_status, indexing_status,
            created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued',
                   'queued', 'queued', ?)""",
        (
            organization_id, document_id, version_number, content_id,
            stored_filename, storage_key, mimetypes.guess_type(filename)[0],
            resolve_storage_key(storage_key).stat().st_size,
            file_hash, owner_id,
        ),
    ).lastrowid)
    child_key = f"{parent_job['id']}:{member_index}:{file_hash}"
    child_id = enqueue_job(
        organization_id=organization_id,
        owner_id=owner_id,
        document_id=document_id,
        version_id=version_id,
        storage_key=storage_key,
        idempotency_key=child_key,
        connection=connection,
    )
    return {
        "filename": filename,
        "status": "queued",
        "document_id": document_id,
        "version_id": version_id,
        "job_id": child_id,
    }


def _process_archive_job(job) -> None:
    try:
        payload = json.loads(job["payload_json"])
        archive_path = resolve_storage_key(
            payload.get("storage_key") or payload["stored_filename"]
        )
        plan = inspect_archive(archive_path)
        results: list[dict[str, object]] = []
        with temporary_archive_directory() as temporary:
            extraction_root = Path(temporary)
            with zipfile.ZipFile(archive_path) as archive:
                for index, member in enumerate(plan.members):
                    if member.rejection_reason:
                        results.append({
                            "filename": member.filename,
                            "status": "rejected",
                            "reason": member.rejection_reason,
                        })
                        continue
                    try:
                        extracted = extract_member(archive, member, extraction_root)
                        content = extracted.read_bytes()
                        validate_file_signature(member.filename, content)
                        stored_filename = f"{uuid4().hex}{Path(member.filename).suffix.lower()}"
                        storage_key = storage_key_for(
                            str(job["organization_id"]), stored_filename
                        )
                        write_storage_bytes(storage_key, content)
                        with get_connection() as connection:
                            connection.execute("BEGIN IMMEDIATE")
                            results.append(_create_archive_member_job(
                                connection,
                                parent_job=job,
                                filename=member.filename,
                                stored_filename=stored_filename,
                                storage_key=storage_key,
                                file_hash=sha256(content).hexdigest(),
                                collection_id=payload.get("collection_id"),
                                member_index=index,
                            ))
                    except Exception as member_error:
                        logger.exception(
                            "Archive member ingestion was rejected",
                            extra={
                                "job_id": str(job["id"]),
                                "member": member.filename,
                                "error_type": type(member_error).__name__,
                            },
                        )
                        error_code, public_message, _ = _classify_error(member_error)
                        results.append({
                            "filename": member.filename,
                            "status": "rejected",
                            "error_code": error_code,
                            "reason": public_message[:300],
                        })
        result = {
            "archive": payload["archive_filename"],
            "summary": {
                "total_entries": len(results),
                "queued": sum(item["status"] == "queued" for item in results),
                "duplicates": sum(item["status"] == "duplicate" for item in results),
                "rejected": sum(item["status"] == "rejected" for item in results),
            },
            "files": results,
        }
        with get_connection() as connection:
            connection.execute(
                """UPDATE ingestion_jobs SET status = 'completed', result_json = ?,
                   completed_at = CURRENT_TIMESTAMP, locked_by = NULL,
                   locked_at = NULL, next_retry_at = NULL,
                   error_code = NULL, last_error_code = NULL,
                   error_message = NULL, last_error_message = NULL,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (json.dumps(result), job["id"]),
            )
    except Exception as error:
        logger.exception(
            "Archive ingestion failed",
            extra={"job_id": str(job["id"]), "error_type": type(error).__name__},
        )
        code, public_message, transient = _classify_error(error)
        _fail_or_retry(
            str(job["id"]), code, public_message, transient=transient
        )


def run_one(worker_id: str) -> bool:
    job_id = claim_next_job(worker_id)
    if job_id is None:
        return False
    process_job(job_id)
    return True
