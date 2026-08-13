"""User-account lookup, duplicate detection, and safe merge helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3


@dataclass(frozen=True)
class UserMergeResult:
    """Describe one soft-deleted duplicate user that was merged into a primary."""

    email: str
    primary_user_id: int
    merged_user_id: int
    primary_organization_id: str
    merged_organization_id: str
    moved_documents: int
    moved_chat_sessions: int


def normalize_email(email: str) -> str:
    """Normalize email identity consistently before lookup or persistence."""
    return email.strip().casefold()


def get_active_user_by_email(
    connection: sqlite3.Connection,
    email: str,
) -> sqlite3.Row | None:
    """Load the single active account for a normalized email address."""
    return connection.execute(
        """SELECT id, email, password_hash, organization_id, role
           FROM users
           WHERE lower(email) = ? AND deleted_at IS NULL""",
        (normalize_email(email),),
    ).fetchone()


def active_email_exists(connection: sqlite3.Connection, email: str) -> bool:
    """Check active email uniqueness before attempting a registration write."""
    return get_active_user_by_email(connection, email) is not None


def insert_user_account(
    connection: sqlite3.Connection,
    *,
    email: str,
    password_hash: str,
    organization_id: str,
    role: str,
) -> int:
    """Insert one active account after the caller has created its organization."""
    cursor = connection.execute(
        """INSERT INTO users (email, password_hash, organization_id, role)
           VALUES (?, ?, ?, ?)""",
        (normalize_email(email), password_hash, organization_id, role),
    )
    return int(cursor.lastrowid)


def find_active_duplicate_email_groups(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return active normalized emails that still have more than one user."""
    return connection.execute(
        """SELECT lower(email) AS normalized_email, COUNT(*) AS user_count
           FROM users
           WHERE deleted_at IS NULL
           GROUP BY lower(email)
           HAVING COUNT(*) > 1
           ORDER BY lower(email)"""
    ).fetchall()


def _utc_now_iso() -> str:
    """Use one UTC timestamp format for merge audit and soft-delete records."""
    return datetime.now(timezone.utc).isoformat()


def _active_name_exists(
    connection: sqlite3.Connection,
    *,
    table: str,
    owner_id: int,
    column: str,
    value: str,
) -> bool:
    """Check trusted owner-scoped names while keeping values parameterized."""
    if table not in {"documents", "document_collections"}:
        raise ValueError("Unsupported owner-scoped table.")
    if column not in {"display_filename", "name"}:
        raise ValueError("Unsupported owner-scoped column.")
    deleted_clause = "AND deleted_at IS NULL" if table == "documents" else ""
    return connection.execute(
        f"""SELECT 1 FROM {table}
            WHERE owner_id = ? AND lower({column}) = lower(?) {deleted_clause}
            LIMIT 1""",
        (owner_id, value),
    ).fetchone() is not None


def _merged_name(base_name: str, merged_user_id: int, attempt: int) -> str:
    """Build a deterministic collision-safe name without losing the original."""
    suffix = f" (merged user {merged_user_id})"
    if attempt:
        suffix = f" (merged user {merged_user_id}-{attempt})"
    return f"{base_name}{suffix}"


def _rename_owner_conflicts(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    name_column: str,
    duplicate_user_id: int,
    primary_user_id: int,
) -> None:
    """Rename duplicate-owned rows that would collide after owner reassignment."""
    rows = connection.execute(
        f"""SELECT {id_column} AS row_id, {name_column} AS row_name
            FROM {table}
            WHERE owner_id = ?
            ORDER BY {id_column}""",
        (duplicate_user_id,),
    ).fetchall()
    for row in rows:
        original = str(row["row_name"])
        if not _active_name_exists(
            connection,
            table=table,
            owner_id=primary_user_id,
            column=name_column,
            value=original,
        ):
            continue
        attempt = 0
        candidate = _merged_name(original, duplicate_user_id, attempt)
        while _active_name_exists(
            connection,
            table=table,
            owner_id=primary_user_id,
            column=name_column,
            value=candidate,
        ):
            attempt += 1
            candidate = _merged_name(original, duplicate_user_id, attempt)
        connection.execute(
            f"UPDATE {table} SET {name_column} = ? WHERE {id_column} = ?",
            (candidate, row["row_id"]),
        )


