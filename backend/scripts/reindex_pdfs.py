"""Durably enqueue legacy PDFs for corrected chunking and vector indexing."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import re
import sqlite3
import sys
from typing import Sequence

from app import database
from app.services.ingestion_jobs import enqueue_job


PDF_INDEX_VERSION = "pdf-180-v1"
MAX_BATCH_SIZE = 1000
_ORGANIZATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class Candidate:
    document_id: int
    version_id: int
    owner_id: int
    organization_id: str
    storage_key: str


@dataclass
class ReindexSummary:
    scanned: int = 0
    eligible: int = 0
    enqueued: int = 0
    skipped: int = 0


def _positive_identifier(value: str) -> int:
    try:
        identifier = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if identifier <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return identifier


def _batch_size(value: str) -> int:
    size = _positive_identifier(value)
    if size > MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"must not exceed {MAX_BATCH_SIZE}")
    return size


def _organization_identifier(value: str) -> str:
    identifier = value.strip()
    if not _ORGANIZATION_ID_PATTERN.fullmatch(identifier):
        raise argparse.ArgumentTypeError("invalid organization identifier")
    return identifier


def _read_connection(*, dry_run: bool) -> sqlite3.Connection:
    if not dry_run:
        return database.get_connection()
    connection = sqlite3.connect(
        f"{database.DATABASE_PATH.resolve().as_uri()}?mode=ro",
        uri=True,
        factory=database.ClosingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _candidate_batch(
    *,
    after_document_id: int,
    document_id: int | None,
    owner_id: int | None,
    organization_id: str | None,
    batch_size: int,
    dry_run: bool,
) -> list[Candidate]:
    clauses = [
        "d.id > ?",
        "d.deleted_at IS NULL",
        "dv.deleted_at IS NULL",
        "dv.status = 'completed'",
        "LOWER(COALESCE(dv.storage_key, dv.stored_filename)) LIKE '%.pdf'",
    ]
    parameters: list[object] = [after_document_id]
    if document_id is not None:
        clauses.append("d.id = ?")
        parameters.append(document_id)
    if owner_id is not None:
        clauses.append("d.owner_id = ?")
        parameters.append(owner_id)
    if organization_id is not None:
        clauses.append("d.organization_id = ?")
        parameters.append(organization_id)
    clauses.append(
        """NOT EXISTS (
            SELECT 1
            FROM ingestion_jobs ij
            WHERE ij.organization_id = d.organization_id
              AND ij.version_id = dv.id
              AND ij.job_type = 'document_ingestion'
              AND ij.pipeline_version = ?
              AND ij.status IN ('queued', 'processing',
                                'retry_scheduled', 'completed')
        )"""
    )
    parameters.extend((PDF_INDEX_VERSION, batch_size))
    with _read_connection(dry_run=dry_run) as connection:
        rows = connection.execute(
            f"""SELECT d.id AS document_id, d.owner_id, d.organization_id,
                       dv.id AS version_id,
                       COALESCE(dv.storage_key, dv.stored_filename) AS storage_key
                FROM documents d
                JOIN document_versions dv
                  ON dv.id = d.current_version_id
                 AND dv.document_id = d.id
                 AND dv.organization_id = d.organization_id
                WHERE {' AND '.join(clauses)}
                ORDER BY d.id
                LIMIT ?""",
            parameters,
        ).fetchall()
    return [
        Candidate(
            document_id=int(row["document_id"]),
            version_id=int(row["version_id"]),
            owner_id=int(row["owner_id"]),
            organization_id=str(row["organization_id"]),
            storage_key=str(row["storage_key"]),
        )
        for row in rows
    ]


def run_reindex(
    *,
    dry_run: bool,
    document_id: int | None,
    owner_id: int | None,
    organization_id: str | None,
    batch_size: int,
) -> ReindexSummary:
    summary = ReindexSummary()
    after_document_id = 0
    while True:
        batch = _candidate_batch(
            after_document_id=after_document_id,
            document_id=document_id,
            owner_id=owner_id,
            organization_id=organization_id,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        if not batch:
            break
        after_document_id = batch[-1].document_id
        for candidate in batch:
            summary.scanned += 1
            summary.eligible += 1
            if dry_run:
                continue
            enqueue_job(
                organization_id=candidate.organization_id,
                owner_id=candidate.owner_id,
                document_id=candidate.document_id,
                version_id=candidate.version_id,
                storage_key=candidate.storage_key,
                idempotency_key=(
                    f"{PDF_INDEX_VERSION}:"
                    f"{candidate.document_id}:{candidate.version_id}"
                ),
                allow_active_content_reuse=True,
                pipeline_version=PDF_INDEX_VERSION,
                force_reprocess=True,
            )
            summary.enqueued += 1
        if document_id is not None:
            break
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Queue active PDFs for durable corrected reindexing."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--document-id", type=_positive_identifier)
    parser.add_argument("--owner-id", type=_positive_identifier)
    parser.add_argument("--organization-id", type=_organization_identifier)
    parser.add_argument("--batch-size", type=_batch_size, default=100)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PDF_INDEX_VERSION}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if not arguments.dry_run:
            database.initialize_database()
        summary = run_reindex(
            dry_run=arguments.dry_run,
            document_id=arguments.document_id,
            owner_id=arguments.owner_id,
            organization_id=arguments.organization_id,
            batch_size=arguments.batch_size,
        )
    except (OSError, sqlite3.DatabaseError):
        print("PDF reindexing could not access the configured data.", file=sys.stderr)
        return 3
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
