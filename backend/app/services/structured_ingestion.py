"""Transactional persistence for structured workbook content."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from app.database import (
    get_connection,
    sanitize_structured_index_error,
)
from app.config import settings
from app.services.document_loader import DocumentParseError
from app.services.embeddings import create_embeddings
from app.services.source_extraction import SourceChunk, extract_source_chunks
from app.services.storage import resolve_storage_key
from app.services.vector_store import VectorPoint, get_vector_store
from app.services.workbooks import (
    STRUCTURED_INDEX_VERSION,
    WorkbookData,
    extract_workbook,
    workbook_chunks,
    workbook_schema,
)
from app.utils.file_validation import validate_file_signature


@dataclass(frozen=True)
class StructuredDocumentContext:
    """Authoritative tenant and document identifiers for one content version."""

    document_id: int
    version_id: int
    content_id: int
    owner_id: int
    organization_id: str


@dataclass(frozen=True)
class StructuredCsvReindexResult:
    document_id: int
    content_id: int
    status: str
    sheet_count: int = 0
    row_count: int = 0


class StructuredCsvReindexError(RuntimeError):
    """A safe operational failure after the failed state has been persisted."""


@dataclass(frozen=True)
class SpreadsheetReindexResult:
    document_id: int
    content_id: int
    status: str
    sheet_count: int = 0
    row_count: int = 0
    chunks_reindexed: int = 0


class SpreadsheetReindexError(RuntimeError):
    """Safe failure from a structured-row and vector spreadsheet rebuild."""


def _validate_context(
    connection: sqlite3.Connection,
    context: StructuredDocumentContext,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT dv.storage_key, dv.stored_filename, dv.file_hash
           FROM documents d
           JOIN document_versions dv
             ON dv.document_id = d.id
            AND dv.organization_id = d.organization_id
           JOIN document_contents dc
             ON dc.id = dv.content_id
            AND dc.organization_id = d.organization_id
           WHERE d.id = ? AND dv.id = ? AND dc.id = ?
             AND d.owner_id = ? AND dc.owner_id = ?
             AND d.organization_id = ? AND dc.organization_id = ?
             AND d.deleted_at IS NULL
             AND dv.deleted_at IS NULL
             AND dc.deleted_at IS NULL""",
        (
            context.document_id,
            context.version_id,
            context.content_id,
            context.owner_id,
            context.owner_id,
            context.organization_id,
            context.organization_id,
        ),
    ).fetchone()
    if row is None:
        raise ValueError("Structured document context is no longer active.")
    return row


def replace_workbook_content(
    connection: sqlite3.Connection,
    *,
    context: StructuredDocumentContext,
    workbook: WorkbookData,
) -> None:
    """Replace one content's structured rows inside the caller's transaction."""
    if not connection.in_transaction:
        raise RuntimeError("Structured persistence requires an active transaction.")
    _validate_context(connection, context)
    # Delete and insert are atomic because this function never commits its caller's work.
    connection.execute(
        "DELETE FROM workbook_sheets WHERE content_id = ?",
        (context.content_id,),
    )
    for sheet_index, sheet in enumerate(workbook.sheets):
        sheet_cursor = connection.execute(
            """INSERT INTO workbook_sheets
               (content_id, owner_id, organization_id, sheet_index,
                name, visibility, status, header_row, headers_json,
                schema_json, processing_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                context.content_id,
                context.owner_id,
                context.organization_id,
                sheet_index,
                sheet.name,
                sheet.state,
                sheet.status,
                sheet.header_row,
                json.dumps(sheet.headers),
                json.dumps(workbook_schema(sheet)),
                sheet.error,
            ),
        )
        connection.executemany(
            """INSERT INTO workbook_rows
               (sheet_id, content_id, owner_id, organization_id,
                row_number, values_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    sheet_cursor.lastrowid,
                    context.content_id,
                    context.owner_id,
                    context.organization_id,
                    row.row_number,
                    json.dumps(row.values),
                )
                for row in sheet.rows
            ],
        )
    connection.execute(
        """UPDATE document_contents
           SET structured_index_status = 'completed',
               structured_index_version = ?,
               structured_indexed_at = CURRENT_TIMESTAMP,
               structured_index_error = NULL
           WHERE id = ? AND organization_id = ? AND owner_id = ?""",
        (
            STRUCTURED_INDEX_VERSION,
            context.content_id,
            context.organization_id,
            context.owner_id,
        ),
    )


