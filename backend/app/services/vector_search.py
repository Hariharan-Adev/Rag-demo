"""Find the most relevant document chunks for a question."""

from json import loads

from app.database import get_connection
from app.services.embeddings import create_embeddings


def search_chunks(
    query: str,
    owner_id: int,
    limit: int = 3,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> list[dict[str, object]]:
    """Return the highest-scoring chunks using cosine similarity."""
    query_embedding = create_embeddings([query])[0]

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                chunks.id,
                chunks.content_id,
                chunks.text,
                chunks.embedding,
                MIN(documents.id) AS document_id,
                MIN(documents.display_filename) AS filename,
                GROUP_CONCAT(documents.display_filename, '|') AS referencing_filenames
            FROM chunks
            JOIN document_contents ON document_contents.id = chunks.content_id
            JOIN documents ON documents.content_id = document_contents.id
            WHERE chunks.embedding IS NOT NULL
            AND documents.owner_id = ?
            AND document_contents.owner_id = ?
            AND document_contents.processing_status = 'completed'
            AND (? IS NULL OR documents.collection_id = ?)
            AND (? IS NULL OR documents.id = ?)
            GROUP BY chunks.id, chunks.content_id, chunks.text, chunks.embedding
            """,
            (owner_id, owner_id, collection_id, collection_id, document_id, document_id),
        ).fetchall()

    results = []

    for row in rows:
        chunk_embedding = loads(row["embedding"])

        # Vectors are normalized, so their dot product is cosine similarity.
        score = sum(
            query_value * chunk_value
            for query_value, chunk_value in zip(query_embedding, chunk_embedding)
        )

        results.append(
            {
                "chunk_id": row["id"],
                "document_id": row["document_id"],
                "content_id": row["content_id"],
                "filename": row["filename"],
                "referencing_filenames": list(dict.fromkeys(
                    str(row["referencing_filenames"] or "").split("|")
                )),
                "content": row["text"],
                "score": round(score, 4),
            }
        )

    return sorted(results, key=lambda item: item["score"], reverse=True)[:limit]
