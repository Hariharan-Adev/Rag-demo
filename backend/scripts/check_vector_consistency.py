"""Compare active current SQLite chunks with active vector-store points."""

from __future__ import annotations

import argparse
import json

from app.database import get_connection, initialize_database
from app.services.vector_store import get_vector_store


def check_consistency(
    organization_id: str | None = None,
) -> dict[str, object]:
    """Return count and ID differences without exposing document content."""
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT c.vector_point_id, c.content_id, c.chunk_index
               FROM chunks c
               JOIN documents d
                 ON d.id = c.document_id
                AND d.organization_id = c.organization_id
               JOIN document_versions dv
                 ON dv.id = c.version_id
                AND dv.organization_id = c.organization_id
               WHERE c.deleted_at IS NULL
                 AND d.deleted_at IS NULL
                 AND dv.deleted_at IS NULL
                 AND d.current_version_id = c.version_id
                 AND dv.status = 'completed'
                 AND c.indexing_status = 'completed'
                 AND c.vector_point_id IS NOT NULL
                 AND (? IS NULL OR c.organization_id = ?)""",
            (organization_id, organization_id),
        ).fetchall()

    sqlite_ids = {str(row["vector_point_id"]) for row in rows}
    unique_content_chunks = {
        (int(row["content_id"]), int(row["chunk_index"]))
        for row in rows
    }
    points = get_vector_store().list_active_points(organization_id)
    vector_ids = set(points)
    vector_content_keys = [
        (
            int(payload["content_id"]),
            int(payload["chunk_index"]),
        )
        for payload in points.values()
        if payload.get("content_id") is not None
        and payload.get("chunk_index") is not None
    ]
    duplicate_content_points = len(vector_content_keys) - len(
        set(vector_content_keys)
    )
    missing = sorted(sqlite_ids - vector_ids)
    unexpected = sorted(vector_ids - sqlite_ids)
    return {
        "consistent": not missing and not unexpected,
        "sqlite_indexed_chunks": len(sqlite_ids),
        "sqlite_unique_content_chunks": len(unique_content_chunks),
        "vector_store_active_points": len(vector_ids),
        "duplicate_content_points": duplicate_content_points,
        "missing_point_ids": missing,
        "unexpected_point_ids": unexpected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile active SQLite chunk IDs with the vector store."
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
