"""Safely rebuild structured rows and labeled vectors for active spreadsheets."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import re
import sqlite3
import sys
from time import monotonic
from typing import Sequence

from app import database
from app.services.structured_ingestion import (
    SpreadsheetReindexError,
    reindex_existing_spreadsheet_document,
)
from app.services.workbooks import STRUCTURED_INDEX_VERSION


SCRIPT_VERSION = "1.0"
MAX_BATCH_SIZE = 1000
_ORGANIZATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class Candidate:
    document_id: int
    owner_id: int
    organization_id: str
    status: str
    indexed_version: str | None


@dataclass
class ReindexSummary:
    scanned: int = 0
    eligible: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    rows_indexed: int = 0
    chunks_reindexed: int = 0
    duration: float = 0.0


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
        "dc.deleted_at IS NULL",
        """(
            LOWER(COALESCE(dv.storage_key, dv.stored_filename)) LIKE '%.csv'
            OR LOWER(COALESCE(dv.storage_key, dv.stored_filename)) LIKE '%.xlsx'
            OR LOWER(COALESCE(dv.storage_key, dv.stored_filename)) LIKE '%.xls'
        )""",
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
    parameters.append(batch_size)
    with _read_connection(dry_run=dry_run) as connection:
        rows = connection.execute(
            f"""SELECT d.id AS document_id, d.owner_id, d.organization_id,
                       dc.structured_index_status,
                       dc.structured_index_version
                FROM documents d
                JOIN document_versions dv
                  ON dv.id = d.current_version_id
                 AND dv.document_id = d.id
                 AND dv.content_id = d.content_id
                 AND dv.organization_id = d.organization_id
                JOIN document_contents dc
                  ON dc.id = d.content_id
                 AND dc.owner_id = d.owner_id
                 AND dc.organization_id = d.organization_id
                WHERE {' AND '.join(clauses)}
                ORDER BY d.id LIMIT ?""",
            parameters,
        ).fetchall()
    return [
        Candidate(
            document_id=int(row["document_id"]),
            owner_id=int(row["owner_id"]),
            organization_id=str(row["organization_id"]),
            status=str(row["structured_index_status"]),
            indexed_version=(
                str(row["structured_index_version"])
                if row["structured_index_version"] is not None
                else None
            ),
        )
        for row in rows
    ]


def _is_eligible(
    candidate: Candidate,
    *,
    retry_failed: bool,
    force: bool,
) -> bool:
    if force:
        return True
    if candidate.status == "failed":
        return retry_failed
    if candidate.status == "processing":
        return False
    return not (
        candidate.status == "completed"
        and candidate.indexed_version == STRUCTURED_INDEX_VERSION
    )


def run_reindex(
    *,
    dry_run: bool,
    document_id: int | None,
    owner_id: int | None,
    organization_id: str | None,
    batch_size: int,
    retry_failed: bool,
    force: bool,
) -> ReindexSummary:
    started = monotonic()
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
            if not _is_eligible(
                candidate,
                retry_failed=retry_failed,
                force=force,
            ):
                summary.skipped += 1
                continue
            summary.eligible += 1
            if dry_run:
                continue
            try:
                result = reindex_existing_spreadsheet_document(
                    document_id=candidate.document_id,
                    owner_id=candidate.owner_id,
                    organization_id=candidate.organization_id,
                )
            except Exception:
                summary.failed += 1
                print(json.dumps({
                    "document_id": candidate.document_id,
                    "status": "failed",
                    "error": "Spreadsheet reindexing failed.",
                }, sort_keys=True), file=sys.stderr)
                continue
            if result.status == "completed":
                summary.completed += 1
                summary.rows_indexed += result.row_count
                summary.chunks_reindexed += result.chunks_reindexed
            else:
                summary.skipped += 1
        if document_id is not None:
            break
    summary.duration = round(monotonic() - started, 3)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reindex active CSV, XLSX, and XLS documents."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--document-id", type=_positive_identifier)
    parser.add_argument("--owner-id", type=_positive_identifier)
    parser.add_argument("--organization-id", type=_organization_identifier)
    parser.add_argument("--batch-size", type=_batch_size, default=100)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION}",
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
            retry_failed=arguments.retry_failed,
            force=arguments.force,
        )
    except (OSError, sqlite3.DatabaseError, SpreadsheetReindexError):
        print("Spreadsheet reindexing could not access the configured data.", file=sys.stderr)
        return 3
    print(json.dumps(asdict(summary), sort_keys=True))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
