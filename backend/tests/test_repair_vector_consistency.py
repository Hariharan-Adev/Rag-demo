"""Tests for the explicit-confirmation vector repair command."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts import repair_vector_consistency as repair
from scripts.migrate_vectors_to_qdrant import MigrationReport


def _consistency(*, consistent: bool, missing: int = 0) -> dict[str, object]:
    """Create a content-free checker response for repair-command tests."""
    return {
        "consistent": consistent,
        "sqlite_indexed_chunks": 4,
        "discrepancies": {
            "sqlite_chunks_without_vector": [{}] * missing,
            "sqlite_chunks_missing_active_vector": [],
            "vectors_without_valid_current_sqlite_chunk": [],
            "deleted_document_vectors_marked_active": [],
            "wrong_document_version_vectors": [],
            "duplicate_active_vector_ids": [],
        },
    }


def _orphan_consistency(*, consistent: bool) -> dict[str, object]:
    """Create one checker-confirmed orphan without including document contents."""
    report = _consistency(consistent=consistent)
    report["discrepancies"]["vectors_without_valid_current_sqlite_chunk"] = [{
        "vector_point_id": "orphan-point",
        "chunk_id": 91,
        "chunk_index": 0,
        "document_id": 9,
        "version_id": 3,
        "organization_id": "org-a",
    }]
    return report


class RepairVectorConsistencyTests(unittest.TestCase):
    def test_dry_run_checks_consistency_without_reindexing(self) -> None:
        """The default command path is read-only and returns planned counts."""
        plan = MigrationReport(4, 4, 0, 0, 0, 0, 1, False)
        with patch.object(repair, "check_consistency", return_value=_consistency(consistent=False, missing=2)), patch.object(repair, "migrate_vectors", return_value=plan) as migrate:
            report = repair.repair_vectors()

        migrate.assert_called_once_with(
            apply=False,
            organization_id=None,
            upsert_batch_size=256,
            smoke_query_limit=3,
        )
        self.assertFalse(report.applied)
        self.assertEqual(report.planned_active_chunks, 4)
        self.assertEqual(report.remaining_discrepancy_counts["sqlite_chunks_without_vector"], 2)

    def test_apply_requires_explicit_confirmation(self) -> None:
        """No retrieval-affecting writes occur from --apply alone."""
        with patch.object(repair, "check_consistency") as check, patch.object(repair, "migrate_vectors") as migrate:
            with self.assertRaisesRegex(ValueError, "confirm-repair"):
                repair.repair_vectors(apply=True)

        check.assert_not_called()
        migrate.assert_not_called()

    def test_confirmed_repair_reindexes_then_verifies_final_consistency(self) -> None:
        """A confirmed run delegates stable-ID reindexing and returns the final check."""
        migration = MigrationReport(
            active_chunks=4,
            reused_sqlite_vectors=4,
            regenerated_vectors=0,
            upserted_points=4,
            verified_points=4,
            smoke_queries=1,
            organizations=1,
            applied=True,
        )
        plan = MigrationReport(4, 4, 0, 0, 0, 0, 1, False)
        with patch.object(repair, "check_consistency", side_effect=[_consistency(consistent=False, missing=1), _consistency(consistent=True)]) as check, patch.object(repair, "migrate_vectors", side_effect=[plan, migration]) as migrate:
            report = repair.repair_vectors(apply=True, confirmed=True, organization_id="org-a")

        self.assertEqual(migrate.call_args_list[0].kwargs["apply"], False)
        self.assertEqual(migrate.call_args_list[1].kwargs["apply"], True)
        self.assertEqual(check.call_count, 2)
        self.assertTrue(report.applied)
        self.assertFalse(report.pre_consistent)
        self.assertTrue(report.post_consistent)
        self.assertEqual((report.reindexed_points, report.verified_points), (4, 4))
        self.assertTrue(all(count == 0 for count in report.remaining_discrepancy_counts.values()))

    def test_orphan_dry_run_never_deletes_or_reindexes(self) -> None:
        """Explicit orphan dry-runs describe only the IDs that would be deleted."""
        actions = [{
            "vector_point_id": "orphan-point",
            "chunk_id": 91,
            "document_id": 9,
            "version_id": 3,
            "reason": "active_point_has_no_sqlite_chunk_or_current_equivalent",
            "action": "delete_qdrant_point",
        }]
        with (
            patch.object(repair, "check_consistency", return_value=_orphan_consistency(consistent=False)),
            patch.object(repair, "_confirmed_orphan_actions", return_value=actions),
            patch.object(repair, "get_vector_store") as store,
            patch.object(repair, "migrate_vectors") as migrate,
        ):
            report = repair.repair_vectors(repair_orphans=True)

        store.assert_not_called()
        migrate.assert_not_called()
        self.assertFalse(report.applied)
        self.assertEqual(report.orphan_actions, actions)

    def test_confirmed_orphan_repair_deletes_only_checker_actions(self) -> None:
        """A confirmed orphan repair sends exactly the qualified point IDs to Qdrant."""
        actions = [{
            "vector_point_id": "orphan-point",
            "chunk_id": 91,
            "document_id": 9,
            "version_id": 3,
            "reason": "active_point_has_no_sqlite_chunk_or_current_equivalent",
            "action": "delete_qdrant_point",
        }]
        store = MagicMock()
        store.delete_points.return_value = 1
        with (
            patch.object(repair, "check_consistency", side_effect=[
                _orphan_consistency(consistent=False), _consistency(consistent=True),
            ]),
            patch.object(repair, "_confirmed_orphan_actions", return_value=actions),
            patch.object(repair, "get_vector_store", return_value=store),
            patch.object(repair, "migrate_vectors") as migrate,
        ):
            report = repair.repair_vectors(
                apply=True, confirmed=True, repair_orphans=True, organization_id="org-a"
            )

        store.delete_points.assert_called_once_with(["orphan-point"])
        migrate.assert_not_called()
        self.assertEqual(report.deleted_orphan_point_ids, ["orphan-point"])
        self.assertTrue(report.post_consistent)

    def test_repeated_orphan_repair_is_safe_when_checker_finds_none(self) -> None:
        """A second run leaves valid/current vectors untouched after the orphan is gone."""
        store = MagicMock()
        with (
            patch.object(repair, "check_consistency", return_value=_consistency(consistent=True)),
            patch.object(repair, "_confirmed_orphan_actions", return_value=[]),
            patch.object(repair, "get_vector_store", return_value=store),
        ):
            report = repair.repair_vectors(
                apply=True, confirmed=True, repair_orphans=True, organization_id="org-a"
            )

        store.delete_points.assert_not_called()
        self.assertEqual(report.deleted_orphan_point_ids, [])
        self.assertTrue(report.post_consistent)

if __name__ == "__main__":
    unittest.main()
