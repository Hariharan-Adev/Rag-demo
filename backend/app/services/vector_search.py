"""Retrieve authorized current-version chunks through the vector-store provider."""

import json
import re

from app.database import get_connection
from app.services.document_access import READABLE_DOCUMENT_SQL
from app.services.embeddings import create_embeddings
from app.services.vector_store import get_vector_store


_LEXICAL_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "has", "have", "how", "i", "in", "is", "it", "me", "of", "on",
    "or", "overall", "show", "tell", "that", "the", "this", "to", "was",
    "what", "which", "who", "with",
}


def _search_terms(value: str) -> list[str]:
    """Return bounded, meaningful terms for deterministic hybrid retrieval."""
    normalized = value.casefold().replace("%", " percentage ")
    terms: list[str] = []
    for term in re.findall(r"[a-z0-9]+", normalized):
        if len(term) < 2 or term in _LEXICAL_STOP_WORDS or term in terms:
            continue
        terms.append(term)
    # Bound both SQL size and adversarial query cost while favoring specific terms.
    return sorted(terms, key=lambda term: (-len(term), term))[:12]


def _lexical_candidates(
    *,
    query: str,
    organization_id: str,
    allowed_documents: set[int],
    searchable_versions: list[int],
    limit: int,
) -> list[dict[str, object]]:
    """Find exact-term candidates when semantic similarity is too conservative."""
    terms = _search_terms(query)
    if not terms:
        return []
    document_placeholders = ",".join("?" for _ in allowed_documents)
    version_placeholders = ",".join("?" for _ in searchable_versions)
    lexical_conditions: list[str] = []
    lexical_parameters: list[object] = []
    for term in terms:
        lexical_conditions.append(
            "(lower(c.text) LIKE ? OR lower(d.display_filename) LIKE ?)"
        )
        lexical_parameters.extend((f"%{term}%", f"%{term}%"))
        if term == "percentage":
            lexical_conditions.append(r"(c.text LIKE ? ESCAPE '\')")
            lexical_parameters.append(r"%\%%")
    candidate_limit = max(100, limit * 20)
    with get_connection() as connection:
        rows = connection.execute(
            f"""SELECT c.id, c.text, d.display_filename
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.organization_id = ?
                  AND c.document_id IN ({document_placeholders})
                  AND c.version_id IN ({version_placeholders})
                  AND c.deleted_at IS NULL AND d.deleted_at IS NULL
                  AND ({" OR ".join(lexical_conditions)})
                LIMIT ?""",
            (
                organization_id,
                *sorted(allowed_documents),
                *searchable_versions,
                *lexical_parameters,
                candidate_limit,
            ),
        ).fetchall()

    candidates: list[dict[str, object]] = []
    minimum_matches = 1 if len(terms) == 1 else 2
    for row in rows:
        searchable = (
            f"{row['display_filename']} {row['text']}"
            .casefold()
            .replace("%", " percentage ")
        )
        searchable_terms = set(re.findall(r"[a-z0-9]+", searchable))
        matched = sum(term in searchable_terms for term in terms)
        if matched < minimum_matches:
            continue
        coverage = matched / len(terms)
        if coverage < 0.5:
            continue
        candidates.append({
            "chunk_id": int(row["id"]),
            # Keep this comparable to cosine similarity while making a strong
            # exact-term match eligible above the normal retrieval threshold.
            "score": min(0.99, 0.45 + (0.5 * coverage)),
        })
    candidates.sort(key=lambda item: (-float(item["score"]), int(item["chunk_id"])))
    return candidates[:limit]


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
    semantic_ranked = [
        result for result in results
        if min_score is None or float(result["score"]) >= min_score
    ]
    lexical_ranked = _lexical_candidates(
        query=query,
        organization_id=organization_id,
        allowed_documents=allowed_documents,
        searchable_versions=searchable_versions,
        limit=limit,
    )
    merged: dict[int, dict[str, object]] = {}
    for result in [*semantic_ranked, *lexical_ranked]:
        chunk_id = int(result["chunk_id"])
        existing = merged.get(chunk_id)
        if existing is None or float(result["score"]) > float(existing["score"]):
            merged[chunk_id] = result
    ranked = sorted(
        merged.values(),
        key=lambda result: (-float(result["score"]), int(result["chunk_id"])),
    )[:limit]
    chunk_ids = [int(result["chunk_id"]) for result in ranked]
    if not chunk_ids:
        return []
    placeholders = ",".join("?" for _ in chunk_ids)
    with get_connection() as connection:
        chunk_rows = connection.execute(
            f"""SELECT c.id, c.document_id, c.version_id, c.text,
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
            "document_id": int(row["document_id"]),
            "version_id": int(row["version_id"]),
            "filename": str(row["display_filename"]),
            "referencing_filenames": [str(row["display_filename"])],
            "content": str(row["text"]),
            "source_type": str(row["source_type"] or "text"),
            "source_location": source_location,
            "sheet_name": source_location.get("sheet_name"),
            "row_number": source_location.get("row_start"),
            "score": round(float(result["score"]), 4),
        })
    return matches
