"""One-time, non-destructive migration of active SQLite vectors to Qdrant."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

from app.config import settings
from app.database import get_connection, initialize_database
from app.services.embeddings import create_embeddings
from app.services.vector_store import (
    VectorPoint,
    VectorStore,
    get_vector_store,
    make_vector_point_id,
)


@dataclass(frozen=True)
class MigrationReport:
    """Machine-readable migration outcome without customer content."""

    active_chunks: int
    reused_sqlite_vectors: int
    regenerated_vectors: int
    upserted_points: int
    verified_points: int
    smoke_queries: int
    organizations: int
    applied: bool
    legacy_vectors_preserved: bool = True


def _batches(values: Sequence, size: int) -> Iterable[Sequence]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _active_chunks(organization_id: str | None) -> list[sqlite3.Row]:
    with get_connection() as connection:
        return connection.execute(
            """SELECT c.id, c.content_id, c.organization_id, c.document_id,
                      c.version_id, c.chunk_index, c.text, c.embedding,
                      c.source_type, c.source_location_json,
                      c.embedding_model, c.embedding_dimension,
                      c.vector_point_id, d.owner_id, d.display_filename,
                      d.visibility
               FROM chunks c
               JOIN documents d
                 ON d.id = c.document_id
                AND d.organization_id = c.organization_id
               JOIN document_versions dv
                 ON dv.id = c.version_id
                AND dv.document_id = c.document_id
                AND dv.organization_id = c.organization_id
               JOIN document_contents dc
                 ON dc.id = c.content_id
                AND dc.organization_id = c.organization_id
               WHERE c.deleted_at IS NULL
                 AND d.deleted_at IS NULL
                 AND dv.deleted_at IS NULL
                 AND dc.deleted_at IS NULL
                 AND d.current_version_id = c.version_id
                 AND dv.status = 'completed'
                 AND dc.processing_status = 'completed'
                 AND (? IS NULL OR c.organization_id = ?)
               ORDER BY c.organization_id, c.document_id, c.chunk_index""",
            (organization_id, organization_id),
        ).fetchall()


def _valid_legacy_vector(row: sqlite3.Row) -> list[float] | None:
    if (
        not row["embedding"]
        or row["embedding_model"] != settings.embedding_model_version
    ):
        return None
    if (
        row["embedding_dimension"] is not None
        and int(row["embedding_dimension"]) != settings.embedding_dimension
    ):
        return None
    try:
        raw = json.loads(row["embedding"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list) or len(raw) != settings.embedding_dimension:
        return None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in raw
    ):
        return None
    return [float(value) for value in raw]


def _point_id(row: sqlite3.Row) -> str:
    return make_vector_point_id(
        str(row["organization_id"]),
        int(row["version_id"]),
        int(row["chunk_index"]),
        settings.embedding_model_version,
    )


def _validate_point_id_ownership(
    rows: Sequence[sqlite3.Row],
    point_ids: Sequence[str],
) -> None:
    expected_owner = {
        point_id: int(row["id"])
        for row, point_id in zip(rows, point_ids)
    }
    if len(expected_owner) != len(rows):
        raise RuntimeError(
            "Stable point ID collision exists among active chunks; migration aborted."
        )
    for batch in _batches(list(point_ids), 500):
        placeholders = ",".join("?" for _ in batch)
        with get_connection() as connection:
            existing = connection.execute(
                f"""SELECT id, vector_point_id FROM chunks
                    WHERE vector_point_id IN ({placeholders})""",
                tuple(batch),
            ).fetchall()
        for row in existing:
            if expected_owner[str(row["vector_point_id"])] != int(row["id"]):
                raise RuntimeError(
                    "Stable point ID is owned by another chunk; migration aborted."
                )


def _update_chunks(
    chunk_ids: Sequence[int],
    *,
    status: str,
    set_indexed_at: bool,
) -> None:
    for batch in _batches(list(chunk_ids), 500):
        placeholders = ",".join("?" for _ in batch)
        indexed_at = (
            "qdrant_indexed_at = CURRENT_TIMESTAMP"
            if set_indexed_at
            else "qdrant_indexed_at = NULL"
        )
        with get_connection() as connection:
            connection.execute(
                f"""UPDATE chunks
                    SET embedding_model = ?, embedding_dimension = ?,
                        indexing_status = ?, {indexed_at}
                    WHERE id IN ({placeholders})""",
                (
                    settings.embedding_model_version,
                    settings.embedding_dimension,
                    status,
                    *batch,
                ),
            )


def _assign_pending_ids(
    rows: Sequence[sqlite3.Row],
    point_ids: Sequence[str],
) -> None:
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            """UPDATE chunks
               SET vector_point_id = ?, embedding_model = ?,
                   embedding_dimension = ?, indexing_status = 'pending',
                   qdrant_indexed_at = NULL
               WHERE id = ? AND organization_id = ?""",
            [
                (
                    point_id,
                    settings.embedding_model_version,
                    settings.embedding_dimension,
                    int(row["id"]),
                    str(row["organization_id"]),
                )
                for row, point_id in zip(rows, point_ids)
            ],
        )


def _vectors_for_batch(
    rows: Sequence[sqlite3.Row],
) -> tuple[list[list[float]], int, int]:
    vectors: list[list[float] | None] = [
        _valid_legacy_vector(row) for row in rows
    ]
    missing = [index for index, vector in enumerate(vectors) if vector is None]
    for indexes in _batches(missing, settings.embedding_batch_size):
        generated = create_embeddings(
            [str(rows[index]["text"]) for index in indexes]
        )
        if len(generated) != len(indexes):
            raise RuntimeError("Embedding count did not match the migration batch.")
        for index, vector in zip(indexes, generated):
            if len(vector) != settings.embedding_dimension:
                raise RuntimeError(
                    "Generated embedding dimension does not match configuration."
                )
            vectors[index] = vector
    return (
        [vector or [] for vector in vectors],
        len(rows) - len(missing),
        len(missing),
    )


def _make_points(
    rows: Sequence[sqlite3.Row],
    vectors: Sequence[list[float]],
) -> list[VectorPoint]:
    return [
        VectorPoint(
            organization_id=str(row["organization_id"]),
            owner_id=int(row["owner_id"]),
            document_id=int(row["document_id"]),
            version_id=int(row["version_id"]),
            content_id=int(row["content_id"]),
            chunk_id=int(row["id"]),
            chunk_index=int(row["chunk_index"]),
            vector=vector,
            text=str(row["text"]),
            filename=str(row["display_filename"]),
            visibility=str(row["visibility"]),
            source_type=str(row["source_type"] or "text"),
            source_location=json.loads(row["source_location_json"] or "{}"),
            embedding_model=settings.embedding_model_version,
        )
        for row, vector in zip(rows, vectors)
    ]


def _verify_points(
    store: VectorStore,
    point_ids: Sequence[str],
    batch_size: int,
) -> int:
    verified = 0
    for batch in _batches(list(point_ids), batch_size):
        vectors = store.get_vectors(list(batch))
        if set(vectors) != set(batch):
            missing = len(set(batch) - set(vectors))
            raise RuntimeError(
                f"Qdrant point-count verification failed; {missing} points are missing."
            )
        if any(
            len(vector) != settings.embedding_dimension
            for vector in vectors.values()
        ):
            raise RuntimeError(
                "Qdrant contains a migrated point with an invalid vector dimension."
            )
        verified += len(vectors)
    if verified != len(point_ids):
        raise RuntimeError("Qdrant point-count verification did not match SQLite.")
    return verified


def _run_smoke_queries(
    store: VectorStore,
    samples: Sequence[tuple[sqlite3.Row, list[float]]],
    limit: int,
) -> int:
    completed = 0
    for row, vector in samples[:limit]:
        results = store.search(
            vector,
            organization_id=str(row["organization_id"]),
            user_id=int(row["owner_id"]),
            current_version_ids=[int(row["version_id"])],
            document_id=int(row["document_id"]),
            limit=5,
            score_threshold=None,
        )
        if int(row["id"]) not in {
            int(result["chunk_id"])
            for result in results
            if result.get("chunk_id") is not None
        }:
            raise RuntimeError(
                "Tenant-filtered Qdrant smoke query did not return its source chunk."
            )
        completed += 1
    return completed


def migrate_vectors(
    *,
    apply: bool,
    organization_id: str | None = None,
    upsert_batch_size: int = 256,
    smoke_query_limit: int = 3,
    store: VectorStore | None = None,
) -> MigrationReport:
    """Migrate active current-version vectors while preserving SQLite rollback data."""
    if upsert_batch_size < 1:
        raise ValueError("upsert_batch_size must be positive.")
    if smoke_query_limit < 0:
        raise ValueError("smoke_query_limit cannot be negative.")

    rows = _active_chunks(organization_id)
    point_ids = [_point_id(row) for row in rows]
    _validate_point_id_ownership(rows, point_ids)
    reused = sum(_valid_legacy_vector(row) is not None for row in rows)
    organizations = len({str(row["organization_id"]) for row in rows})
    if not apply or not rows:
        return MigrationReport(
            active_chunks=len(rows),
            reused_sqlite_vectors=reused,
            regenerated_vectors=len(rows) - reused,
            upserted_points=0,
            verified_points=0,
            smoke_queries=0,
            organizations=organizations,
            applied=apply,
        )

    chunk_ids = [int(row["id"]) for row in rows]
    vector_store = store or get_vector_store()
    _assign_pending_ids(rows, point_ids)
    reused_count = 0
    regenerated_count = 0
    upserted_count = 0
    smoke_samples: list[tuple[sqlite3.Row, list[float]]] = []
    sampled_documents: set[tuple[str, int]] = set()
    try:
        for row_batch in _batches(rows, upsert_batch_size):
            vectors, batch_reused, batch_regenerated = _vectors_for_batch(row_batch)
            points = _make_points(row_batch, vectors)
            vector_store.upsert_chunks(points)
            reused_count += batch_reused
            regenerated_count += batch_regenerated
            upserted_count += len(points)
            for row, vector in zip(row_batch, vectors):
                document = (
                    str(row["organization_id"]),
                    int(row["document_id"]),
                )
                if (
                    document not in sampled_documents
                    and len(smoke_samples) < smoke_query_limit
                ):
                    smoke_samples.append((row, vector))
                    sampled_documents.add(document)

        verified_count = _verify_points(
            vector_store, point_ids, upsert_batch_size
        )
        smoke_count = _run_smoke_queries(
            vector_store, smoke_samples, smoke_query_limit
        )
    except Exception:
        _update_chunks(chunk_ids, status="failed", set_indexed_at=False)
        raise

    _update_chunks(chunk_ids, status="completed", set_indexed_at=True)
    return MigrationReport(
        active_chunks=len(rows),
        reused_sqlite_vectors=reused_count,
        regenerated_vectors=regenerated_count,
        upserted_points=upserted_count,
        verified_points=verified_count,
        smoke_queries=smoke_count,
        organizations=organizations,
        applied=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate active SQLite chunk vectors to Qdrant without deleting "
            "the rollback copy."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform writes. Without this flag the command is a dry-run.",
    )
    parser.add_argument("--organization-id")
    parser.add_argument("--upsert-batch-size", type=int, default=256)
    parser.add_argument("--smoke-query-limit", type=int, default=3)
    arguments = parser.parse_args()

    initialize_database()
    report = migrate_vectors(
        apply=arguments.apply,
        organization_id=arguments.organization_id,
        upsert_batch_size=arguments.upsert_batch_size,
        smoke_query_limit=arguments.smoke_query_limit,
    )
    print(json.dumps(asdict(report), sort_keys=True))


if __name__ == "__main__":
    main()
