"""Read-only reconciliation of SQLite chunk state and active vector-store points."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from typing import Any

from app.database import get_connection, initialize_database
from app.services.vector_store import get_vector_store


def _safe_chunk(row: dict[str, object]) -> dict[str, object]:
    """Keep discrepancy output operationally useful without including chunk text."""
    return {
        key: row[key]
        for key in ("chunk_id", "document_id", "version_id", "vector_point_id")
        if row.get(key) is not None
    }


def _discrepancies(
    active_chunks: list[dict[str, object]],
    all_vector_chunks: list[dict[str, object]],
    active_points: dict[str, dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Classify identifier-only mismatches without mutating SQLite or Qdrant."""
    active_by_id = {
        str(row["vector_point_id"]): row
        for row in active_chunks
        if row.get("vector_point_id")
    }
    all_by_id: dict[str, list[dict[str, object]]] = {}
    for row in all_vector_chunks:
        point_id = row.get("vector_point_id")
        if point_id:
            all_by_id.setdefault(str(point_id), []).append(row)

    sqlite_without_vector = [
        _safe_chunk(row) for row in active_chunks if not row.get("vector_point_id")
    ]
    missing_active_vector = [
        _safe_chunk(row)
        for row in active_chunks
        if row.get("vector_point_id")
        and str(row["vector_point_id"]) not in active_points
    ]
    duplicate_ids = [
        {"vector_point_id": point_id, "chunk_count": count}
        for point_id, count in Counter(
            str(row["vector_point_id"])
            for row in active_chunks
            if row.get("vector_point_id")
        ).items()
        if count > 1
    ]

    invalid_vectors: list[dict[str, object]] = []
    deleted_document_vectors: list[dict[str, object]] = []
    wrong_version_vectors: list[dict[str, object]] = []
    for point_id, payload in active_points.items():
        expected = active_by_id.get(point_id)
        records = all_by_id.get(point_id, [])
        vector = {
            "vector_point_id": point_id,
            "chunk_id": payload.get("chunk_id"),
            "chunk_index": payload.get("chunk_index"),
            "content_id": payload.get("content_id"),
            "organization_id": payload.get("organization_id"),
            "document_id": payload.get("document_id"),
            "version_id": payload.get("document_version_id", payload.get("version_id")),
        }
        if expected is None:
            invalid_vectors.append(vector)
            if any(row.get("document_deleted") for row in records):
                deleted_document_vectors.append(vector)
            elif records:
                wrong_version_vectors.append(vector)
            continue
        if (
            payload.get("document_id") != expected.get("document_id")
            or payload.get("document_version_id", payload.get("version_id"))
            != expected.get("version_id")
        ):
            wrong_version_vectors.append(vector)

    return {
        "sqlite_chunks_without_vector": sqlite_without_vector,
        "sqlite_chunks_missing_active_vector": missing_active_vector,
        "vectors_without_valid_current_sqlite_chunk": invalid_vectors,
        "deleted_document_vectors_marked_active": deleted_document_vectors,
        "wrong_document_version_vectors": wrong_version_vectors,
        "duplicate_active_vector_ids": duplicate_ids,
    }


def _load_sqlite_chunks(
    organization_id: str | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Load lifecycle metadata only; document text is intentionally never selected."""
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT c.id AS chunk_id, c.content_id, c.chunk_index,
                      c.document_id, c.version_id,
                      c.vector_point_id, c.deleted_at AS chunk_deleted_at,
                      d.deleted_at AS document_deleted_at,
                      d.current_version_id, d.processing_status,
                      dv.deleted_at AS version_deleted_at, dv.status AS version_status
               FROM chunks c
               JOIN documents d
                 ON d.id = c.document_id
                AND d.organization_id = c.organization_id
               JOIN document_versions dv
                 ON dv.id = c.version_id
                AND dv.document_id = d.id
                AND dv.organization_id = d.organization_id
               WHERE (? IS NULL OR c.organization_id = ?)""",
            (organization_id, organization_id),
        ).fetchall()
    all_chunks = [
        {
            "chunk_id": int(row["chunk_id"]),
            "content_id": int(row["content_id"]),
            "chunk_index": int(row["chunk_index"]),
            "document_id": int(row["document_id"]),
            "version_id": int(row["version_id"]),
            "vector_point_id": row["vector_point_id"],
            "document_deleted": row["document_deleted_at"] is not None,
            "active": (
                row["chunk_deleted_at"] is None
                and row["document_deleted_at"] is None
                and int(row["current_version_id"] or 0) == int(row["version_id"])
                and row["processing_status"] == "completed"
                and row["version_deleted_at"] is None
                and row["version_status"] == "completed"
            ),
        }
        for row in rows
    ]
    return [row for row in all_chunks if row["active"]], all_chunks


def check_consistency(
    organization_id: str | None = None,
) -> dict[str, object]:
    """Return a structured, read-only active/current vector discrepancy report."""
    active_chunks, all_chunks = _load_sqlite_chunks(organization_id)
    points = get_vector_store().list_active_points(organization_id)
    discrepancies = _discrepancies(active_chunks, all_chunks, points)
    sqlite_ids = {
        str(row["vector_point_id"])
        for row in active_chunks
        if row.get("vector_point_id")
    }
    vector_ids = set(points)
    vector_content_keys = [
        (int(payload["content_id"]), int(payload["chunk_index"]))
        for payload in points.values()
        if payload.get("content_id") is not None
        and payload.get("chunk_index") is not None
    ]
    duplicate_content_points = len(vector_content_keys) - len(set(vector_content_keys))
    consistent = not any(discrepancies.values())
    return {
        "consistent": consistent,
        "organization_id": organization_id,
        "sqlite_indexed_chunks": len(sqlite_ids),
        "sqlite_unique_content_chunks": len({
            (int(row.get("content_id") or 0), int(row.get("chunk_index") or 0))
            for row in active_chunks if row.get("vector_point_id")
        }),
        "vector_store_active_points": len(vector_ids),
        "duplicate_content_points": duplicate_content_points,
        "missing_point_ids": sorted(sqlite_ids - vector_ids),
        "unexpected_point_ids": sorted(vector_ids - sqlite_ids),
        "discrepancies": discrepancies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only reconciliation of current SQLite chunks and vectors."
    )
    parser.add_argument("--organization-id")
    arguments = parser.parse_args()
    initialize_database()
    report = check_consistency(arguments.organization_id)
    print(json.dumps(report, sort_keys=True))
    if not report["consistent"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
