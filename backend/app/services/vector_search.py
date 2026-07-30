"""Retrieve authorized current-version chunks through the vector-store provider."""

from app.database import get_connection
from app.services.document_access import READABLE_DOCUMENT_SQL
from app.services.embeddings import create_embeddings
from app.services.vector_store import get_vector_store


def search_chunks(
    query: str,
    owner_id: int,
    limit: int = 3,
    collection_id: int | None = None,
    document_id: int | None = None,
    organization_id: str | None = None,
    version_id: int | None = None,
    min_score: float | None = None,
) -> list[dict[str, object]]:
    """Search authorized current versions, or one explicitly requested old version."""
    with get_connection() as connection:
        if organization_id is None:
            user = connection.execute(
                "SELECT organization_id FROM users WHERE id = ?", (owner_id,)
            ).fetchone()
            if user is None:
                return []
            organization_id = str(user["organization_id"])
        if version_id is None:
            rows = connection.execute(
                f"""SELECT d.id, d.current_version_id AS searchable_version_id
                    FROM documents d
                    WHERE {READABLE_DOCUMENT_SQL}
                      AND d.current_version_id IS NOT NULL
                      AND (? IS NULL OR d.collection_id = ?)
                      AND (? IS NULL OR d.id = ?)""",
                (
                    organization_id, owner_id, owner_id,
                    collection_id, collection_id, document_id, document_id,
                ),
            ).fetchall()
        else:
            rows = connection.execute(
                f"""SELECT d.id, dv.id AS searchable_version_id
                    FROM documents d
                    JOIN document_versions dv
                      ON dv.document_id = d.id
                     AND dv.organization_id = d.organization_id
                    WHERE {READABLE_DOCUMENT_SQL}
                      AND dv.id = ?
                      AND dv.status = 'completed'
                      AND dv.deleted_at IS NULL
                      AND (? IS NULL OR d.collection_id = ?)
                      AND (? IS NULL OR d.id = ?)""",
                (
                    organization_id, owner_id, owner_id, version_id,
                    collection_id, collection_id, document_id, document_id,
                ),
            ).fetchall()
    searchable_versions = [int(row["searchable_version_id"]) for row in rows]
    allowed_documents = {int(row["id"]) for row in rows}
    if document_id is not None and document_id not in allowed_documents:
        return []
    vector = create_embeddings([query])[0]
    results = get_vector_store().search(
        vector,
        organization_id=organization_id,
        user_id=owner_id,
        current_version_ids=searchable_versions,
        document_id=document_id,
        limit=limit,
        score_threshold=min_score,
    )
    matches = [
        {
            "chunk_id": result["chunk_id"],
            "document_id": result["document_id"],
            "version_id": result["version_id"],
            "filename": result["filename"],
            "referencing_filenames": [result["filename"]],
            "content": result["text"],
            "source_type": result.get("source_type", "text"),
            "source_location": result.get("source_location", {}),
            "sheet_name": (result.get("source_location") or {}).get("sheet_name"),
            "row_number": (result.get("source_location") or {}).get("row_start"),
            "score": round(float(result["score"]), 4),
        }
        for result in results
        if min_score is None or float(result["score"]) >= min_score
    ]
    return matches
