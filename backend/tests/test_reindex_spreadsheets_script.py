"""CLI orchestration tests for spreadsheet row and vector reindexing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.structured_ingestion import (
    SpreadsheetReindexError,
    SpreadsheetReindexResult,
)
from app.services.workbooks import STRUCTURED_INDEX_VERSION
from scripts import reindex_spreadsheets as script


class SpreadsheetReindexScriptTests(unittest.TestCase):
    @staticmethod
    def candidate(
        document_id: int,
        *,
        status: str = "pending",
        version: str | None = None,
    ) -> script.Candidate:
        return script.Candidate(
            document_id=document_id,
            owner_id=10,
            organization_id="org-a",
            status=status,
            indexed_version=version,
        )

    def test_dry_run_never_calls_mutating_service(self):
        candidates = [
            self.candidate(1),
            self.candidate(
                2,
                status="completed",
                version=STRUCTURED_INDEX_VERSION,
            ),
        ]
        with patch.object(
            script,
            "_candidate_batch",
            side_effect=[candidates, []],
        ), patch.object(
            script,
            "reindex_existing_spreadsheet_document",
        ) as reindex:
            summary = script.run_reindex(
                dry_run=True,
                document_id=None,
                owner_id=None,
                organization_id=None,
                batch_size=100,
                retry_failed=False,
                force=False,
            )

        reindex.assert_not_called()
        self.assertEqual((summary.scanned, summary.eligible), (2, 1))
        self.assertEqual(summary.skipped, 1)

    def test_failure_isolated_and_later_document_completes(self):
        candidates = [self.candidate(1), self.candidate(2)]
        completed = SpreadsheetReindexResult(
            document_id=2,
            content_id=20,
            status="completed",
            row_count=3,
            chunks_reindexed=4,
        )
        with patch.object(
            script,
            "_candidate_batch",
            side_effect=[candidates, []],
        ), patch.object(
            script,
            "reindex_existing_spreadsheet_document",
            side_effect=[SpreadsheetReindexError("safe failure"), completed],
        ):
            summary = script.run_reindex(
                dry_run=False,
                document_id=None,
                owner_id=None,
                organization_id=None,
                batch_size=100,
                retry_failed=False,
                force=False,
            )

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.completed, 1)
        self.assertEqual(summary.rows_indexed, 3)
        self.assertEqual(summary.chunks_reindexed, 4)


if __name__ == "__main__":
    unittest.main()
