"""CLI orchestration tests for structured CSV reindexing."""

from __future__ import annotations

from contextlib import closing, ExitStack, redirect_stderr
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import database
from app.routes import documents as document_routes
from app.services import vector_store
from app.services.storage import storage_key_for, write_storage_bytes
from app.services.structured_ingestion import (
    StructuredCsvReindexError,
    StructuredCsvReindexResult,
)
from app.services.workbooks import STRUCTURED_INDEX_VERSION
from scripts import reindex_structured_csv as script


class StructuredCsvReindexScriptTests(unittest.TestCase):
    @staticmethod
    def _candidate(
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

    def test_dry_run_selects_candidates_without_calling_service(self) -> None:
        candidates = [
            self._candidate(1),
            self._candidate(
                2,
                status="completed",
                version=STRUCTURED_INDEX_VERSION,
            ),
        ]
        with patch.object(
            script,
            "_candidate_batch",
            side_effect=[candidates, []],
        ), patch.object(script, "reindex_existing_csv_document") as reindex:
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
        self.assertEqual(summary.scanned, 2)
        self.assertEqual(summary.eligible, 1)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.completed, 0)
        self.assertEqual(summary.failed, 0)

    def test_dry_run_connection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readonly.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE marker (value INTEGER)")
                connection.execute("INSERT INTO marker VALUES (1)")
                connection.commit()
            with patch.object(database, "DATABASE_PATH", path):
                with script._read_connection(dry_run=True) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA query_only").fetchone()[0],
                        1,
                    )
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("UPDATE marker SET value = 2")
            with closing(sqlite3.connect(path)) as connection:
                value = connection.execute(
                    "SELECT value FROM marker"
                ).fetchone()[0]
            self.assertEqual(value, 1)

    def test_failure_does_not_stop_later_documents(self) -> None:
        candidates = [self._candidate(1), self._candidate(2)]
        completed = StructuredCsvReindexResult(
            document_id=2,
            content_id=20,
            status="completed",
            row_count=3,
        )
        with patch.object(
            script,
            "_candidate_batch",
            side_effect=[candidates, []],
        ), patch.object(
            script,
            "reindex_existing_csv_document",
            side_effect=[
                StructuredCsvReindexError("safe failure"),
                completed,
            ],
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
        self.assertEqual(summary.eligible, 2)
        self.assertEqual(summary.completed, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.rows_indexed, 3)

    def test_retry_and_force_eligibility(self) -> None:
        failed = self._candidate(1, status="failed")
        processing = self._candidate(2, status="processing")
        current = self._candidate(
            3,
            status="completed",
            version=STRUCTURED_INDEX_VERSION,
        )
        old = self._candidate(4, status="completed", version="old")

        self.assertFalse(
            script._is_eligible(failed, retry_failed=False, force=False)
        )
        self.assertTrue(
            script._is_eligible(failed, retry_failed=True, force=False)
        )
        self.assertFalse(
            script._is_eligible(processing, retry_failed=True, force=False)
        )
        self.assertFalse(
            script._is_eligible(current, retry_failed=True, force=False)
        )
        self.assertTrue(
            script._is_eligible(old, retry_failed=False, force=False)
        )
        self.assertTrue(
            script._is_eligible(current, retry_failed=False, force=True)
        )


class StructuredCsvDryRunIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", root / "dry-run.db")
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
                    (10, "owner-a@example.com", "org-a"),
                    (11, "owner-b@example.com", "org-a"),
                    (20, "owner-c@example.com", "org-b"),
                ],
            )
        self.pending_id = self._insert_document(owner_id=10, organization_id="org-a")
        self.current_id = self._insert_document(
            owner_id=10,
            organization_id="org-a",
            status="completed",
            version=STRUCTURED_INDEX_VERSION,
        )
        self.deleted_id = self._insert_document(
            owner_id=10,
            organization_id="org-a",
            deleted=True,
        )
        self.owner_11_id = self._insert_document(
            owner_id=11,
            organization_id="org-a",
        )
        self.org_b_id = self._insert_document(
            owner_id=20,
            organization_id="org-b",
        )
        self._insert_document(
            owner_id=10,
            organization_id="org-a",
            extension=".txt",
        )

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _insert_document(
        self,
        *,
        owner_id: int,
        organization_id: str,
        status: str = "pending",
        version: str | None = None,
        deleted: bool = False,
        extension: str = ".csv",
    ) -> int:
        with database.get_connection() as connection:
            discriminator = connection.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM document_contents"
            ).fetchone()[0]
            content_cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text,
                    processing_status, structured_index_status,
                    structured_index_version)
                   VALUES (?, ?, ?, ?, '', 'completed', ?, ?)""",
                (
                    owner_id,
                    organization_id,
                    f"hash-{discriminator}",
                    f"normalized-{discriminator}",
                    status,
                    version,
                ),
            )
            content_id = int(content_cursor.lastrowid)
            stored_filename = f"stored-{content_id}{extension}"
            document_cursor = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    visibility, processing_status, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'private', 'completed',
                           CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)""",
                (
                    owner_id,
                    organization_id,
                    f"source-{content_id}{extension}",
                    f"source-{content_id}{extension}",
                    stored_filename,
                    f"hash-{discriminator}",
                    content_id,
                    int(deleted),
                ),
            )
            document_id = int(document_cursor.lastrowid)
            version_cursor = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, mime_type, file_size,
                    file_hash, status, ingestion_status, extraction_status,
                    indexing_status, created_by)
                   VALUES (?, ?, 1, ?, ?, ?, ?, 1, ?, 'completed',
                           'completed', 'completed', 'completed', ?)""",
                (
                    organization_id,
                    document_id,
                    content_id,
                    stored_filename,
                    f"tenant/{stored_filename}",
                    "text/csv" if extension == ".csv" else "text/plain",
                    f"hash-{discriminator}",
                    owner_id,
                ),
            )
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (int(version_cursor.lastrowid), document_id),
            )
        return document_id

    @staticmethod
    def _run(**overrides) -> script.ReindexSummary:
        arguments = {
            "dry_run": True,
            "document_id": None,
            "owner_id": None,
            "organization_id": None,
            "batch_size": 2,
            "retry_failed": False,
            "force": False,
        }
        arguments.update(overrides)
        return script.run_reindex(**arguments)

    @staticmethod
    def _database_snapshot() -> tuple[list[tuple], int, int, int]:
        with database.get_connection() as connection:
            statuses = [
                tuple(row)
                for row in connection.execute(
                    """SELECT id, structured_index_status,
                              structured_index_version,
                              structured_indexed_at,
                              structured_index_error
                       FROM document_contents ORDER BY id"""
                ).fetchall()
            ]
            sheets = connection.execute(
                "SELECT COUNT(*) FROM workbook_sheets"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT COUNT(*) FROM workbook_rows"
            ).fetchone()[0]
            cells_table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'table' AND name = 'workbook_cells'"""
            ).fetchone()
            cells = (
                connection.execute(
                    "SELECT COUNT(*) FROM workbook_cells"
                ).fetchone()[0]
                if cells_table is not None
                else 0
            )
        return statuses, sheets, rows, cells

    def test_dry_run_is_read_only_and_counts_active_csvs(self) -> None:
        before = self._database_snapshot()
        qdrant_points = {"existing-point"}
        qdrant_mutations: list[str] = []

        class GuardedVectorStore:
            def upsert(self, _points) -> None:
                qdrant_mutations.append("upsert")

            def upsert_chunks(self, _points) -> None:
                qdrant_mutations.append("upsert_chunks")

            def set_document_deleted(self, *_args, **_kwargs) -> None:
                qdrant_mutations.append("set_document_deleted")

            def delete_document(self, *_args, **_kwargs) -> None:
                qdrant_mutations.append("delete_document")

            def delete_document_version(self, *_args, **_kwargs) -> None:
                qdrant_mutations.append("delete_document_version")

        guarded_store = GuardedVectorStore()
        with patch.object(
            script,
            "reindex_existing_csv_document",
            side_effect=AssertionError("dry-run called the write service"),
        ) as reindex, patch.object(
            vector_store,
            "get_vector_store",
            return_value=guarded_store,
        ) as get_store:
            summary = self._run()

        self.assertEqual(summary.scanned, 4)
        self.assertEqual(summary.eligible, 3)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.completed, 0)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.rows_indexed, 0)
        self.assertEqual(self._database_snapshot(), before)
        self.assertEqual(qdrant_points, {"existing-point"})
        self.assertEqual(qdrant_mutations, [])
        reindex.assert_not_called()
        get_store.assert_not_called()

    def test_owner_and_organization_filters_are_isolated(self) -> None:
        owner = self._run(owner_id=10, organization_id="org-a")
        other_owner = self._run(owner_id=11, organization_id="org-a")
        other_organization = self._run(organization_id="org-b")
        mismatched_tenant = self._run(owner_id=20, organization_id="org-a")

        self.assertEqual(
            (owner.scanned, owner.eligible, owner.skipped),
            (2, 1, 1),
        )
        self.assertEqual(
            (other_owner.scanned, other_owner.eligible, other_owner.skipped),
            (1, 1, 0),
        )
        self.assertEqual(
            (
                other_organization.scanned,
                other_organization.eligible,
                other_organization.skipped,
            ),
            (1, 1, 0),
        )
        self.assertEqual(
            (
                mismatched_tenant.scanned,
                mismatched_tenant.eligible,
                mismatched_tenant.skipped,
            ),
            (0, 0, 0),
        )

    def test_invalid_identifiers_exit_before_database_or_service_access(self) -> None:
        invalid_arguments = [
            ["--dry-run", "--document-id", "0"],
            ["--dry-run", "--owner-id", "-1"],
            ["--dry-run", "--organization-id", "../org-a"],
            ["--dry-run", "--batch-size", "0"],
            ["--dry-run", "--batch-size", "1001"],
        ]
        with patch.object(script, "_read_connection") as read, patch.object(
            script,
            "reindex_existing_csv_document",
        ) as reindex, redirect_stderr(StringIO()):
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(SystemExit, "2"):
                        script.main(arguments)
        read.assert_not_called()
        reindex.assert_not_called()


class StructuredCsvReindexLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", root / "lifecycle.db")
        )
        self.stack.enter_context(
            patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
        )
        database.initialize_database()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES ('org-a', 'A')"
            )
            connection.execute(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (10, 'owner@example.com', 'hash', 'org-a', 'member')"""
            )
        self.document_id, self.content_id = self._insert_legacy_csv()

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _insert_legacy_csv(self) -> tuple[int, int]:
        content = b"Equipment,Price\nPump,75000\nTractor,550000\n"
        stored_filename = "legacy-structured.csv"
        storage_key = storage_key_for("org-a", stored_filename)
        write_storage_bytes(storage_key, content)
        file_hash = sha256(content).hexdigest()
        with database.get_connection() as connection:
            content_cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text, processing_status)
                   VALUES (10, 'org-a', ?, 'legacy-normalized',
                           'legacy text', 'completed')""",
                (file_hash,),
            )
            content_id = int(content_cursor.lastrowid)
            document_cursor = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    visibility, processing_status)
                   VALUES (10, 'org-a', 'legacy.csv', 'legacy.csv', ?, ?, ?,
                           'private', 'completed')""",
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
        return document_id, content_id

    def _run(self, **overrides) -> script.ReindexSummary:
        arguments = {
            "dry_run": False,
            "document_id": self.document_id,
            "owner_id": 10,
            "organization_id": "org-a",
            "batch_size": 10,
            "retry_failed": False,
            "force": False,
        }
        arguments.update(overrides)
        return script.run_reindex(**arguments)

    def _structured_counts(self) -> tuple[int, int, int, int]:
        with database.get_connection() as connection:
            workbook_count = connection.execute(
                """SELECT COUNT(DISTINCT content_id)
                   FROM workbook_sheets WHERE content_id = ?""",
                (self.content_id,),
            ).fetchone()[0]
            sheet_count = connection.execute(
                "SELECT COUNT(*) FROM workbook_sheets WHERE content_id = ?",
                (self.content_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """SELECT values_json FROM workbook_rows
                   WHERE content_id = ? ORDER BY row_number""",
                (self.content_id,),
            ).fetchall()
        # Cells are represented inside each row's values_json in this schema.
        cell_count = sum(len(json.loads(row["values_json"])) for row in rows)
        return workbook_count, sheet_count, len(rows), cell_count

    def _structured_status(self) -> tuple[str, str | None, str | None]:
        with database.get_connection() as connection:
            row = connection.execute(
                """SELECT structured_index_status, structured_index_version,
                          structured_index_error
                   FROM document_contents WHERE id = ?""",
                (self.content_id,),
            ).fetchone()
        return tuple(row)

    def test_reindex_is_idempotent_and_failed_attempt_is_retryable(self) -> None:
        qdrant_points = {"existing-point"}
        point_counts = [len(qdrant_points)]

        with patch.object(
            vector_store,
            "get_vector_store",
            side_effect=AssertionError("reindex accessed Qdrant"),
        ) as get_store:
            first = self._run()
            baseline = self._structured_counts()
            point_counts.append(len(qdrant_points))

            second = self._run()
            self.assertEqual(self._structured_counts(), baseline)
            point_counts.append(len(qdrant_points))

            forced = self._run(force=True)
            self.assertEqual(self._structured_counts(), baseline)
            point_counts.append(len(qdrant_points))

            with database.get_connection() as connection:
                connection.execute(
                    """UPDATE document_contents
                       SET structured_index_version = 'structured-workbook-v0'
                       WHERE id = ?""",
                    (self.content_id,),
                )
            upgraded = self._run()
            self.assertEqual(self._structured_counts(), baseline)
            point_counts.append(len(qdrant_points))

            with database.get_connection() as connection:
                connection.execute(
                    """CREATE TRIGGER fail_structured_row_insert
                       BEFORE INSERT ON workbook_rows
                       BEGIN
                         SELECT RAISE(ABORT, 'injected structured failure');
                       END"""
                )
            with redirect_stderr(StringIO()):
                failed = self._run(force=True)
            self.assertEqual(self._structured_counts(), baseline)
            failed_status = self._structured_status()
            point_counts.append(len(qdrant_points))

            with database.get_connection() as connection:
                connection.execute("DROP TRIGGER fail_structured_row_insert")
            retried = self._run(retry_failed=True)
            self.assertEqual(self._structured_counts(), baseline)
            point_counts.append(len(qdrant_points))

        self.assertEqual(first.completed, 1)
        self.assertEqual(first.rows_indexed, 2)
        self.assertEqual(baseline, (1, 1, 2, 4))
        self.assertEqual(
            (second.scanned, second.eligible, second.skipped),
            (1, 0, 1),
        )
        self.assertEqual(forced.completed, 1)
        self.assertEqual(upgraded.completed, 1)
        self.assertEqual(
            self._structured_status(),
            ("completed", STRUCTURED_INDEX_VERSION, None),
        )
        self.assertEqual(failed.failed, 1)
        self.assertEqual(failed_status[0], "failed")
        self.assertEqual(
            failed_status[2],
            "Structured CSV reindexing failed.",
        )
        self.assertNotIn("\n", failed_status[2])
        self.assertNotIn("\x00", failed_status[2])
        self.assertLessEqual(
            len(failed_status[2]),
            database.STRUCTURED_INDEX_ERROR_MAX_LENGTH,
        )
        self.assertEqual(retried.completed, 1)
        self.assertEqual(point_counts, [1, 1, 1, 1, 1, 1, 1])
        get_store.assert_not_called()


class SharedContentCsvReindexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(database, "DATABASE_PATH", root / "shared-content.db")
        )
        self.stack.enter_context(
            patch.object(database, "UPLOAD_DIRECTORY", root / "uploads")
        )
        database.initialize_database()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES ('org-a', 'A')"
            )
            connection.execute(
                """INSERT INTO users
                   (id, email, password_hash, organization_id, role)
                   VALUES (10, 'owner@example.com', 'hash', 'org-a', 'member')"""
            )
        self.content = b"Equipment,Price\nPump,75000\nTractor,550000\n"
        self.file_hash = sha256(self.content).hexdigest()
        self.content_id = self._insert_content(deleted=False)
        self.document_ids = [
            self._insert_reference("shared-a.csv", duplicate=False),
            self._insert_reference("shared-b.csv", duplicate=True),
        ]
        self.deleted_content_id = self._insert_content(deleted=True)
        self.deleted_document_id = self._insert_reference(
            "deleted-shared.csv",
            duplicate=True,
            content_id=self.deleted_content_id,
            deleted=True,
        )

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _insert_content(self, *, deleted: bool) -> int:
        with database.get_connection() as connection:
            cursor = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash,
                    normalized_content_hash, extracted_text, processing_status,
                    deleted_at, deleted_by, deleted_with_document)
                   VALUES (10, 'org-a', ?, 'identical-normalized-content',
                           'legacy text', 'completed',
                           CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                           CASE WHEN ? THEN 10 ELSE NULL END, ?)""",
                (
                    self.file_hash,
                    int(deleted),
                    int(deleted),
                    int(deleted),
                ),
            )
        return int(cursor.lastrowid)

    def _insert_reference(
        self,
        filename: str,
        *,
        duplicate: bool,
        content_id: int | None = None,
        deleted: bool = False,
    ) -> int:
        linked_content_id = content_id or self.content_id
        storage_key = storage_key_for("org-a", filename)
        write_storage_bytes(storage_key, self.content)
        with database.get_connection() as connection:
            document_cursor = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename,
                    display_filename, stored_filename, file_hash, content_id,
                    is_duplicate_content, visibility, processing_status,
                    deleted_at, deleted_by)
                   VALUES (10, 'org-a', ?, ?, ?, ?, ?, ?, 'private',
                           'completed',
                           CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                           CASE WHEN ? THEN 10 ELSE NULL END)""",
                (
                    filename,
                    filename,
                    filename,
                    self.file_hash,
                    linked_content_id,
                    int(duplicate),
                    int(deleted),
                    int(deleted),
                ),
            )
            document_id = int(document_cursor.lastrowid)
            version_cursor = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, storage_key, mime_type, file_size,
                    file_hash, status, ingestion_status, extraction_status,
                    indexing_status, created_by, deleted_at, deleted_by,
                    deleted_with_document)
                   VALUES ('org-a', ?, 1, ?, ?, ?, 'text/csv', ?, ?,
                           'completed', 'completed', 'completed', 'completed',
                           10,
                           CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                           CASE WHEN ? THEN 10 ELSE NULL END, ?)""",
                (
                    document_id,
                    linked_content_id,
                    filename,
                    storage_key,
                    len(self.content),
                    self.file_hash,
                    int(deleted),
                    int(deleted),
                    int(deleted),
                ),
            )
            version_id = int(version_cursor.lastrowid)
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (version_id, document_id),
            )
            if not deleted:
                connection.execute(
                    """INSERT INTO chunks
                       (content_id, chunk_index, text, embedding,
                        organization_id, document_id, version_id, source_type,
                        source_location_json, vector_point_id, embedding_model,
                        embedding_dimension, indexing_status)
                       VALUES (?, 0, 'legacy chunk', '[1.0, 0.0]', 'org-a',
                               ?, ?, 'text', '{}', ?, 'legacy-model', 2,
                               'completed')""",
                    (
                        linked_content_id,
                        document_id,
                        version_id,
                        f"existing-point-{document_id}",
                    ),
                )
        return document_id

    def _run(self, **overrides) -> script.ReindexSummary:
        arguments = {
            "dry_run": False,
            "document_id": None,
            "owner_id": 10,
            "organization_id": "org-a",
            "batch_size": 10,
            "retry_failed": False,
            "force": False,
        }
        arguments.update(overrides)
        return script.run_reindex(**arguments)

    def _structured_snapshot(self) -> tuple[int, int, int, int]:
        with database.get_connection() as connection:
            sheets = connection.execute(
                """SELECT id, content_id, owner_id, organization_id
                   FROM workbook_sheets WHERE content_id = ?""",
                (self.content_id,),
            ).fetchall()
            rows = connection.execute(
                """SELECT content_id, owner_id, organization_id, values_json
                   FROM workbook_rows WHERE content_id = ?
                   ORDER BY row_number""",
                (self.content_id,),
            ).fetchall()
        cell_count = sum(len(json.loads(row["values_json"])) for row in rows)
        return (
            len({row["content_id"] for row in sheets}),
            len(sheets),
            len(rows),
            cell_count,
        )

    def test_shared_content_reindex_preserves_duplicate_policy(self) -> None:
        qdrant_points = {
            f"existing-point-{document_id}" for document_id in self.document_ids
        }
        initial_point_count = len(qdrant_points)
        with database.get_connection() as connection:
            initial_content_count = connection.execute(
                "SELECT COUNT(*) FROM document_contents"
            ).fetchone()[0]
            initial_chunk_points = {
                row["vector_point_id"]
                for row in connection.execute(
                    """SELECT vector_point_id FROM chunks
                       WHERE content_id = ? ORDER BY id""",
                    (self.content_id,),
                ).fetchall()
            }

        with patch.object(
            vector_store,
            "get_vector_store",
            side_effect=AssertionError("structured reindex accessed Qdrant"),
        ) as get_store:
            first = self._run()
            baseline = self._structured_snapshot()
            second = self._run()

            with database.get_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE documents
                       SET deleted_at = CURRENT_TIMESTAMP, deleted_by = 10
                       WHERE id = ?""",
                    (self.document_ids[0],),
                )
                connection.execute(
                    """UPDATE document_versions
                       SET deleted_at = CURRENT_TIMESTAMP, deleted_by = 10,
                           deleted_with_document = 1
                       WHERE document_id = ?""",
                    (self.document_ids[0],),
                )
                connection.execute(
                    """UPDATE chunks
                       SET deleted_at = CURRENT_TIMESTAMP, deleted_by = 10,
                           deleted_with_document = 1
                       WHERE document_id = ?""",
                    (self.document_ids[0],),
                )
                document_routes._soft_delete_orphan_contents(
                    connection,
                    "org-a",
                    10,
                    deleted_with_document=True,
                )

            after_delete = self._run()
            forced = self._run(force=True)

        with database.get_connection() as connection:
            content_count = connection.execute(
                "SELECT COUNT(*) FROM document_contents"
            ).fetchone()[0]
            active_content = connection.execute(
                """SELECT owner_id, organization_id, deleted_at
                   FROM document_contents WHERE id = ?""",
                (self.content_id,),
            ).fetchone()
            deleted_content = connection.execute(
                """SELECT deleted_at FROM document_contents WHERE id = ?""",
                (self.deleted_content_id,),
            ).fetchone()
            active_documents = connection.execute(
                """SELECT id, content_id, is_duplicate_content
                   FROM documents
                   WHERE content_id = ? AND deleted_at IS NULL ORDER BY id""",
                (self.content_id,),
            ).fetchall()
            structured_owners = {
                tuple(row)
                for row in connection.execute(
                    """SELECT owner_id, organization_id
                       FROM workbook_sheets WHERE content_id = ?
                       UNION
                       SELECT owner_id, organization_id
                       FROM workbook_rows WHERE content_id = ?""",
                    (self.content_id, self.content_id),
                ).fetchall()
            }
            final_chunk_points = {
                row["vector_point_id"]
                for row in connection.execute(
                    """SELECT vector_point_id FROM chunks
                       WHERE content_id = ? ORDER BY id""",
                    (self.content_id,),
                ).fetchall()
            }

        self.assertEqual((first.scanned, first.completed), (2, 2))
        self.assertEqual(first.rows_indexed, 4)
        self.assertEqual(baseline, (1, 1, 2, 4))
        self.assertEqual(
            (second.scanned, second.eligible, second.skipped),
            (2, 0, 2),
        )
        self.assertEqual(
            (after_delete.scanned, after_delete.eligible, after_delete.skipped),
            (1, 0, 1),
        )
        self.assertEqual(forced.completed, 1)
        self.assertEqual(self._structured_snapshot(), baseline)
        self.assertEqual(content_count, initial_content_count)
        self.assertEqual(
            tuple(active_content),
            (10, "org-a", None),
        )
        self.assertIsNotNone(deleted_content["deleted_at"])
        self.assertEqual(
            [tuple(row) for row in active_documents],
            [(self.document_ids[1], self.content_id, 1)],
        )
        self.assertEqual(structured_owners, {(10, "org-a")})
        self.assertEqual(final_chunk_points, initial_chunk_points)
        self.assertEqual(len(qdrant_points), initial_point_count)
        get_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
