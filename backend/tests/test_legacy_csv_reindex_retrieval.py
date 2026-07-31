"""End-to-end structured retrieval after reindexing one legacy CSV."""

from __future__ import annotations

from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import database
from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services import rag_service, vector_store
from app.services.storage import storage_key_for, write_storage_bytes
from app.services.structured_ingestion import reindex_existing_csv_document
from app.services.workbook_analysis import has_structured_workbook


RANGE_QUESTION = "Show all equipment priced between ₹50,000 and ₹200,000."


class LegacyCsvReindexRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", root / "retrieval.db")
        )
        self.stack.enter_context(
            patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
        )
        database.initialize_database()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                [("org-a", "A"), ("org-b", "B")],
            )
            connection.executemany(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (?, ?, 'hash', ?, 'member')""",
                [
                    (10, "owner@example.com", "org-a"),
                    (11, "other-owner@example.com", "org-a"),
                    (20, "other-org@example.com", "org-b"),
                ],
            )
        self.document_id, self.content_id, self.sheet_name = (
            self._insert_legacy_csv()
        )

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _insert_legacy_csv(self) -> tuple[int, int, str]:
        content = (
            b"Equipment,Price,Category\n"
            b"Seed Drill,45000,Sowing\n"
            b"Boundary Sprayer,50000,Spraying\n"
            b"Power Tiller,125000,Soil Preparation\n"
            b"Irrigation Pump,75000,Irrigation\n"
            b"Harvester,200000,Harvesting\n"
            b"Agricultural Drone,210000,Monitoring\n"
            b"Tractor,550000,Heavy\n"
        )
        stored_filename = "legacy-equipment-stored.csv"
        storage_key = storage_key_for("org-a", stored_filename)
        write_storage_bytes(storage_key, content)
        file_hash = sha256(content).hexdigest()
        with database.get_connection() as connection:
            content_cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text, processing_status)
                   VALUES (10, 'org-a', ?, 'legacy-range-content',
                           'legacy extracted text', 'completed')""",
                (file_hash,),
            )
            content_id = int(content_cursor.lastrowid)
            document_cursor = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    visibility, processing_status)
                   VALUES (10, 'org-a', 'legacy-equipment.csv',
                           'legacy-equipment.csv', ?, ?, ?, 'private',
                           'completed')""",
                (stored_filename, file_hash, content_id),
            )
            document_id = int(document_cursor.lastrowid)
            version_cursor = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, mime_type, file_size,
                    file_hash, status, ingestion_status, extraction_status,
                    indexing_status, created_by)
                   VALUES ('org-a', ?, 1, ?, ?, ?, 'text/csv', ?, ?,
                           'completed', 'completed', 'completed', 'completed',
                           10)""",
                (
                    document_id,
                    content_id,
                    stored_filename,
                    storage_key,
                    len(content),
                    file_hash,
                ),
            )
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (int(version_cursor.lastrowid), document_id),
            )
        return document_id, content_id, Path(stored_filename).stem

    def _answer_without_semantic_results(
        self,
        question: str,
        *,
        user_id: int,
    ) -> dict[str, object]:
        with patch.object(
            rag_service,
            "search_chunks",
            return_value=[],
        ), patch.object(
            rag_service,
            "generate_answer",
            side_effect=AssertionError("LLM must not run without context"),
        ), patch.object(rag_service, "log_audit_event"):
            return rag_service.answer_question(
                question,
                user_id=user_id,
                document_id=self.document_id,
            )

    def test_legacy_csv_becomes_deterministically_queryable_after_reindex(
        self,
    ) -> None:
        with database.get_connection() as connection:
            structured_before = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM workbook_sheets
                      WHERE content_id = ?) AS sheets,
                     (SELECT COUNT(*) FROM workbook_rows
                      WHERE content_id = ?) AS rows""",
                (self.content_id, self.content_id),
            ).fetchone()
        self.assertEqual(tuple(structured_before), (0, 0))
        self.assertFalse(
            has_structured_workbook(10, document_id=self.document_id)
        )

        before = self._answer_without_semantic_results(
            RANGE_QUESTION,
            user_id=10,
        )
        self.assertEqual(before["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(before["sources"], [])
        self.assertFalse(before["grounded"])

        with patch.object(
            vector_store,
            "get_vector_store",
            side_effect=AssertionError("structured reindex accessed Qdrant"),
        ) as get_store:
            reindexed = reindex_existing_csv_document(
                document_id=self.document_id,
                owner_id=10,
                organization_id="org-a",
            )
        get_store.assert_not_called()
        self.assertEqual(reindexed.status, "completed")
        self.assertEqual(reindexed.row_count, 7)
        self.assertTrue(
            has_structured_workbook(10, document_id=self.document_id)
        )

        with patch.object(
            rag_service,
            "search_chunks",
            side_effect=AssertionError(
                "structured range must not use semantic retrieval"
            ),
        ), patch.object(
            rag_service,
            "generate_answer",
            side_effect=AssertionError("structured range must not use the LLM"),
        ), patch.object(rag_service, "log_audit_event"):
            first = rag_service.answer_question(
                RANGE_QUESTION,
                user_id=10,
                document_id=self.document_id,
            )
            second = rag_service.answer_question(
                RANGE_QUESTION,
                user_id=10,
                document_id=self.document_id,
            )

        self.assertEqual(first, second)
        answer = str(first["answer"])
        self.assertIn("Matching records (4):", answer)
        matches = [
            "Boundary Sprayer",
            "Power Tiller",
            "Irrigation Pump",
            "Harvester",
        ]
        excluded = [
            "Seed Drill",
            "Agricultural Drone",
            "Tractor",
        ]
        for equipment in matches:
            self.assertIn(equipment, answer)
        for equipment in excluded:
            self.assertNotIn(equipment, answer)
        self.assertEqual(
            [answer.index(equipment) for equipment in matches],
            sorted(answer.index(equipment) for equipment in matches),
        )
        self.assertTrue(first["grounded"])
        self.assertEqual(len(first["sources"]), 1)
        source = first["sources"][0]
        self.assertEqual(source["document_id"], self.document_id)
        self.assertEqual(source["filename"], "legacy-equipment.csv")
        self.assertEqual(source["source_type"], "csv")
        self.assertEqual(
            source["source_location"],
            {
                "sheet_name": "CSV",
                "row_start": 3,
                "row_end": 6,
            },
        )

        unavailable = self._answer_without_semantic_results(
            "What is the warranty period?",
            user_id=10,
        )
        self.assertEqual(unavailable["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(
            unavailable["answer"],
            "The requested information is not available in the uploaded documents.",
        )
        self.assertEqual(unavailable["sources"], [])
        self.assertFalse(unavailable["grounded"])

        for isolated_user in (11, 20):
            self.assertFalse(
                has_structured_workbook(
                    isolated_user,
                    document_id=self.document_id,
                )
            )
            isolated = self._answer_without_semantic_results(
                RANGE_QUESTION,
                user_id=isolated_user,
            )
            self.assertEqual(isolated["answer"], UNAVAILABLE_ANSWER)
            self.assertEqual(isolated["sources"], [])
            self.assertFalse(isolated["grounded"])


if __name__ == "__main__":
    unittest.main()