def _avoid_content_hash_conflicts(
    connection: sqlite3.Connection,
    duplicate_user_id: int,
    primary_user_id: int,
) -> None:
    """Preserve duplicate content rows when active content hashes already exist."""
    rows = connection.execute(
        """SELECT id, normalized_content_hash
           FROM document_contents
           WHERE owner_id = ? AND deleted_at IS NULL
           ORDER BY id""",
        (duplicate_user_id,),
    ).fetchall()
    for row in rows:
        conflict = connection.execute(
            """SELECT 1 FROM document_contents
               WHERE owner_id = ? AND normalized_content_hash = ?
                 AND deleted_at IS NULL""",
            (primary_user_id, row["normalized_content_hash"]),
        ).fetchone()
        if conflict is None:
            continue
        connection.execute(
            """UPDATE document_contents
               SET normalized_content_hash = ?
               WHERE id = ?""",
            (f"{row['normalized_content_hash']}:merged:{row['id']}", row["id"]),
        )


def _merge_usage_records(
    connection: sqlite3.Connection,
    *,
    duplicate_user_id: int,
    primary_user_id: int,
    primary_organization_id: str,
) -> None:
    """Add duplicate quota history into the primary user before deleting old keys."""
    connection.execute(
        """INSERT INTO llm_usage
              (organization_id, user_id, usage_date, request_count,
               prompt_tokens, completion_tokens)
           SELECT ?, ?, usage_date, SUM(request_count),
                  SUM(prompt_tokens), SUM(completion_tokens)
           FROM llm_usage
           WHERE user_id = ?
           GROUP BY usage_date
           ON CONFLICT(organization_id, user_id, usage_date) DO UPDATE SET
               request_count = request_count + excluded.request_count,
               prompt_tokens = prompt_tokens + excluded.prompt_tokens,
               completion_tokens = completion_tokens + excluded.completion_tokens""",
        (primary_organization_id, primary_user_id, duplicate_user_id),
    )
    connection.execute(
        "DELETE FROM llm_usage WHERE user_id = ?",
        (duplicate_user_id,),
    )


def _merge_document_permissions(
    connection: sqlite3.Connection,
    *,
    duplicate_user_id: int,
    primary_user_id: int,
    primary_organization_id: str,
) -> None:
    """Move shared-document grants without duplicating existing permissions."""
    connection.execute(
        """INSERT OR IGNORE INTO document_permissions
              (organization_id, document_id, user_id, permission, created_at)
           SELECT ?, document_id, ?, permission, created_at
           FROM document_permissions
           WHERE user_id = ?""",
        (primary_organization_id, primary_user_id, duplicate_user_id),
    )
    connection.execute(
        "DELETE FROM document_permissions WHERE user_id = ?",
        (duplicate_user_id,),
    )


def _placeholders(values: list[int]) -> str:
    """Create trusted placeholder lists while keeping each value parameterized."""
    if not values:
        raise ValueError("At least one placeholder value is required.")
    return ",".join("?" for _ in values)


def _move_duplicate_owned_rows(
    connection: sqlite3.Connection,
    *,
    duplicate_user_id: int,
    primary_user_id: int,
    primary_organization_id: str,
) -> None:
    """Re-home duplicate-owned data to the primary account and tenant."""
    duplicate_document_ids = [
        int(row["id"]) for row in connection.execute(
            "SELECT id FROM documents WHERE owner_id = ? ORDER BY id",
            (duplicate_user_id,),
        ).fetchall()
    ]
    connection.execute(
        """UPDATE chat_messages
           SET organization_id = ?
           WHERE session_id IN (
               SELECT id FROM chat_sessions WHERE owner_id = ?
           )""",
        (primary_organization_id, duplicate_user_id),
    )
    for table in (
        "documents", "document_contents", "document_collections",
        "upload_batches", "workbook_sheets", "workbook_rows",
        "ingestion_jobs", "chat_sessions", "chat_contexts",
    ):
        connection.execute(
            f"""UPDATE {table}
                SET owner_id = ?, organization_id = ?
                WHERE owner_id = ?""",
            (primary_user_id, primary_organization_id, duplicate_user_id),
        )
    if duplicate_document_ids:
        document_placeholders = _placeholders(duplicate_document_ids)
        connection.execute(
            f"""UPDATE document_versions
                SET organization_id = ?,
                    created_by = CASE WHEN created_by = ? THEN ? ELSE created_by END
                WHERE document_id IN ({document_placeholders})""",
            (
                primary_organization_id,
                duplicate_user_id,
                primary_user_id,
                *duplicate_document_ids,
            ),
        )
        connection.execute(
            f"""UPDATE chunks
                SET organization_id = ?,
                    vector_point_id = NULL,
                    qdrant_indexed_at = NULL,
                    indexing_status = 'pending'
                WHERE document_id IN ({document_placeholders})""",
            (primary_organization_id, *duplicate_document_ids),
        )
        connection.execute(
            f"""UPDATE OR IGNORE document_permissions
                SET organization_id = ?
                WHERE document_id IN ({document_placeholders})""",
            (primary_organization_id, *duplicate_document_ids),
        )
        connection.execute(
            f"""DELETE FROM document_permissions
                WHERE document_id IN ({document_placeholders})
                  AND organization_id <> ?""",
            (*duplicate_document_ids, primary_organization_id),
        )
    connection.execute(
        """UPDATE document_versions
           SET created_by = ?
           WHERE created_by = ?""",
        (primary_user_id, duplicate_user_id),
    )
    connection.execute(
        """UPDATE audit_events
           SET user_id = ?, organization_id = ?
           WHERE user_id = ?""",
        (primary_user_id, primary_organization_id, duplicate_user_id),
    )
    connection.execute(
        """UPDATE password_reset_tokens
           SET user_id = ?, organization_id = ?
           WHERE user_id = ?""",
        (primary_user_id, primary_organization_id, duplicate_user_id),
    )


