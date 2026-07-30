"""Rebuild Qdrant points from active current document versions."""

from __future__ import annotations

import argparse
import json

from app.config import settings
from app.database import get_connection, initialize_database
from app.services.vector_store import VectorPoint, get_vector_store


def reindex(*, organization_id: str | None = None) -> int:
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT c.id, c.organization_id, c.document_id, c.version_id,
                      c.chunk_index, c.embedding, c.text, c.source_type,
                      c.source_location_json, d.owner_id, d.display_filename,
                      d.visibility, c.embedding_model
               FROM chunks c
               JOIN documents d ON d.id = c.document_id
               WHERE c.deleted_at IS NULL AND d.deleted_at IS NULL
                 AND d.current_version_id = c.version_id
                 AND c.embedding IS NOT NULL
                 AND (? IS NULL OR c.organization_id = ?)
               ORDER BY c.organization_id, c.document_id, c.chunk_index""",
            (organization_id, organization_id),
        ).fetchall()
    points = [
        VectorPoint(
            organization_id=str(row["organization_id"]),
            owner_id=int(row["owner_id"]),
            document_id=int(row["document_id"]),
            version_id=int(row["version_id"]),
            chunk_id=int(row["id"]),
            chunk_index=int(row["chunk_index"]),
            vector=json.loads(row["embedding"]),
            text=str(row["text"]),
            filename=str(row["display_filename"]),
            visibility=str(row["visibility"]),
            source_type=str(row["source_type"] or "text"),
            source_location=json.loads(row["source_location_json"] or "{}"),
            embedding_model=str(
                row["embedding_model"] or settings.embedding_model_version
            ),
        )
        for row in rows
    ]
    store = get_vector_store()
    store.clear(organization_id)
    batch_size = 256
    for offset in range(0, len(points), batch_size):
        store.upsert_chunks(points[offset:offset + batch_size])
    with get_connection() as connection:
        connection.executemany(
            """UPDATE chunks SET vector_point_id = ?, embedding_model = ?,
                   embedding_dimension = ?
               WHERE id = ? AND organization_id = ?""",
            [
                (
                    point.point_id, point.embedding_model, len(point.vector),
                    point.chunk_id, point.organization_id,
                )
                for point in points
            ],
        )
    return len(points)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild current Qdrant points.")
    parser.add_argument("--organization-id")
    arguments = parser.parse_args()
    initialize_database()
    count = reindex(organization_id=arguments.organization_id)
    print(f"Reindexed {count} current chunks.")


if __name__ == "__main__":
    main()
