"""Local BM25-style exact-term retrieval over already authorized chunk text."""

from __future__ import annotations

from collections import Counter
import math
import re

from app.database import get_connection
from app.services.document_access import READABLE_DOCUMENT_SQL


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "file",
    "for", "from", "how", "in", "is", "it", "me", "of", "on", "or", "show",
    "tell", "than", "the", "them", "there", "these", "they", "this", "those",
    "to", "was", "were", "what", "which", "with",
}


def _tokens(value: object) -> list[str]:
    """Keep identifier fragments and numbers while using deterministic boundaries."""
    return [
        token for token in _TOKEN_PATTERN.findall(str(value).casefold())
        if token not in _STOP_WORDS and len(token) >= 2
    ]


def search_keyword_chunks(
    query: str,
    *,
    owner_id: int,
    organization_id: str,
    limit: int,
    collection_id: int | None = None,
    document_id: int | None = None,
    version_id: int | None = None,
) -> list[dict[str, object]]:
    """Rank ACL-filtered chunks with local BM25 and filename/source-location boosts."""
    query_tokens = _tokens(query)
    if not query_tokens or limit <= 0:
        return []
    version_join = "dv.id = d.current_version_id" if version_id is None else "dv.id = ?"
    params: list[object] = []
    if version_id is not None:
        params.append(version_id)
    params.extend([organization_id, owner_id, owner_id])
    params.extend([collection_id, collection_id, document_id, document_id])
    with get_connection() as connection:
        rows = connection.execute(
            f"""SELECT c.id, c.content_id, c.document_id, c.version_id, c.chunk_index, c.text, c.source_type,
                       c.source_location_json, d.display_filename
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                JOIN document_versions dv
                  ON dv.document_id = d.id
                 AND dv.organization_id = d.organization_id
                 AND {version_join}
                 AND c.version_id = dv.id
                WHERE {READABLE_DOCUMENT_SQL}
                  AND c.organization_id = d.organization_id
                  AND c.deleted_at IS NULL
                  AND d.processing_status = 'completed'
                  AND dv.status = 'completed'
                  AND dv.deleted_at IS NULL
                  AND (? IS NULL OR d.collection_id = ?)
                  AND (? IS NULL OR d.id = ?)
                ORDER BY c.id""",
            params,
        ).fetchall()
    if not rows:
        return []
    corpus = []
    document_frequency: Counter[str] = Counter()
    for row in rows:
        content_tokens = _tokens(row["text"])
        filename_tokens = _tokens(row["display_filename"])
        location_tokens = _tokens(row["source_location_json"] or "")
        terms = set(content_tokens) | set(filename_tokens) | set(location_tokens)
        document_frequency.update(token for token in set(query_tokens) if token in terms)
        corpus.append((row, content_tokens, filename_tokens, location_tokens))
    average_length = sum(len(item[1]) for item in corpus) / len(corpus) or 1.0
    ranked = []
    for row, content_tokens, filename_tokens, location_tokens in corpus:
        content_counts = Counter(content_tokens)
        filename_counts = Counter(filename_tokens)
        location_counts = Counter(location_tokens)
        score = 0.0
        for token in set(query_tokens):
            frequency = content_counts[token] + (4 * filename_counts[token]) + (2 * location_counts[token])
            if not frequency:
                continue
            inverse_frequency = math.log(1 + (len(corpus) - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            normalization = 1.2 * (1 - 0.75 + 0.75 * len(content_tokens) / average_length)
            score += inverse_frequency * (frequency * 2.2) / (frequency + normalization)
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (-item[0], int(item[1]["id"])))
    return [
        {
            "chunk_id": int(row["id"]),
            "content_id": int(row["content_id"]),
            "document_id": int(row["document_id"]),
            "version_id": int(row["version_id"]),
            "chunk_index": int(row["chunk_index"]),
            "filename": str(row["display_filename"]),
            "content": str(row["text"]),
            "source_type": str(row["source_type"] or "text"),
            "source_location_json": str(row["source_location_json"] or "{}"),
            "keyword_score": round(score, 4),
        }
        for score, row in ranked[:limit]
    ]
