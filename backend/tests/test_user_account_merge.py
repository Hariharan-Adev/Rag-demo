"""Regression tests for active email uniqueness and duplicate-user merging."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.auth import hash_password
from app.main import app
from app.models.user_accounts import merge_duplicate_active_users


class DuplicateUserMergeTests(unittest.TestCase):
    """Exercise duplicate email cleanup with representative user-owned data."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "merge.db"
        self.db_patch = patch.object(database, "DATABASE_PATH", self.database_path)
        self.db_patch.start()
        database.initialize_database()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.db_patch.stop()
        self.temporary.cleanup()

    def _create_primary_user(self) -> tuple[int, str]:
        """Create the first account through the public registration API."""
        response = self.client.post(
            "/auth/register",
            json={
                "email": "Owner@Example.com",
                "password": "primary-password",
                "organization_name": "Primary Org",
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        return int(payload["id"]), str(payload["organization_id"])

    def _insert_duplicate_user_data(
        self,
        primary_user_id: int,
        primary_organization_id: str,
    ) -> tuple[int, str]:
        """Seed an older duplicate-account state after dropping the new index."""
        duplicate_organization_id = "22222222-2222-4222-8222-222222222222"
        with database.get_connection() as connection:
            connection.execute("DROP INDEX IF EXISTS ux_users_active_email")
            connection.execute(
                "INSERT INTO organizations (id, name) VALUES (?, ?)",
                (duplicate_organization_id, "Duplicate Org"),
            )
            cursor = connection.execute(
                """INSERT INTO users
                   (email, password_hash, organization_id, role, created_at)
                   VALUES (?, ?, ?, 'organization_admin', ?)""",
                (
                    "owner@example.com",
                    hash_password("duplicate-password"),
                    duplicate_organization_id,
                    "2099-01-01T00:00:00+00:00",
                ),
            )
            duplicate_user_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO document_collections
                   (owner_id, organization_id, name)
                   VALUES (?, ?, ?)""",
                (primary_user_id, primary_organization_id, "Shared"),
            )
            duplicate_collection = connection.execute(
                """INSERT INTO document_collections
                   (owner_id, organization_id, name)
                   VALUES (?, ?, ?)""",
                (duplicate_user_id, duplicate_organization_id, "Shared"),
            ).lastrowid
            primary_content = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash, normalized_content_hash,
                    extracted_text, processing_status)
                   VALUES (?, ?, ?, ?, ?, 'completed')""",
                (primary_user_id, primary_organization_id, "file-a", "same-hash", "primary text"),
            ).lastrowid
            duplicate_content = connection.execute(
                """INSERT INTO document_contents
                   (owner_id, organization_id, file_hash, normalized_content_hash,
                    extracted_text, processing_status)
                   VALUES (?, ?, ?, ?, ?, 'completed')""",
                (duplicate_user_id, duplicate_organization_id, "file-b", "same-hash", "duplicate text"),
            ).lastrowid
            connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename, display_filename,
                    stored_filename, file_hash, content_id, is_duplicate_content,
                    processing_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'completed')""",
                (
                    primary_user_id,
                    primary_organization_id,
                    "same.txt",
                    "same.txt",
                    "primary.txt",
                    "file-a",
                    primary_content,
                ),
            )
            duplicate_document = connection.execute(
                """INSERT INTO documents
                   (owner_id, organization_id, original_filename, display_filename,
                    stored_filename, file_hash, content_id, is_duplicate_content,
                    collection_id, processing_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'completed')""",
                (
                    duplicate_user_id,
                    duplicate_organization_id,
                    "same.txt",
                    "same.txt",
                    "duplicate.txt",
                    "file-b",
                    duplicate_content,
                    duplicate_collection,
                ),
            ).lastrowid
            duplicate_version = connection.execute(
                """INSERT INTO document_versions
                   (organization_id, document_id, version_number, content_id,
                    stored_filename, file_hash, normalized_content_hash, status, created_by)
                   VALUES (?, ?, 1, ?, ?, ?, ?, 'completed', ?)""",
                (
                    duplicate_organization_id,
                    duplicate_document,
                    duplicate_content,
                    "duplicate.txt",
                    "file-b",
                    "same-hash",
                    duplicate_user_id,
                ),
            ).lastrowid
            connection.execute(
                "UPDATE documents SET current_version_id = ? WHERE id = ?",
                (duplicate_version, duplicate_document),
            )
            connection.execute(
                """INSERT INTO chunks
                   (content_id, document_id, version_id, chunk_index, text,
                    organization_id, source_type, source_location_json,
                    embedding_model, embedding_dimension, vector_point_id,
                    indexing_status, qdrant_indexed_at)
                   VALUES (?, ?, ?, 0, ?, ?, 'text', '{}', ?, ?, ?, 'completed', ?)""",
                (
                    duplicate_content,
                    duplicate_document,
                    duplicate_version,
                    "duplicate chunk",
                    duplicate_organization_id,
                    "all-MiniLM-L6-v2",
                    384,
                    "old-vector-point",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            connection.execute(
                """INSERT INTO workbook_sheets
                   (content_id, owner_id, organization_id, sheet_index, name, status)
                   VALUES (?, ?, ?, 0, 'Sheet1', 'processed')""",
                (duplicate_content, duplicate_user_id, duplicate_organization_id),
            )
            connection.execute(
                """INSERT INTO upload_batches
                   (owner_id, organization_id, collection_id, original_folder_name,
                    total_files)
                   VALUES (?, ?, ?, 'Folder', 1)""",
                (duplicate_user_id, duplicate_organization_id, duplicate_collection),
            )
            connection.execute(
                """INSERT INTO ingestion_jobs
                   (id, organization_id, owner_id, document_id, version_id,
                    idempotency_key, request_idempotency_key)
                   VALUES ('job-1', ?, ?, ?, ?, 'dup-key', 'request-key')""",
                (
                    duplicate_organization_id,
                    duplicate_user_id,
                    duplicate_document,
                    duplicate_version,
                ),
            )
            connection.execute(
                """INSERT INTO chat_sessions
                   (id, organization_id, owner_id, title)
                   VALUES ('session-1', ?, ?, 'Old chat')""",
                (duplicate_organization_id, duplicate_user_id),
            )
            connection.execute(
                """INSERT INTO chat_messages
                   (id, organization_id, session_id, role, content)
                   VALUES ('message-1', ?, 'session-1', 'user', 'hello')""",
                (duplicate_organization_id,),
            )
            connection.execute(
                """INSERT INTO chat_contexts
                   (organization_id, owner_id, conversation_id, previous_question,
                    previous_answer, context_json, expires_at)
                   VALUES (?, ?, 'session-1', 'q', 'a', '{}', '2099-01-01')""",
                (duplicate_organization_id, duplicate_user_id),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (user_id, organization_id, event_type, endpoint, outcome)
                   VALUES (?, ?, 'test', 'test', 'ok')""",
                (duplicate_user_id, duplicate_organization_id),
            )
            connection.execute(
                """INSERT INTO llm_usage
                   (organization_id, user_id, usage_date, request_count,
                    prompt_tokens, completion_tokens)
                   VALUES (?, ?, '2026-08-13', 2, 10, 20)""",
                (duplicate_organization_id, duplicate_user_id),
            )
            connection.execute(
                """INSERT INTO password_reset_tokens
                   (organization_id, user_id, token_hash, expires_at)
                   VALUES (?, ?, 'token-hash', '2099-01-01')""",
                (duplicate_organization_id, duplicate_user_id),
            )
        return duplicate_user_id, duplicate_organization_id

    def test_duplicate_user_data_merges_into_primary_and_soft_deletes_duplicate(self) -> None:
        """Duplicate-owned documents, chats, and tokens move to one active email."""
        primary_user_id, primary_organization_id = self._create_primary_user()
        duplicate_user_id, duplicate_organization_id = self._insert_duplicate_user_data(
            primary_user_id,
            primary_organization_id,
        )

        with database.get_connection() as connection:
            results = merge_duplicate_active_users(connection)
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_users_active_email
                   ON users(lower(email))
                   WHERE deleted_at IS NULL"""
            )

        self.assertEqual(len(results), 1)
        with database.get_connection() as connection:
            active_count = connection.execute(
                """SELECT COUNT(*) FROM users
                   WHERE lower(email) = 'owner@example.com' AND deleted_at IS NULL"""
            ).fetchone()[0]
            duplicate_deleted_at = connection.execute(
                "SELECT deleted_at FROM users WHERE id = ?",
                (duplicate_user_id,),
            ).fetchone()[0]
            moved_document = connection.execute(
                """SELECT owner_id, organization_id, display_filename
                   FROM documents WHERE stored_filename = 'duplicate.txt'"""
            ).fetchone()
            chunk = connection.execute(
                """SELECT organization_id, vector_point_id, indexing_status,
                          qdrant_indexed_at
                   FROM chunks WHERE text = 'duplicate chunk'"""
            ).fetchone()
            chat_message = connection.execute(
                "SELECT organization_id FROM chat_messages WHERE id = 'message-1'"
            ).fetchone()
            reset_token = connection.execute(
                "SELECT organization_id, user_id FROM password_reset_tokens WHERE token_hash = 'token-hash'"
            ).fetchone()
            usage = connection.execute(
                """SELECT request_count, prompt_tokens, completion_tokens
                   FROM llm_usage
                   WHERE organization_id = ? AND user_id = ? AND usage_date = '2026-08-13'""",
                (primary_organization_id, primary_user_id),
            ).fetchone()
            audit = connection.execute(
                "SELECT merged_user_id FROM user_merge_audit WHERE merged_user_id = ?",
                (duplicate_user_id,),
            ).fetchone()

        self.assertEqual(active_count, 1)
        self.assertIsNotNone(duplicate_deleted_at)
        self.assertEqual(moved_document["owner_id"], primary_user_id)
        self.assertEqual(moved_document["organization_id"], primary_organization_id)
        self.assertIn(f"merged user {duplicate_user_id}", moved_document["display_filename"])
        self.assertEqual(chunk["organization_id"], primary_organization_id)
        self.assertIsNone(chunk["vector_point_id"])
        self.assertEqual(chunk["indexing_status"], "pending")
        self.assertIsNone(chunk["qdrant_indexed_at"])
        self.assertEqual(chat_message["organization_id"], primary_organization_id)
        self.assertEqual(reset_token["organization_id"], primary_organization_id)
        self.assertEqual(reset_token["user_id"], primary_user_id)
        self.assertEqual(tuple(usage), (2, 10, 20))
        self.assertIsNotNone(audit)
        self.assertEqual(duplicate_organization_id, results[0].merged_organization_id)

    def test_login_and_forgot_password_use_single_active_email(self) -> None:
        """Email-only login and reset target the primary user after merge."""
        primary_user_id, primary_organization_id = self._create_primary_user()
        self._insert_duplicate_user_data(primary_user_id, primary_organization_id)
        with database.get_connection() as connection:
            merge_duplicate_active_users(connection)
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_users_active_email
                   ON users(lower(email))
                   WHERE deleted_at IS NULL"""
            )

        login = self.client.post(
            "/auth/login",
            data={"username": "OWNER@example.com", "password": "primary-password"},
        )
        duplicate_login = self.client.post(
            "/auth/login",
            data={"username": "owner@example.com", "password": "duplicate-password"},
        )
        with (
            patch("app.routes.auth.token_urlsafe", return_value="single-reset-token-value-1234567890"),
            patch("app.routes.auth.send_password_reset_email", return_value=True) as mailer,
        ):
            reset = self.client.post(
                "/auth/forgot-password",
                json={"email": "owner@example.com"},
            )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(duplicate_login.status_code, 401)
        self.assertEqual(reset.status_code, 200)
        mailer.assert_called_once()
        with database.get_connection() as connection:
            reset_owner = connection.execute(
                """SELECT user_id, organization_id
                   FROM password_reset_tokens
                   WHERE token_hash <> 'token-hash'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(reset_owner["user_id"], primary_user_id)
        self.assertEqual(reset_owner["organization_id"], primary_organization_id)

    def test_existing_email_registration_is_rejected(self) -> None:
        """Registration rejects any active email before creating another user."""
        self._create_primary_user()

        response = self.client.post(
            "/auth/register",
            json={
                "email": "owner@example.com",
                "password": "another-password",
                "organization_name": "Another Org",
            },
        )

        self.assertEqual(response.status_code, 409)

    def test_malicious_looking_auth_inputs_are_not_interpolated(self) -> None:
        """SQL-like email strings fail safely without changing auth state."""
        self._create_primary_user()
        malicious_email = "owner@example.com' OR 1=1 --"

        login = self.client.post(
            "/auth/login",
            data={"username": malicious_email, "password": "primary-password"},
        )
        forgot = self.client.post(
            "/auth/forgot-password",
            json={"email": malicious_email},
        )
        register = self.client.post(
            "/auth/register",
            json={
                "email": malicious_email,
                "password": "another-password",
                "organization_name": "Bad Org",
            },
        )

        self.assertEqual(login.status_code, 401)
        self.assertEqual(forgot.status_code, 422)
        self.assertEqual(register.status_code, 422)


if __name__ == "__main__":
    unittest.main()
