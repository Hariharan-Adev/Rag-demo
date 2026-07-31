"""Compare retained SQLite cosine search with Qdrant using a JSONL question set."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from time import perf_counter

from app.database import get_connection, initialize_database
from app.services.document_access import READABLE_DOCUMENT_SQL
from app.services.embeddings import create_embeddings
from app.services.vector_store import QdrantVectorStore, SQLiteVectorStore


def _search_scope(user_id: int, organization_id: str) -> list[int]:
    with get_connection() as connection:
        user = connection.execute(
            """SELECT 1 FROM users WHERE id = ? AND organization_id = ?
               AND deleted_at IS NULL""",
            (user_id, organization_id),
        ).fetchone()
        if user is None:
            raise ValueError("User does not belong to the requested organization.")
        rows = connection.execute(
            f"""SELECT d.current_version_id
                FROM documents d
                WHERE {READABLE_DOCUMENT_SQL}
                  AND d.current_version_id IS NOT NULL""",
            (organization_id, user_id, user_id),
        ).fetchall()
    return [int(row["current_version_id"]) for row in rows]


def _run_search(store, vector, *, user_id, organization_id, version_ids):
    started = perf_counter()
    matches = store.search(
        vector,
        organization_id=organization_id,
        user_id=user_id,
        current_version_ids=version_ids,
        limit=1,
        score_threshold=None,
    )
    elapsed_ms = (perf_counter() - started) * 1000
    return (matches[0] if matches else None), round(elapsed_ms, 3)


def compare(
    *,
    questions_path: Path,
    output_path: Path,
    user_id: int,
    organization_id: str,
) -> int:
    """Write one comparison row per supplied question and return its count."""
    questions = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not 20 <= len(questions) <= 30:
        raise ValueError("The comparison set must contain 20 to 30 questions.")
    version_ids = _search_scope(user_id, organization_id)
    sqlite_store = SQLiteVectorStore()
    qdrant_store = QdrantVectorStore()
    rows = []
    try:
        for item in questions:
            question = str(item["question"]).strip()
            expected_chunk_id = item.get("expected_chunk_id")
            vector = create_embeddings([question])[0]
            sqlite_match, sqlite_ms = _run_search(
                sqlite_store,
                vector,
                user_id=user_id,
                organization_id=organization_id,
                version_ids=version_ids,
            )
            qdrant_match, qdrant_ms = _run_search(
                qdrant_store,
                vector,
                user_id=user_id,
                organization_id=organization_id,
                version_ids=version_ids,
            )
            sqlite_chunk = (
                int(sqlite_match["chunk_id"]) if sqlite_match else None
            )
            qdrant_chunk = (
                int(qdrant_match["chunk_id"]) if qdrant_match else None
            )
            rows.append({
                "question": question,
                "expected_chunk_id": expected_chunk_id,
                "sqlite_chunk_id": sqlite_chunk,
                "sqlite_score": (
                    round(float(sqlite_match["score"]), 6)
                    if sqlite_match else None
                ),
                "sqlite_correct": (
                    sqlite_chunk == int(expected_chunk_id)
                    if expected_chunk_id is not None else ""
                ),
                "sqlite_response_ms": sqlite_ms,
                "qdrant_chunk_id": qdrant_chunk,
                "qdrant_score": (
                    round(float(qdrant_match["score"]), 6)
                    if qdrant_match else None
                ),
                "qdrant_correct": (
                    qdrant_chunk == int(expected_chunk_id)
                    if expected_chunk_id is not None else ""
                ),
                "qdrant_response_ms": qdrant_ms,
            })
    finally:
        client = getattr(qdrant_store, "client", None)
        if client is not None:
            client.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare SQLite and Qdrant retrieval for 20-30 questions."
    )
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--organization-id", required=True)
    arguments = parser.parse_args()
    initialize_database()
    count = compare(
        questions_path=arguments.questions,
        output_path=arguments.output,
        user_id=arguments.user_id,
        organization_id=arguments.organization_id,
    )
    print(f"Wrote {count} retrieval comparisons.")


if __name__ == "__main__":
    main()
