"""Safe, bounded conversation context for grounded chat follow-ups."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.database import get_connection
from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services.document_access import READABLE_DOCUMENT_SQL

CONTEXT_TTL_HOURS = 6
MAX_CONTEXTS_PER_CONVERSATION = 6
FOLLOW_UP_WORDS = {
    "they", "them", "those", "these", "that", "it", "ones", "details",
    "same", "above", "previous", "their",
}
FOLLOW_UP_COMMAND_WORDS = {"name", "list", "show", "give", "which", "count", "group"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def is_follow_up_question(question: str) -> bool:
    """Detect reference-style follow-ups without relying on one exact phrase."""
    normalized = _normalize(question)
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    if re.search(r"\b(?:on|for|in)\s+\d{1,2}\s*(?:st|nd|rd|th)?\b", normalized):
        return True
    if tokens & FOLLOW_UP_WORDS:
        return True
    substantive = tokens - FOLLOW_UP_COMMAND_WORDS - {"me", "all", "the", "by"}
    if len(tokens) <= 4 and tokens & FOLLOW_UP_COMMAND_WORDS and not substantive:
        return True
    return bool(re.search(r"\b(only|just)\s+(for|in|by)\b|\bgroup\b.+\bby\b", normalized))


def _day_column_requested(question: str, rows: list[dict[str, object]]) -> str | None:
    """Match follow-ups like 'on 18 th' to stored day columns such as '18th'."""
    normalized = _normalize(question)
    match = re.search(r"\b(?:on|for|in)?\s*(\d{1,2})\s*(st|nd|rd|th)?\b", normalized)
    if not match:
        return None
    day = int(match.group(1))
    if not 1 <= day <= 31:
        return None
    suffix = match.group(2) or {1: "st", 2: "nd", 3: "rd"}.get(day if day < 20 else day % 10, "th")
    requested = f"{day}{suffix}"
    for row in rows:
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        for header in values:
            if _normalize(str(header)) == requested:
                return str(header)
    return None


def _organization_id(owner_id: int) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT organization_id FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
    return str(row["organization_id"]) if row else None


def _cited_ids(result: dict[str, object]) -> tuple[list[int], list[int]]:
    documents: set[int] = set()
    versions: set[int] = set()
    for source in result.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if source.get("document_id") is not None:
            documents.add(int(source["document_id"]))
        if source.get("version_id") is not None:
            versions.add(int(source["version_id"]))
    context = result.get("_context")
    if isinstance(context, dict):
        documents.update(int(value) for value in context.get("document_ids") or [])
        versions.update(int(value) for value in context.get("version_ids") or [])
    return sorted(documents), sorted(versions)


def _accessible_documents(
    owner_id: int,
    organization_id: str,
    document_ids: list[int],
    version_ids: list[int],
) -> set[int]:
    if not document_ids:
        return set()
    placeholders = ",".join("?" for _ in document_ids)
    version_filter = ""
    params: list[object] = [organization_id, owner_id, owner_id, *document_ids]
    if version_ids:
        version_filter = f" AND dv.id IN ({','.join('?' for _ in version_ids)})"
        params.extend(version_ids)
    with get_connection() as connection:
        rows = connection.execute(
            f"""SELECT DISTINCT d.id
                FROM documents d
                JOIN document_versions dv
                  ON dv.id = d.current_version_id
                 AND dv.document_id = d.id
                 AND dv.organization_id = d.organization_id
                WHERE {READABLE_DOCUMENT_SQL}
                  AND d.id IN ({placeholders})
                  AND dv.status = 'completed'
                  AND dv.deleted_at IS NULL
                  {version_filter}""",
            params,
        ).fetchall()
    return {int(row["id"]) for row in rows}


def save_grounded_context(
    *,
    owner_id: int,
    conversation_id: str | None,
    question: str,
    result: dict[str, object],
) -> None:
    """Persist only compact grounded context needed for later references."""
    if not conversation_id or not result.get("grounded"):
        return
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return
    source_document_ids = {
        int(source["document_id"])
        for source in (result.get("sources") or [])
        if isinstance(source, dict) and source.get("document_id") is not None
    }
    structured_context = result.get("_context")
    structured_document_ids = {
        int(value)
        for value in (
            structured_context.get("document_ids")
            if isinstance(structured_context, dict)
            else []
        ) or []
    }
    if structured_document_ids and source_document_ids and not structured_document_ids <= source_document_ids:
        return
    document_ids, version_ids = _cited_ids(result)
    accessible = _accessible_documents(owner_id, organization_id, document_ids, version_ids)
    if not accessible:
        return
    context = result.get("_context") if isinstance(result.get("_context"), dict) else {}
    payload = {
        "document_ids": [doc_id for doc_id in document_ids if doc_id in accessible],
        "version_ids": version_ids[:10],
        "question_type": result.get("question_type"),
        "structured": context,
        "sources": [
            {
                "document_id": source.get("document_id"),
                "version_id": source.get("version_id"),
                "filename": source.get("filename"),
                "source_type": source.get("source_type"),
                "source_location": source.get("source_location") or {},
            }
            for source in (result.get("sources") or [])
            if isinstance(source, dict) and source.get("document_id") in accessible
        ][:10],
    }
    expires_at = (_now() + timedelta(hours=CONTEXT_TTL_HOURS)).isoformat()
    with get_connection() as connection:
        connection.execute(
            """INSERT OR IGNORE INTO chat_sessions
               (id, organization_id, owner_id, title)
               VALUES (?, ?, ?, '')""",
            (conversation_id, organization_id, owner_id),
        )
        connection.execute(
            """INSERT INTO chat_contexts
               (organization_id, owner_id, conversation_id, previous_question,
                previous_answer, context_json, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                organization_id, owner_id, conversation_id, question[:1000],
                str(result.get("answer") or "")[:4000],
                json.dumps(payload, ensure_ascii=False),
                expires_at,
            ),
        )
        connection.execute(
            """DELETE FROM chat_contexts
               WHERE id NOT IN (
                   SELECT id FROM chat_contexts
                   WHERE organization_id = ? AND owner_id = ? AND conversation_id = ?
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC
                   LIMIT ?
               )
                 AND organization_id = ? AND owner_id = ? AND conversation_id = ?""",
            (
                organization_id, owner_id, conversation_id,
                MAX_CONTEXTS_PER_CONVERSATION,
                organization_id, owner_id, conversation_id,
            ),
        )