def ingest_structured_csv(
    connection: sqlite3.Connection,
    *,
    context: StructuredDocumentContext,
    validated_path: Path,
) -> WorkbookData:
    """Parse and atomically replace structured CSV rows without touching vectors."""
    if not connection.in_transaction:
        raise RuntimeError("Structured CSV ingestion requires an active transaction.")
    version = _validate_context(connection, context)
    expected_path = resolve_storage_key(
        version["storage_key"] or version["stored_filename"]
    ).resolve()
    candidate = validated_path.resolve()
    if candidate != expected_path or candidate.suffix.lower() != ".csv":
        raise ValueError("Stored CSV path does not match the document version.")
    stored_bytes = candidate.read_bytes()
    if sha256(stored_bytes).hexdigest() != str(version["file_hash"]):
        raise ValueError("Stored CSV hash does not match the document version.")
    validate_file_signature(candidate.name, stored_bytes)
    workbook = extract_workbook(candidate)
    # Content-scoped replacement makes retries converge on one sheet/row set.
    replace_workbook_content(
        connection,
        context=context,
        workbook=workbook,
    )
    return workbook


def _safe_reindex_error(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        message = "Stored CSV file is unavailable."
    elif isinstance(error, DocumentParseError):
        message = str(error)
    elif isinstance(error, ValueError):
        message = str(error)
    else:
        message = "Structured CSV reindexing failed."
    return sanitize_structured_index_error(message)


def reindex_existing_csv_document(
    *,
    document_id: int,
    owner_id: int,
    organization_id: str,
) -> StructuredCsvReindexResult:
    """Rebuild structured rows for one active CSV without changing its vectors."""
    failure: StructuredCsvReindexError | None = None
    result: StructuredCsvReindexResult | None = None
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT d.id AS document_id, d.content_id, d.current_version_id,
                      d.deleted_at AS document_deleted_at,
                      dv.deleted_at AS version_deleted_at,
                      dc.deleted_at AS content_deleted_at,
                      dv.storage_key, dv.stored_filename
               FROM documents d
               JOIN document_versions dv
                 ON dv.id = d.current_version_id
                AND dv.document_id = d.id
                AND dv.organization_id = d.organization_id
               JOIN document_contents dc
                 ON dc.id = d.content_id
                AND dc.id = dv.content_id
                AND dc.organization_id = d.organization_id
               WHERE d.id = ? AND d.owner_id = ? AND dc.owner_id = ?
                 AND d.organization_id = ? AND dc.organization_id = ?""",
            (
                document_id,
                owner_id,
                owner_id,
                organization_id,
                organization_id,
            ),
        ).fetchone()
        if row is None:
            raise StructuredCsvReindexError("Document was not found.")
        if (
            row["document_deleted_at"] is not None
            or row["version_deleted_at"] is not None
            or row["content_deleted_at"] is not None
        ):
            return StructuredCsvReindexResult(
                document_id=document_id,
                content_id=int(row["content_id"]),
                status="skipped_deleted",
            )
        storage_key = str(row["storage_key"] or row["stored_filename"])
        if Path(storage_key).suffix.lower() != ".csv":
            raise StructuredCsvReindexError("Document is not a CSV.")

        content_id = int(row["content_id"])
        context = StructuredDocumentContext(
            document_id=document_id,
            version_id=int(row["current_version_id"]),
            content_id=content_id,
            owner_id=owner_id,
            organization_id=organization_id,
        )
        connection.execute(
            """UPDATE document_contents
               SET structured_index_status = 'processing',
                   structured_index_error = NULL
               WHERE id = ? AND owner_id = ? AND organization_id = ?""",
            (content_id, owner_id, organization_id),
        )
        connection.execute("SAVEPOINT structured_csv_records")
        try:
            path = resolve_storage_key(storage_key)
            workbook = ingest_structured_csv(
                connection,
                context=context,
                validated_path=path,
            )
            connection.execute("RELEASE SAVEPOINT structured_csv_records")
            result = StructuredCsvReindexResult(
                document_id=document_id,
                content_id=content_id,
                status="completed",
                sheet_count=len(workbook.sheets),
                row_count=sum(len(sheet.rows) for sheet in workbook.sheets),
            )
        except Exception as error:
            # Preserve prior structured data while committing a retryable failed state.
            connection.execute("ROLLBACK TO SAVEPOINT structured_csv_records")
            connection.execute("RELEASE SAVEPOINT structured_csv_records")
            safe_error = _safe_reindex_error(error)
            connection.execute(
                """UPDATE document_contents
                   SET structured_index_status = 'failed',
                       structured_index_error = ?
                   WHERE id = ? AND owner_id = ? AND organization_id = ?""",
                (safe_error, content_id, owner_id, organization_id),
            )
            failure = StructuredCsvReindexError(safe_error)

    if failure is not None:
        raise failure
    if result is None:
        raise StructuredCsvReindexError("Structured CSV reindexing failed.")
    return result


def reindex_existing_spreadsheet_document(
    *,
    document_id: int,
    owner_id: int,
    organization_id: str,
) -> SpreadsheetReindexResult:
    """Atomically refresh structured rows and labeled vectors for one spreadsheet."""
    with get_connection() as connection:
        row = connection.execute(
            """SELECT d.id AS document_id, d.content_id, d.current_version_id,
                      d.display_filename, d.visibility,
                      d.deleted_at AS document_deleted_at,
                      dv.deleted_at AS version_deleted_at,
                      dc.deleted_at AS content_deleted_at,
                      dv.storage_key, dv.stored_filename, dv.file_hash
               FROM documents d
               JOIN document_versions dv
                 ON dv.id = d.current_version_id
                AND dv.document_id = d.id
                AND dv.organization_id = d.organization_id
               JOIN document_contents dc
                 ON dc.id = d.content_id
                AND dc.id = dv.content_id
                AND dc.organization_id = d.organization_id
               WHERE d.id = ? AND d.owner_id = ? AND dc.owner_id = ?
                 AND d.organization_id = ? AND dc.organization_id = ?""",
            (
                document_id,
                owner_id,
                owner_id,
                organization_id,
                organization_id,
            ),
        ).fetchone()
        if row is None:
            raise SpreadsheetReindexError("Document was not found.")
        if (
            row["document_deleted_at"] is not None
            or row["version_deleted_at"] is not None
            or row["content_deleted_at"] is not None
        ):
            return SpreadsheetReindexResult(
                document_id=document_id,
                content_id=int(row["content_id"]),
                status="skipped_deleted",
            )
        stored_key = str(row["storage_key"] or row["stored_filename"])
        path = resolve_storage_key(stored_key)
        extension = path.suffix.casefold()
        if extension not in {".csv", ".xlsx", ".xls"}:
            raise SpreadsheetReindexError("Document is not a supported spreadsheet.")
        if not path.is_file():
            raise SpreadsheetReindexError("Stored spreadsheet file is unavailable.")
        stored_bytes = path.read_bytes()
        if sha256(stored_bytes).hexdigest() != str(row["file_hash"]):
            raise SpreadsheetReindexError(
                "Stored spreadsheet hash does not match the document version."
            )
        validate_file_signature(path.name, stored_bytes)
        content_id = int(row["content_id"])
        version_id = int(row["current_version_id"])
        old_chunks = connection.execute(
            """SELECT id, chunk_index, text, source_type, source_location_json,
                      vector_point_id, embedding_model
               FROM chunks
               WHERE organization_id = ? AND document_id = ? AND version_id = ?
                 AND deleted_at IS NULL
               ORDER BY chunk_index""",
            (organization_id, document_id, version_id),
        ).fetchall()

    try:
        workbook = extract_workbook(path)
        source_chunks = extract_source_chunks(
            path,
            include_hidden=settings.include_hidden_worksheets,
            include_very_hidden=settings.include_very_hidden_worksheets,
        )
        if len(source_chunks) != len(old_chunks):
            structured_chunks = workbook_chunks(
                workbook,
                str(row["display_filename"]),
            )
            if len(structured_chunks) != len(old_chunks):
                raise SpreadsheetReindexError(
                    "Spreadsheet chunk count changed; upload a new document version."
                )
            source_chunks = [
                SourceChunk(
                    text=text,
                    source_type=str(old_chunks[index]["source_type"] or "excel"),
                    location=json.loads(
                        old_chunks[index]["source_location_json"] or "{}"
                    ),
                )
                for index, (text, _, _) in enumerate(structured_chunks)
            ]
        new_vectors = create_embeddings([chunk.text for chunk in source_chunks])
        if len(new_vectors) != len(source_chunks):
            raise SpreadsheetReindexError(
                "Embedding count did not match the spreadsheet chunk count."
            )
    except SpreadsheetReindexError:
        raise
    except Exception as error:
        raise SpreadsheetReindexError(
            sanitize_structured_index_error(error)
        ) from error

    store = get_vector_store()
    point_ids = [str(chunk["vector_point_id"] or "") for chunk in old_chunks]
    old_vector_map = store.get_vectors([value for value in point_ids if value])
    old_vectors: list[list[float]] = []
    missing_old_texts: list[str] = []
    missing_indexes: list[int] = []
    for index, chunk in enumerate(old_chunks):
        vector = old_vector_map.get(point_ids[index])
        if vector is None:
            missing_indexes.append(index)
            missing_old_texts.append(str(chunk["text"]))
            old_vectors.append([])
        else:
            old_vectors.append(vector)
    if missing_old_texts:
        generated = create_embeddings(missing_old_texts)
        for index, vector in zip(missing_indexes, generated):
            old_vectors[index] = vector

    def points(
        texts_and_locations: list[tuple[str, str, dict[str, object]]],
        vectors: list[list[float]],
    ) -> list[VectorPoint]:
        return [
            VectorPoint(
                organization_id=organization_id,
                owner_id=owner_id,
                document_id=document_id,
                version_id=version_id,
                content_id=content_id,
                chunk_id=int(old_chunks[index]["id"]),
                chunk_index=int(old_chunks[index]["chunk_index"]),
                vector=vectors[index],
                text=text,
                filename=str(row["display_filename"]),
                visibility=str(row["visibility"]),
                source_type=source_type,
                source_location=location,
                embedding_model=str(
                    old_chunks[index]["embedding_model"]
                    or settings.embedding_model_version
                ),
            )
            for index, (text, source_type, location) in enumerate(
                texts_and_locations
            )
        ]

    new_payloads = [
        (chunk.text, chunk.source_type, chunk.location)
        for chunk in source_chunks
    ]
    old_payloads = [
        (
            str(chunk["text"]),
            str(chunk["source_type"] or "text"),
            json.loads(chunk["source_location_json"] or "{}"),
        )
        for chunk in old_chunks
    ]
    new_points = points(new_payloads, new_vectors)
    old_points = points(old_payloads, old_vectors)
    vectors_replaced = False
    try:
        store.upsert_chunks(new_points)
        vectors_replaced = True
        context = StructuredDocumentContext(
            document_id=document_id,
            version_id=version_id,
            content_id=content_id,
            owner_id=owner_id,
            organization_id=organization_id,
        )
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replace_workbook_content(
                connection,
                context=context,
                workbook=workbook,
            )
            connection.executemany(
                """UPDATE chunks
                   SET text = ?, token_count = ?, source_type = ?,
                       source_location_json = ?, vector_point_id = ?,
                       embedding_model = ?, embedding_dimension = ?,
                       indexing_status = 'completed',
                       qdrant_indexed_at = CURRENT_TIMESTAMP,
                       embedding = CASE WHEN ? THEN ? ELSE embedding END
                   WHERE id = ? AND organization_id = ?""",
                [
                    (
                        chunk.text,
                        len(chunk.text.split()),
                        chunk.source_type,
                        json.dumps(chunk.location),
                        new_points[index].point_id,
                        new_points[index].embedding_model,
                        len(new_vectors[index]),
                        int(settings.vector_store_rollback_dual_write),
                        json.dumps(new_vectors[index]),
                        int(old_chunks[index]["id"]),
                        organization_id,
                    )
                    for index, chunk in enumerate(source_chunks)
                ],
            )
    except Exception as error:
        if vectors_replaced:
            try:
                store.upsert_chunks(old_points)
            except Exception:
                pass
        with get_connection() as connection:
            connection.execute(
                """UPDATE document_contents
                   SET structured_index_status = 'failed',
                       structured_index_error = ?
                   WHERE id = ? AND owner_id = ? AND organization_id = ?""",
                (
                    sanitize_structured_index_error(error),
                    content_id,
                    owner_id,
                    organization_id,
                ),
            )
        raise SpreadsheetReindexError(
            "Spreadsheet reindexing failed; the previous index was retained."
        ) from error

    return SpreadsheetReindexResult(
        document_id=document_id,
        content_id=content_id,
        status="completed",
        sheet_count=len(workbook.sheets),
        row_count=sum(len(sheet.rows) for sheet in workbook.sheets),
        chunks_reindexed=len(source_chunks),
    )
