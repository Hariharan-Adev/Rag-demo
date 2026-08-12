"""Retrieve authorized current-version chunks through the vector-store provider."""

import json

from app.config import settings
from app.database import get_connection
from app.services.document_access import READABLE_DOCUMENT_SQL
from app.services.embeddings import create_embeddings
from app.services.keyword_search import search_keyword_chunks
from app.services.vector_store import get_vector_store
from app.utils.query_normalization import normalize_retrieval_query


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
    """Fuse authorized vector and keyword results, with a vector-only rollback mode."""
    retrieval_query = normalize_retrieval_query(query)
    if not retrieval_query:
        return []
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
                    JOIN document_versions dv
                      ON dv.id = d.current_version_id
                     AND dv.document_id = d.id
                     AND dv.organization_id = d.organization_id
                    WHERE {READABLE_DOCUMENT_SQL}
                      AND d.current_version_id IS NOT NULL
                      AND dv.status = 'completed'
                      AND dv.deleted_at IS NULL
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
    if not searchable_versions:
        return []
    if document_id is not None and document_id not in allowed_documents:
        return []
    mode = settings.rag_retrieval_mode.strip().casefold()
    if mode not in {"hybrid", "vector"}:
        raise ValueError("RAG_RETRIEVAL_MODE must be 'hybrid' or 'vector'.")
    keyword_matches = (
        search_keyword_chunks(
            retrieval_query,
            owner_id=owner_id,
            organization_id=organization_id,
            limit=settings.rag_keyword_candidate_limit,
            collection_id=collection_id,
            document_id=document_id,
            version_id=version_id,
        )
        if mode == "hybrid" else []
    )
    vector = create_embeddings([retrieval_query])[0]
    results = get_vector_store().search(
        vector,
        organization_id=organization_id,
        user_id=owner_id,
        current_version_ids=searchable_versions,
        document_id=document_id,
        limit=settings.rag_vector_candidate_limit,
        score_threshold=min_score,
    )
    ranked = [
        result for result in results
        if min_score is None or float(result["score"]) >= min_score
    ]
    chunk_ids = [int(result["chunk_id"]) for result in ranked]
    chunk_rows = []
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        with get_connection() as connection:
            chunk_rows = connection.execute(
                f"""SELECT c.id, c.content_id, c.document_id, c.version_id, c.chunk_index, c.text,
                           c.source_type, c.source_location_json,
                           d.display_filename
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.id IN ({placeholders})
                      AND c.organization_id = ?
                      AND c.document_id IN ({",".join("?" for _ in allowed_documents)})
                      AND c.version_id IN ({",".join("?" for _ in searchable_versions)})
                      AND c.deleted_at IS NULL AND d.deleted_at IS NULL""",
                (
                    *chunk_ids,
                    organization_id,
                    *sorted(allowed_documents),
                    *searchable_versions,
                ),
            ).fetchall()
    authoritative = {int(row["id"]): row for row in chunk_rows}
    matches: list[dict[str, object]] = []
    for result in ranked:
        row = authoritative.get(int(result["chunk_id"]))
        if row is None:
            continue
        source_location = json.loads(row["source_location_json"] or "{}")
        matches.append({
            "chunk_id": int(row["id"]),
            "content_id": int(row["content_id"]),
            "document_id": int(row["document_id"]),
            "version_id": int(row["version_id"]),
            "chunk_index": int(row["chunk_index"]),
            "filename": str(row["display_filename"]),
            "referencing_filenames": [str(row["display_filename"])],
            "content": str(row["text"]),
            "source_type": str(row["source_type"] or "text"),
            "source_location": source_location,
            "sheet_name": source_location.get("sheet_name"),
            "row_number": source_location.get("row_start"),
            "score": round(float(result["score"]), 4),
        })
    vector_ranks = {int(match["chunk_id"]): rank for rank, match in enumerate(matches, start=1)}
    keyword_ranks = {int(match["chunk_id"]): rank for rank, match in enumerate(keyword_matches, start=1)}
    combined = {int(match["chunk_id"]): match for match in matches}
    for match in keyword_matches:
        chunk_id = int(match["chunk_id"])
        if chunk_id not in combined:
            source_location = json.loads(str(match["source_location_json"]))
            combined[chunk_id] = {
                "chunk_id": chunk_id,
                "content_id": int(match["content_id"]),
                "document_id": int(match["document_id"]),
                "version_id": int(match["version_id"]),
                "chunk_index": int(match["chunk_index"]),
                "filename": str(match["filename"]),
                "referencing_filenames": [str(match["filename"])],
                "content": str(match["content"]),
                "source_type": str(match["source_type"]),
                "source_location": source_location,
                "sheet_name": source_location.get("sheet_name"),
                "row_number": source_location.get("row_start"),
                "score": 0.0,
            }
        combined[chunk_id]["keyword_score"] = float(match["keyword_score"])
    for match in combined.values():
        chunk_id = int(match["chunk_id"])
        vector_rank = vector_ranks.get(chunk_id)
        keyword_rank = keyword_ranks.get(chunk_id)
        fusion_score = (
            (1 / (settings.rag_rrf_k + vector_rank) if vector_rank else 0)
            + (1 / (settings.rag_rrf_k + keyword_rank) if keyword_rank else 0)
        )
        match["vector_score"] = match.get("score") if vector_rank else None
        match["keyword_score"] = match.get("keyword_score") if keyword_rank else None
        match["score"] = round(float(match.get("score") or 0.0) if vector_rank else float(match.get("keyword_score") or 0.0), 4)
        match["fusion_score"] = round(fusion_score, 6)
    return [
        match
        for match in sorted(
            combined.values(),
            key=lambda match: (-float(match["fusion_score"]), -float(match["score"]), int(match["chunk_id"])),
        )[:limit]
    ]
