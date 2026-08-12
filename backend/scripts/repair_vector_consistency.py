"""Safely reindex current vectors and verify SQLite/Qdrant consistency."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import logging

from app.database import get_connection, initialize_database
from app.services.vector_store import get_vector_store
from scripts.check_vector_consistency import check_consistency
from scripts.migrate_vectors_to_qdrant import migrate_vectors


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairReport:
    """Machine-readable repair outcome containing counts and IDs, never content."""

    applied: bool
    organization_id: str | None
    pre_consistent: bool
    post_consistent: bool
    planned_active_chunks: int
    reindexed_points: int
    verified_points: int
    remaining_discrepancy_counts: dict[str, int]
    orphan_actions: list[dict[str, object]] = field(default_factory=list)
    deleted_orphan_point_ids: list[str] = field(default_factory=list)


def _discrepancy_counts(report: dict[str, object]) -> dict[str, int]:
    """Summarize named discrepancy lists without duplicating their identifier payloads."""
    discrepancies = report.get("discrepancies")
    if not isinstance(discrepancies, dict):
        return {}
    return {
        str(name): len(items)
        for name, items in discrepancies.items()
        if isinstance(items, list)
    }


def _confirmed_orphan_actions(
    report: dict[str, object],
    organization_id: str | None,
) -> list[dict[str, object]]:
    """Return only active checker orphans with no SQLite identity or active ingestion."""
    discrepancies = report.get("discrepancies")
    candidates = (
        discrepancies.get("vectors_without_valid_current_sqlite_chunk", [])
        if isinstance(discrepancies, dict)
        else []
    )
    actions: list[dict[str, object]] = []
    with get_connection() as connection:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            point_id = str(candidate.get("vector_point_id") or "")
            chunk_id = candidate.get("chunk_id")
            document_id = candidate.get("document_id")
            version_id = candidate.get("version_id")
            chunk_index = candidate.get("chunk_index")
            point_organization_id = str(candidate.get("organization_id") or organization_id or "")
            if not (
                point_id
                and isinstance(chunk_id, int)
                and isinstance(document_id, int)
                and isinstance(version_id, int)
                and isinstance(chunk_index, int)
                and point_organization_id
            ):
                continue
            chunk_exists = connection.execute(
                "SELECT 1 FROM chunks WHERE id = ? AND organization_id = ?",
                (chunk_id, point_organization_id),
            ).fetchone()
            active_equivalent = connection.execute(
                """SELECT 1
                   FROM chunks c
                   JOIN documents d ON d.id = c.document_id
                   JOIN document_versions dv ON dv.id = c.version_id
                   WHERE c.organization_id = ? AND c.document_id = ?
                     AND c.version_id = ? AND c.chunk_index = ?
                     AND c.deleted_at IS NULL AND d.deleted_at IS NULL
                     AND d.current_version_id = c.version_id
                     AND d.processing_status = 'completed'
                     AND dv.deleted_at IS NULL AND dv.status = 'completed'""",
                (point_organization_id, document_id, version_id, chunk_index),
            ).fetchone()
            active_ingestion = connection.execute(
                """SELECT 1 FROM ingestion_jobs
                   WHERE organization_id = ? AND document_id = ? AND version_id = ?
                     AND status IN ('queued', 'processing', 'retry_scheduled')""",
                (point_organization_id, document_id, version_id),
            ).fetchone()
            if chunk_exists or active_equivalent or active_ingestion:
                continue
            actions.append({
                "vector_point_id": point_id,
                "chunk_id": chunk_id,
                "document_id": document_id,
                "version_id": version_id,
                "reason": "active_point_has_no_sqlite_chunk_or_current_equivalent",
                "action": "delete_qdrant_point",
            })
    return sorted(actions, key=lambda item: str(item["vector_point_id"]))


def repair_vectors(
    *,
    apply: bool = False,
    confirmed: bool = False,
    organization_id: str | None = None,
    upsert_batch_size: int = 256,
    smoke_query_limit: int = 3,
    repair_orphans: bool = False,
) -> RepairReport:
    """Reindex active current chunks only after explicit confirmation, then reconcile."""
    if apply and not confirmed:
        raise ValueError("Vector repair requires explicit --confirm-repair confirmation.")

    before = check_consistency(organization_id)
    orphan_actions = _confirmed_orphan_actions(before, organization_id) if repair_orphans else []
    if repair_orphans:
        if not apply:
            return RepairReport(
                applied=False,
                organization_id=organization_id,
                pre_consistent=bool(before.get("consistent")),
                post_consistent=bool(before.get("consistent")),
                planned_active_chunks=0,
                reindexed_points=0,
                verified_points=0,
                remaining_discrepancy_counts=_discrepancy_counts(before),
                orphan_actions=orphan_actions,
            )
        deleted_ids = [str(action["vector_point_id"]) for action in orphan_actions]
        deleted = get_vector_store().delete_points(deleted_ids) if deleted_ids else 0
        after = check_consistency(organization_id)
        return RepairReport(
            applied=True,
            organization_id=organization_id,
            pre_consistent=bool(before.get("consistent")),
            post_consistent=bool(after.get("consistent")),
            planned_active_chunks=0,
            reindexed_points=0,
            verified_points=0,
            remaining_discrepancy_counts=_discrepancy_counts(after),
            orphan_actions=orphan_actions,
            deleted_orphan_point_ids=deleted_ids if deleted == len(deleted_ids) else [],
        )
    plan = migrate_vectors(
        apply=False,
        organization_id=organization_id,
        upsert_batch_size=upsert_batch_size,
        smoke_query_limit=smoke_query_limit,
    )
    planned = plan.active_chunks
    if not apply:
        LOGGER.info(
            "vector repair dry-run organization_id=%s active_chunks=%s consistent=%s",
            organization_id,
            planned,
            before.get("consistent"),
        )
        return RepairReport(
            applied=False,
            organization_id=organization_id,
            pre_consistent=bool(before.get("consistent")),
            post_consistent=bool(before.get("consistent")),
            planned_active_chunks=planned,
            reindexed_points=0,
            verified_points=0,
            remaining_discrepancy_counts=_discrepancy_counts(before),
        )

    # migrate_vectors uses stable IDs, current-version filtering, and batch verification.
    migration = migrate_vectors(
        apply=True,
        organization_id=organization_id,
        upsert_batch_size=upsert_batch_size,
        smoke_query_limit=smoke_query_limit,
    )
    after = check_consistency(organization_id)
    remaining = _discrepancy_counts(after)
    LOGGER.info(
        "vector repair complete organization_id=%s reindexed_points=%s verified_points=%s consistent=%s",
        organization_id,
        migration.upserted_points,
        migration.verified_points,
        after.get("consistent"),
    )
    return RepairReport(
        applied=True,
        organization_id=organization_id,
        pre_consistent=bool(before.get("consistent")),
        post_consistent=bool(after.get("consistent")),
        planned_active_chunks=planned,
        reindexed_points=migration.upserted_points,
        verified_points=migration.verified_points,
        remaining_discrepancy_counts=remaining,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run by default: repair current vectors and verify consistency."
    )
    parser.add_argument("--apply", action="store_true", help="Permit reindex writes.")
    parser.add_argument(
        "--confirm-repair",
        action="store_true",
        help="Explicitly confirm the retrieval-affecting repair operation.",
    )
    parser.add_argument("--organization-id")
    parser.add_argument(
        "--repair-orphans",
        action="store_true",
        help="Target only checker-confirmed orphan point IDs; dry-run unless --apply is also set.",
    )
    parser.add_argument("--upsert-batch-size", type=int, default=256)
    parser.add_argument("--smoke-query-limit", type=int, default=3)
    arguments = parser.parse_args()

    initialize_database()
    report = repair_vectors(
        apply=arguments.apply,
        confirmed=arguments.confirm_repair,
        organization_id=arguments.organization_id,
        upsert_batch_size=arguments.upsert_batch_size,
        smoke_query_limit=arguments.smoke_query_limit,
        repair_orphans=arguments.repair_orphans,
    )
    print(json.dumps(asdict(report), sort_keys=True))
    if arguments.apply and not report.post_consistent:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
