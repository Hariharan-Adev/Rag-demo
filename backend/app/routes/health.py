"""Liveness, readiness, and operational metric endpoints."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.database import get_connection
from app.services.vector_store import get_vector_store

router = APIRouter(tags=["operations"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def readiness() -> dict[str, object]:
    try:
        with get_connection() as connection:
            connection.execute("SELECT 1").fetchone()
        vector = get_vector_store().health()
        if settings.app_environment == "production" and vector.get("mode") != "remote":
            raise RuntimeError("Production requires a remote persistent Qdrant deployment.")
    except Exception as error:
        raise HTTPException(status_code=503, detail="A required dependency is unavailable.") from error
    return {"status": "ready", "database": "ok", "vector_store": vector}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    with get_connection() as connection:
        job_rows = connection.execute(
            """SELECT organization_id, status, COUNT(*) AS count
               FROM ingestion_jobs GROUP BY organization_id, status"""
        ).fetchall()
        document_rows = connection.execute(
            """SELECT organization_id, COUNT(*) AS count FROM documents
               WHERE deleted_at IS NULL GROUP BY organization_id"""
        ).fetchall()
        aggregate_rows = connection.execute(
            """SELECT organization_id,
                      COALESCE(SUM(CASE WHEN attempt_count > 1
                           THEN attempt_count - 1 ELSE 0 END), 0) AS retries,
                      COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0)
                           AS permanent_failures,
                      COALESCE(SUM(chunks_created), 0) AS chunks_created,
                      COALESCE(SUM(vector_upsert_failures), 0) AS vector_failures,
                      COALESCE(AVG(extraction_duration_ms), 0) AS extraction_ms,
                      COALESCE(AVG(embedding_duration_ms), 0) AS embedding_ms,
                      COALESCE(AVG(indexing_duration_ms), 0) AS indexing_ms
               FROM ingestion_jobs GROUP BY organization_id"""
        ).fetchall()
        lifecycle_rows = connection.execute(
            """SELECT organization_id, event_type, COUNT(*) AS count
               FROM audit_events
               WHERE event_type IN ('document.delete', 'document.restore',
                                    'document.hard_delete')
               GROUP BY organization_id, event_type"""
        ).fetchall()
    lines = [
        "# TYPE rag_documents_active gauge",
        "# TYPE rag_ingestion_jobs gauge",
    ]
    lines.extend(
        f'rag_documents_active{{organization_id="{row["organization_id"]}"}} {row["count"]}'
        for row in document_rows
    )
    lines.extend(
        f'rag_ingestion_jobs{{organization_id="{row["organization_id"]}",status="{row["status"]}"}} {row["count"]}'
        for row in job_rows
    )
    lines.extend([
        "# TYPE rag_ingestion_retries_total counter",
        "# TYPE rag_ingestion_permanent_failures_total counter",
        "# TYPE rag_chunks_created_total counter",
        "# TYPE rag_vector_upsert_failures_total counter",
        "# TYPE rag_ingestion_stage_duration_ms gauge",
    ])
    for row in aggregate_rows:
        tenant = row["organization_id"]
        lines.extend([
            f'rag_ingestion_retries_total{{organization_id="{tenant}"}} {row["retries"]}',
            f'rag_ingestion_permanent_failures_total{{organization_id="{tenant}"}} {row["permanent_failures"]}',
            f'rag_chunks_created_total{{organization_id="{tenant}"}} {row["chunks_created"]}',
            f'rag_vector_upsert_failures_total{{organization_id="{tenant}"}} {row["vector_failures"]}',
            f'rag_ingestion_stage_duration_ms{{organization_id="{tenant}",stage="extraction"}} {row["extraction_ms"]}',
            f'rag_ingestion_stage_duration_ms{{organization_id="{tenant}",stage="embedding"}} {row["embedding_ms"]}',
            f'rag_ingestion_stage_duration_ms{{organization_id="{tenant}",stage="indexing"}} {row["indexing_ms"]}',
        ])
    lines.append("# TYPE rag_document_lifecycle_total counter")
    lines.extend(
        f'rag_document_lifecycle_total{{organization_id="{row["organization_id"]}",event="{row["event_type"]}"}} {row["count"]}'
        for row in lifecycle_rows
    )
    return "\n".join(lines) + "\n"
