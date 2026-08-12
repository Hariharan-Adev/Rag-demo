"""Owner-scoped persistence for browser-independent chat history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from app.database import get_connection


def _now() -> str:
    """Return a stable UTC timestamp for history metadata updates."""
    return datetime.now(timezone.utc).isoformat()


def _organization_id(owner_id: int) -> str | None:
    """Resolve the tenant scope before reading or writing conversation data."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT organization_id FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
    return str(row["organization_id"]) if row else None


def _title_from_question(question: str) -> str:
    """Create the same compact title shape the frontend already displays."""
    normalized = " ".join(question.split())
    return normalized[:47].rstrip() + "..." if len(normalized) > 48 else normalized


def list_conversations(owner_id: int) -> list[dict[str, object]]:
    """Load all non-deleted conversations and messages for one owner."""
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return []
    with get_connection() as connection:
        sessions = connection.execute(
            """SELECT id, title, created_at, updated_at, is_pinned, pinned_at
               FROM chat_sessions
               WHERE organization_id = ? AND owner_id = ? AND deleted_at IS NULL
               ORDER BY is_pinned DESC, COALESCE(pinned_at, updated_at) DESC, updated_at DESC""",
            (organization_id, owner_id),
        ).fetchall()
        output = []
        for session in sessions:
            messages = connection.execute(
                """SELECT id, role, content, citations_json, created_at
                   FROM chat_messages
                   WHERE organization_id = ? AND session_id = ? AND deleted_at IS NULL
                   ORDER BY created_at, id""",
                (organization_id, session["id"]),
            ).fetchall()
            output.append({
                "id": str(session["id"]),
                "title": str(session["title"] or "New chat"),
                "created_at": str(session["created_at"]),
                "updated_at": str(session["updated_at"] or session["created_at"]),
                "is_pinned": bool(session["is_pinned"]),
                "pinned_at": session["pinned_at"],
                "messages": [
                    {
                        "id": str(message["id"]),
                        "role": str(message["role"]),
                        "content": str(message["content"]),
                        "citations": json.loads(str(message["citations_json"] or "[]")),
                        "created_at": str(message["created_at"]),
                    }
                    for message in messages
                ],
            })
    return output


def upsert_conversation(owner_id: int, conversation_id: str, title: str) -> None:
    """Create a session shell or revive its title without crossing owner scope."""
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return
    now = _now()
    with get_connection() as connection:
        connection.execute(
            """INSERT INTO chat_sessions
               (id, organization_id, owner_id, title, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title = CASE
                   WHEN chat_sessions.owner_id = excluded.owner_id
                    AND chat_sessions.organization_id = excluded.organization_id
                    AND TRIM(chat_sessions.title) = ''
                   THEN excluded.title ELSE chat_sessions.title END,
                 updated_at = CASE
                   WHEN chat_sessions.owner_id = excluded.owner_id
                    AND chat_sessions.organization_id = excluded.organization_id
                   THEN excluded.updated_at ELSE chat_sessions.updated_at END,
                 deleted_at = CASE
                   WHEN chat_sessions.owner_id = excluded.owner_id
                    AND chat_sessions.organization_id = excluded.organization_id
                   THEN NULL ELSE chat_sessions.deleted_at END""",
            (conversation_id, organization_id, owner_id, title[:48], now),
        )


def append_exchange(
    *,
    owner_id: int,
    conversation_id: str,
    question: str,
    answer: str,
    sources: list[dict[str, object]],
) -> None:
    """Persist the user question and assistant answer after the backend responds."""
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return
    now = _now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT OR IGNORE INTO chat_sessions
               (id, organization_id, owner_id, title, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (conversation_id, organization_id, owner_id, _title_from_question(question), now),
        )
        session = connection.execute(
            """SELECT id FROM chat_sessions
               WHERE id = ? AND organization_id = ? AND owner_id = ? AND deleted_at IS NULL""",
            (conversation_id, organization_id, owner_id),
        ).fetchone()
        if session is None:
            return
        connection.executemany(
            """INSERT INTO chat_messages
               (id, organization_id, session_id, role, content, citations_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (str(uuid4()), organization_id, conversation_id, "user", question, "[]", now),
                (
                    str(uuid4()),
                    organization_id,
                    conversation_id,
                    "assistant",
                    answer,
                    json.dumps(sources, ensure_ascii=False),
                    (datetime.now(timezone.utc) + timedelta(milliseconds=1)).isoformat(),
                ),
            ],
        )
        connection.execute(
            """UPDATE chat_sessions
               SET title = CASE WHEN TRIM(title) = '' THEN ? ELSE title END,
                   updated_at = ?
               WHERE id = ? AND organization_id = ? AND owner_id = ?""",
            (_title_from_question(question), now, conversation_id, organization_id, owner_id),
        )


def update_conversation(
    *,
    owner_id: int,
    conversation_id: str,
    title: str | None = None,
    is_pinned: bool | None = None,
) -> bool:
    """Patch title or pin state for one accessible session."""
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return False
    assignments = []
    params: list[object] = []
    if title is not None:
        normalized = " ".join(title.split())[:48]
        if not normalized:
            return False
        assignments.append("title = ?")
        params.append(normalized)
    if is_pinned is not None:
        assignments.extend(["is_pinned = ?", "pinned_at = ?"])
        params.extend([1 if is_pinned else 0, _now() if is_pinned else None])
    if not assignments:
        return True
    params.extend([conversation_id, organization_id, owner_id])
    with get_connection() as connection:
        cursor = connection.execute(
            f"""UPDATE chat_sessions
                SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND organization_id = ? AND owner_id = ?
                  AND deleted_at IS NULL""",
            params,
        )
    return cursor.rowcount > 0


def delete_conversation(owner_id: int, conversation_id: str) -> bool:
    """Soft-delete a conversation and its messages within the owner scope."""
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return False
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE chat_sessions
               SET deleted_at = CURRENT_TIMESTAMP
               WHERE id = ? AND organization_id = ? AND owner_id = ?
                 AND deleted_at IS NULL""",
            (conversation_id, organization_id, owner_id),
        )
        if cursor.rowcount:
            connection.execute(
                """UPDATE chat_messages
                   SET deleted_at = CURRENT_TIMESTAMP
                   WHERE session_id = ? AND organization_id = ? AND deleted_at IS NULL""",
                (conversation_id, organization_id),
            )
    return cursor.rowcount > 0