def _latest_context(owner_id: int, conversation_id: str) -> dict[str, object] | None:
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        return None
    with get_connection() as connection:
        row = connection.execute(
            """SELECT context_json, previous_answer
               FROM chat_contexts
               WHERE organization_id = ? AND owner_id = ? AND conversation_id = ?
                 AND deleted_at IS NULL AND expires_at > ?
               ORDER BY id DESC
               LIMIT 1""",
            (organization_id, owner_id, conversation_id, _now().isoformat()),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["context_json"]))
    payload["previous_answer"] = str(row["previous_answer"])
    payload["organization_id"] = organization_id
    return payload


def _load_context_rows(
    owner_id: int,
    organization_id: str,
    context: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    structured = context.get("structured") if isinstance(context.get("structured"), dict) else {}
    row_refs = structured.get("row_refs") if isinstance(structured, dict) else []
    document_ids = [int(value) for value in context.get("document_ids") or []]
    version_ids = [int(value) for value in context.get("version_ids") or []]
    if not row_refs or not document_ids:
        return [], []
    accessible = _accessible_documents(owner_id, organization_id, document_ids, version_ids)
    if not accessible:
        return [], []
    rows: list[dict[str, object]] = []
    sources: dict[tuple[int, str], dict[str, object]] = {}
    with get_connection() as connection:
        for ref in row_refs[:100]:
            if not isinstance(ref, dict):
                continue
            document_id = int(ref.get("document_id") or document_ids[0])
            if document_id not in accessible:
                continue
            sheet = str(ref.get("sheet") or "")
            row_number = int(ref.get("row_number") or 0)
            row = connection.execute(
                """SELECT d.display_filename, d.current_version_id, wr.values_json
                   FROM documents d
                   JOIN workbook_sheets ws ON ws.content_id = d.content_id
                   JOIN workbook_rows wr ON wr.sheet_id = ws.id
                  WHERE d.id = ? AND d.organization_id = ?
                    AND ws.name = ? AND wr.row_number = ?""",
                (document_id, organization_id, sheet, row_number),
            ).fetchone()
            if row is None:
                continue
            values = json.loads(str(row["values_json"]))
            rows.append({
                "document_id": document_id,
                "version_id": int(row["current_version_id"]),
                "filename": str(row["display_filename"]),
                "sheet": sheet,
                "row_number": row_number,
                "values": values,
            })
            key = (document_id, sheet)
            sources[key] = {
                "document_id": document_id,
                "version_id": int(row["current_version_id"]),
                "filename": str(row["display_filename"]),
                "source_type": "excel",
                "source_location": {"sheet_name": sheet, "row_start": row_number, "row_end": row_number},
                "retrieval_score": None,
            }
    return rows, list(sources.values())


def _rows_answer(question: str, context: dict[str, object], rows: list[dict[str, object]]) -> str:
    structured = context.get("structured") if isinstance(context.get("structured"), dict) else {}
    operation = str(structured.get("result_type") or structured.get("operation") or "")
    value_column = structured.get("value_column")
    entity_column = structured.get("entity_column")
    normalized = _normalize(question)
    day_column = _day_column_requested(question, rows)
    if day_column:
        lines = [f"{day_column} from the prior grounded result:"]
        for row in rows[:50]:
            value = row["values"].get(day_column)
            label = row["values"].get(str(entity_column or "EmpLoyee Name & No")) or row["filename"]
            lines.append(
                f"- {label}: {value} ({row['filename']}, {row['sheet']} row {row['row_number']})"
            )
        return "\n".join(lines)
    if re.search(r"\bhow many\b|\bcount\b", normalized):
        return f"Count: {len(rows):,}. Calculation basis: prior grounded context."
    column = entity_column or value_column
    if column:
        values = [
            str(row["values"].get(str(column))).strip()
            for row in rows
            if row["values"].get(str(column)) is not None and str(row["values"].get(str(column))).strip()
        ]
        if values and re.search(r"\b(name|names|list|show|what|which|details|them|they|those|ones)\b", normalized):
            label = "Values" if operation in {"total", "average", "count"} else "Items"
            return f"{label} from the prior grounded result ({len(values)}):\n" + "\n".join(f"- {value}" for value in values)
    lines = ["Records from the prior grounded result:"]
    for row in rows[:50]:
        rendered = "; ".join(
            f"{key}: {value}" for key, value in row["values"].items()
            if value is not None and str(value).strip()
        )
        lines.append(f"- {rendered} ({row['filename']}, {row['sheet']} row {row['row_number']})")
    return "\n".join(lines)


def resolve_follow_up(
    *,
    owner_id: int,
    conversation_id: str | None,
    question: str,
) -> dict[str, object] | None:
    """Resolve reference questions against the latest grounded context."""
    if not is_follow_up_question(question):
        return None
    if not conversation_id:
        return {"answer": UNAVAILABLE_ANSWER, "grounded": False, "sources": []}
    context = _latest_context(owner_id, conversation_id)
    if context is None:
        return {"answer": UNAVAILABLE_ANSWER, "grounded": False, "sources": []}
    organization_id = str(context["organization_id"])
    rows, sources = _load_context_rows(owner_id, organization_id, context)
    if rows:
        return {
            "answer": _rows_answer(question, context, rows),
            "question_type": "follow_up",
            "grounded": True,
            "sources": sources,
        }
    if len(context.get("document_ids") or []) > 1:
        return {
            "answer": "Do you mean the values used in the previous grounded answer?",
            "question_type": "clarification",
            "grounded": False,
            "sources": [],
        }
    return {"answer": UNAVAILABLE_ANSWER, "grounded": False, "sources": []}


def strip_internal_context(result: dict[str, object]) -> dict[str, object]:
    """Remove server-only context metadata before responding to clients."""
    return {key: value for key, value in result.items() if key != "_context"}