def _soft_delete_duplicate_user(
    connection: sqlite3.Connection,
    *,
    duplicate_user_id: int,
    merged_at: str,
) -> None:
    """Deactivate the duplicate account without removing its row history."""
    connection.execute(
        "UPDATE users SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (merged_at, duplicate_user_id),
    )


def _audit_user_merge(
    connection: sqlite3.Connection,
    *,
    result: UserMergeResult,
    merged_at: str,
) -> None:
    """Persist a compact record of each duplicate account merge."""
    connection.execute(
        """INSERT INTO user_merge_audit
             (email, primary_user_id, merged_user_id, primary_organization_id,
              merged_organization_id, merged_at, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            result.email,
            result.primary_user_id,
            result.merged_user_id,
            result.primary_organization_id,
            result.merged_organization_id,
            merged_at,
            json.dumps(
                {
                    "moved_documents": result.moved_documents,
                    "moved_chat_sessions": result.moved_chat_sessions,
                },
                sort_keys=True,
            ),
        ),
    )


def merge_duplicate_active_users(
    connection: sqlite3.Connection,
) -> list[UserMergeResult]:
    """Merge every active duplicate email group into one deterministic primary."""
    results: list[UserMergeResult] = []
    for group in find_active_duplicate_email_groups(connection):
        users = connection.execute(
            """SELECT id, email, organization_id, role, created_at
               FROM users
               WHERE lower(email) = ? AND deleted_at IS NULL
               ORDER BY
                   CASE WHEN role = 'organization_admin' THEN 0 ELSE 1 END,
                   created_at,
                   id""",
            (group["normalized_email"],),
        ).fetchall()
        if len(users) < 2:
            continue
        primary = users[0]
        primary_user_id = int(primary["id"])
        primary_organization_id = str(primary["organization_id"])
        for duplicate in users[1:]:
            duplicate_user_id = int(duplicate["id"])
            moved_documents = int(connection.execute(
                "SELECT COUNT(*) FROM documents WHERE owner_id = ?",
                (duplicate_user_id,),
            ).fetchone()[0])
            moved_chat_sessions = int(connection.execute(
                "SELECT COUNT(*) FROM chat_sessions WHERE owner_id = ?",
                (duplicate_user_id,),
            ).fetchone()[0])
            _rename_owner_conflicts(
                connection,
                table="documents",
                id_column="id",
                name_column="display_filename",
                duplicate_user_id=duplicate_user_id,
                primary_user_id=primary_user_id,
            )
            _rename_owner_conflicts(
                connection,
                table="document_collections",
                id_column="id",
                name_column="name",
                duplicate_user_id=duplicate_user_id,
                primary_user_id=primary_user_id,
            )
            _avoid_content_hash_conflicts(
                connection,
                duplicate_user_id,
                primary_user_id,
            )
            _merge_usage_records(
                connection,
                duplicate_user_id=duplicate_user_id,
                primary_user_id=primary_user_id,
                primary_organization_id=primary_organization_id,
            )
            _merge_document_permissions(
                connection,
                duplicate_user_id=duplicate_user_id,
                primary_user_id=primary_user_id,
                primary_organization_id=primary_organization_id,
            )
            _move_duplicate_owned_rows(
                connection,
                duplicate_user_id=duplicate_user_id,
                primary_user_id=primary_user_id,
                primary_organization_id=primary_organization_id,
            )
            merged_at = _utc_now_iso()
            _soft_delete_duplicate_user(
                connection,
                duplicate_user_id=duplicate_user_id,
                merged_at=merged_at,
            )
            result = UserMergeResult(
                email=str(group["normalized_email"]),
                primary_user_id=primary_user_id,
                merged_user_id=duplicate_user_id,
                primary_organization_id=primary_organization_id,
                merged_organization_id=str(duplicate["organization_id"]),
                moved_documents=moved_documents,
                moved_chat_sessions=moved_chat_sessions,
            )
            _audit_user_merge(connection, result=result, merged_at=merged_at)
            results.append(result)
    return results
