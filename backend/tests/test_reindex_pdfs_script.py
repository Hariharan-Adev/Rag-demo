"""Tests for durable PDF reindex job creation."""

import unittest
from unittest.mock import patch

from scripts import reindex_pdfs


class PdfReindexScriptTests(unittest.TestCase):
    def test_run_enqueues_versioned_idempotent_jobs(self) -> None:
        candidate = reindex_pdfs.Candidate(
            document_id=54,
            version_id=35,
            owner_id=1,
            organization_id="org-a",
            storage_key="org-a/inspection.pdf",
        )
        with patch.object(
            reindex_pdfs,
            "_candidate_batch",
            side_effect=[[candidate], []],
        ), patch.object(reindex_pdfs, "enqueue_job", return_value="job-1") as enqueue:
            summary = reindex_pdfs.run_reindex(
                dry_run=False,
                document_id=None,
                owner_id=None,
                organization_id=None,
                batch_size=100,
            )

        self.assertEqual(summary.enqueued, 1)
        self.assertEqual(
            enqueue.call_args.kwargs["pipeline_version"],
            reindex_pdfs.PDF_INDEX_VERSION,
        )
        self.assertTrue(enqueue.call_args.kwargs["allow_active_content_reuse"])
        self.assertTrue(enqueue.call_args.kwargs["force_reprocess"])
        self.assertIn("54:35", enqueue.call_args.kwargs["idempotency_key"])

    def test_dry_run_never_writes(self) -> None:
        candidate = reindex_pdfs.Candidate(54, 35, 1, "org-a", "inspection.pdf")
        with patch.object(
            reindex_pdfs,
            "_candidate_batch",
            side_effect=[[candidate], []],
        ), patch.object(reindex_pdfs, "enqueue_job") as enqueue:
            summary = reindex_pdfs.run_reindex(
                dry_run=True,
                document_id=None,
                owner_id=None,
                organization_id=None,
                batch_size=100,
            )

        enqueue.assert_not_called()
        self.assertEqual(summary.eligible, 1)
        self.assertEqual(summary.enqueued, 0)


if __name__ == "__main__":
    unittest.main()
