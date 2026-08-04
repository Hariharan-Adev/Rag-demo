"""Schema-driven structured document analysis for workbook-like tables."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from json import loads
import re

from app.config import settings
from app.database import get_connection
from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services.document_access import READABLE_DOCUMENT_SQL


INTENT_PATTERNS = (
    r"\bhow many\b", r"\bcount\b", r"\b(total|sum)\b",
    r"\b(average|avg|mean)\b", r"\b(minimum|min|lowest|smallest)\b",
    r"\b(maximum|max|highest|largest)\b", r"\b(unique|distinct)\b",
    r"\b(list|show|which|what are|give)\b", r"\bgroup\b.+\bby\b",
    r"\bbetween\b.+\band\b", r"\bfrom\b.+\bto\b",
    r"\b(below|under|less than|above|over|greater than|at least|at most)\b",
)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does",
    "for", "from", "give", "how", "in", "is", "it", "me", "of", "on",
    "or", "show", "tell", "than", "the", "them", "there", "these",
    "they", "this", "those", "to", "was", "were", "what", "which",
    "with",
}
MONTH_ALIASES = {
    "jan": "01", "january": "01", "feb": "02", "february": "02",
    "mar": "03", "march": "03", "apr": "04", "april": "04",
    "may": "05", "jun": "06", "june": "06", "jul": "07", "july": "07",
    "aug": "08", "august": "08", "sep": "09", "sept": "09",
    "september": "09", "oct": "10", "october": "10", "nov": "11",
    "november": "11", "dec": "12", "december": "12",
}


@dataclass
class RowRecord:
    sheet: str
    row_number: int
    values: dict[str, object]


@dataclass
class WorkbookScope:
    document_id: int
    version_id: int
    filename: str
    rows: list[RowRecord]
    sheet_names: list[str]
    schema: dict[str, dict[str, str]]


@dataclass
class Plan:
    intent: str
    value_column: str | None = None
    entity_column: str | None = None
    group_column: str | None = None
    list_column: str | None = None
    filters: dict[str, set[str]] | None = None
    numeric_filter: tuple[str, str, Decimal, Decimal | None] | None = None
    confidence: int = 0
    rejection_reason: str | None = None


def is_analytical_question(question: str) -> bool:
    """Detect whether a question can benefit from table-structured planning."""
    normalized = _normalized(question)
    return any(re.search(pattern, normalized) for pattern in INTENT_PATTERNS)


def is_structured_lookup_question(
    question: str,
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> bool:
    """Return whether accessible structured rows can answer the question."""
    scopes = _load_scopes(owner_id, collection_id, document_id)
    return any(
        _is_time_matrix_question(scope, question)
        or _plan_for_scope(scope, question, explicit_scope=document_id is not None).intent != "unavailable"
        for scope in scopes
    )


def has_structured_workbook(
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> bool:
    """Check for accessible completed current versions with stored rows."""
    with get_connection() as connection:
        return connection.execute(
            f"""
            SELECT 1
            FROM documents d
            JOIN document_versions dv
              ON dv.id = d.current_version_id
             AND dv.document_id = d.id
             AND dv.organization_id = d.organization_id
            JOIN workbook_sheets ws ON ws.content_id = d.content_id
            WHERE {READABLE_DOCUMENT_SQL}
              AND ws.organization_id = ?
              AND dv.status = 'completed'
              AND dv.deleted_at IS NULL
              AND (? IS NULL OR d.collection_id = ?)
              AND (? IS NULL OR d.id = ?)
            LIMIT 1
            """,
            (
                *_readable_params(owner_id),
                _organization_id(owner_id),
                collection_id, collection_id, document_id, document_id,
            ),
        ).fetchone() is not None


def analyze_workbook_question(
    question: str,
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> dict[str, object]:
    """Plan and answer from all relevant structured rows, not vector excerpts."""
    scopes = _load_scopes(owner_id, collection_id, document_id)
    time_matrix_answers = [
        (_source_evidence(scope, question)[0], answer)
        for scope in scopes
        if (answer := _answer_time_matrix(scope, question)) is not None
    ]
    if time_matrix_answers:
        time_matrix_answers.sort(key=lambda item: -item[0])
        return time_matrix_answers[0][1]
    ranked = _rank_scopes(scopes, question, explicit_scope=document_id is not None)
    if not ranked or ranked[0][0] < 2:
        return _unavailable("no_relevant_structured_source")
    scope = ranked[0][1]
    plan = _plan_for_scope(scope, question, explicit_scope=document_id is not None)
    if plan.intent == "unavailable":
        return _unavailable(plan.rejection_reason or "invalid_structured_plan")
    rows = _apply_filters(scope.rows, plan.filters or {}, plan.numeric_filter)
    if not rows:
        return _unavailable("structured_filters_matched_no_rows")
    return _answer_from_rows(scope, rows, plan)


def _organization_id(owner_id: int) -> str:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT organization_id FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
    return str(row["organization_id"]) if row else ""


def _readable_params(owner_id: int) -> tuple[object, object, object]:
    return (_organization_id(owner_id), owner_id, owner_id)


def _load_scopes(
    owner_id: int,
    collection_id: int | None,
    document_id: int | None,
) -> list[WorkbookScope]:
    organization_id = _organization_id(owner_id)
    if not organization_id:
        return []
    with get_connection() as connection:
        documents = connection.execute(
            f"""
            SELECT DISTINCT d.id, d.current_version_id, d.display_filename, d.content_id
            FROM documents d
            JOIN document_versions dv
              ON dv.id = d.current_version_id
             AND dv.document_id = d.id
             AND dv.organization_id = d.organization_id
            JOIN workbook_sheets ws ON ws.content_id = d.content_id
            WHERE {READABLE_DOCUMENT_SQL}
              AND ws.organization_id = ?
              AND dv.status = 'completed'
              AND dv.deleted_at IS NULL
              AND (? IS NULL OR d.collection_id = ?)
              AND (? IS NULL OR d.id = ?)
            ORDER BY d.id
            """,
            (
                organization_id, owner_id, owner_id, organization_id,
                collection_id, collection_id, document_id, document_id,
            ),
        ).fetchall()
        scopes: list[WorkbookScope] = []
        for document in documents:
            sheets = connection.execute(
                """SELECT id, name, headers_json, schema_json
                   FROM workbook_sheets
                   WHERE content_id = ? AND organization_id = ? AND status = 'processed'
                   ORDER BY sheet_index""",
                (document["content_id"], organization_id),
            ).fetchall()
            rows: list[RowRecord] = []
            schema: dict[str, dict[str, str]] = {}
            for sheet in sheets:
                try:
                    sheet_schema = loads(str(sheet["schema_json"] or "{}"))
                    for column in sheet_schema.get("columns", []):
                        schema[str(column.get("name"))] = {
                            "type": str(column.get("type") or "text"),
                            "sheet": str(sheet["name"]),
                        }
                except Exception:
                    for header in loads(str(sheet["headers_json"] or "[]")):
                        schema[str(header)] = {"type": "text", "sheet": str(sheet["name"])}
                stored_rows = connection.execute(
                    """SELECT row_number, values_json
                       FROM workbook_rows
                       WHERE sheet_id = ? AND content_id = ? AND organization_id = ?
                       ORDER BY row_number""",
                    (sheet["id"], document["content_id"], organization_id),
                ).fetchall()
                rows.extend(
                    RowRecord(
                        sheet=str(sheet["name"]),
                        row_number=int(row["row_number"]),
                        values=loads(str(row["values_json"])),
                    )
                    for row in stored_rows
                )
            scopes.append(WorkbookScope(
                document_id=int(document["id"]),
                version_id=int(document["current_version_id"]),
                filename=str(document["display_filename"]),
                rows=rows,
                sheet_names=[str(sheet["name"]) for sheet in sheets],
                schema=schema,
            ))
    return scopes


def _normalized(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _tokens(value: object) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", str(value).casefold()):
        if token in STOP_WORDS or len(token) <= 1:
            continue
        tokens.add(token)
        if len(token) > 3 and token.endswith("s"):
            tokens.add(token[:-1])
        if len(token) > 4 and token.endswith("ed"):
            tokens.add(token[:-1])
            tokens.add(token[:-2])
    return tokens


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not re.fullmatch(r"[^\w\s-]?\s*\(?-?\d[\d,]*(?:\.\d+)?\)?", text):
        return None
    try:
        return Decimal(re.sub(r"[^\d.\-]", "", text))
    except InvalidOperation:
        return None


def _question_numbers(question: str) -> list[Decimal]:
    """Extract bounded numeric literals and common scale words from a question."""
    numbers: list[Decimal] = []
    for match in re.finditer(r"(?<![a-z0-9])\d[\d,]*(?:\.\d+)?(?:\s*(?:lakh|lak))?", question.casefold()):
        text = match.group(0).replace(",", "")
        scale = Decimal(100000) if re.search(r"\b(?:lakh|lak)\b", text) else Decimal(1)
        numeric = re.search(r"\d+(?:\.\d+)?", text)
        if numeric:
            numbers.append(Decimal(numeric.group(0)) * scale)
    return numbers


def _numeric_condition(question: str) -> tuple[str, Decimal, Decimal | None] | None:
    """Parse generic numeric comparisons without naming any document domain."""
    normalized = _normalized(question)
    numbers = _question_numbers(question)
    if not numbers:
        return None
    if "between" in normalized and len(numbers) >= 2:
        low, high = sorted((numbers[0], numbers[1]))
        return "between", low, high
    if re.search(r"\b(below|under|less than)\b", normalized):
        return "lt", numbers[0], None
    if re.search(r"\b(at most|up to|no more than)\b", normalized):
        return "le", numbers[0], None
    if re.search(r"\b(above|over|greater than|more than)\b", normalized):
        return "gt", numbers[0], None
    if re.search(r"\b(at least|minimum of|not less than)\b", normalized):
        return "ge", numbers[0], None
    return None


def _numeric_matches(value: object, condition: tuple[str, Decimal, Decimal | None]) -> bool:
    number = _number(value)
    if number is None:
        return False
    operator, left, right = condition
    if operator == "between":
        return right is not None and left <= number <= right
    if operator == "lt":
        return number < left
    if operator == "le":
        return number <= left
    if operator == "gt":
        return number > left
    if operator == "ge":
        return number >= left
    return False


_WEEKDAY_PATTERN = re.compile(
    r"\b(?:mon|monday|tue|tues|tuesday|wed|wednesday|thu|thur|thurs|thursday|fri|friday|sat|saturday|sun|sunday)\b"
)


def _time_minutes(value: object) -> Decimal | None:
    """Parse a clock/duration cell without treating it as an ordinary number."""
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d{1,3}):([0-5]\d)(?::([0-5]\d))?\s*", str(value))
    if not match:
        return None
    hours, minutes, seconds = (Decimal(part or "0") for part in match.groups())
    return hours * 60 + minutes + seconds / Decimal(60)


def _time_role(value: object) -> str | None:
    marker = _normalized(value)
    if marker == "in":
        return "in"
    if marker == "out":
        return "out"
    tokens = set(marker.split())
    if "total" in tokens and ({"hour", "hours", "hr", "hrs"} & tokens):
        return "total"
    return None


def _time_matrix_columns(scope: WorkbookScope) -> tuple[str, str, str | None, list[str]] | None:
    """Discover entity, marker, identifier, and date columns from cell semantics."""
    columns = _column_values(scope.rows)
    marker_candidates: list[tuple[int, str]] = []
    for header, values in columns.items():
        roles = {_time_role(value) for value in values} - {None}
        if {"in", "out"} <= roles:
            marker_candidates.append((len(roles), header))
    if not marker_candidates:
        return None
    marker_candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    marker_column = marker_candidates[0][1]
    name_column = max(
        (header for header in columns if header != marker_column),
        key=lambda header: (_column_score(header, "employee name"), -len(header)),
        default="",
    )
    if _column_score(name_column, "employee name") <= 0:
        return None
    number_column = max(
        (header for header in columns if header not in {marker_column, name_column}),
        key=lambda header: _column_score(header, "employee number id"),
        default="",
    ) or None
    if number_column and _column_score(number_column, "employee number id") <= 0:
        number_column = None
    date_columns = [
        header for header in columns
        if header not in {marker_column, name_column, number_column}
        and (
            _WEEKDAY_PATTERN.search(header.casefold())
            or re.search(r"\b\d{1,2}(?:st|nd|rd|th)\b", header.casefold())
        )
        and any(_time_minutes(value) is not None for value in columns[header])
    ]
    if not date_columns:
        return None
    return name_column, marker_column, number_column, date_columns


def _is_time_matrix_question(scope: WorkbookScope, question: str) -> bool:
    normalized = _normalized(question)
    asks_duration = bool(
        re.search(r"\b(worked|working|work)\b", normalized)
        and re.search(r"\b(hour|hours|hrs|duration)\b", normalized)
    )
    return asks_duration and _numeric_condition(question) is not None and _time_matrix_columns(scope) is not None


def _duration_matches(minutes: Decimal, condition: tuple[str, Decimal, Decimal | None]) -> bool:
    operator, left, right = condition
    return _numeric_matches(
        minutes,
        (operator, left * Decimal(60), right * Decimal(60) if right is not None else None),
    )


def _format_duration(minutes: Decimal) -> str:
    total_seconds = int(minutes * Decimal(60))
    hours, remainder = divmod(total_seconds, 3600)
    minute, second = divmod(remainder, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}"


def _answer_time_matrix(scope: WorkbookScope, question: str) -> dict[str, object] | None:
    """Answer duration comparisons from wide IN/OUT attendance-style matrices."""
    if not _is_time_matrix_question(scope, question):
        return None
    discovered = _time_matrix_columns(scope)
    condition = _numeric_condition(question)
    if discovered is None or condition is None:
        return None
    name_column, marker_column, number_column, date_columns = discovered
    grouped: dict[tuple[str, str], dict[str, RowRecord]] = defaultdict(dict)
    for row in scope.rows:
        role = _time_role(row.values.get(marker_column))
        name = str(row.values.get(name_column) or "").strip()
        if not role or not name:
            continue
        grouped[(row.sheet, name)][role] = row

    matches: list[tuple[str, str, object, object, Decimal, list[RowRecord]]] = []
    for (_, name), role_rows in grouped.items():
        in_row = role_rows.get("in")
        out_row = role_rows.get("out")
        total_row = role_rows.get("total")
        if not in_row or not out_row:
            continue
        for date_column in date_columns:
            in_value = in_row.values.get(date_column)
            out_value = out_row.values.get(date_column)
            in_minutes = _time_minutes(in_value)
            out_minutes = _time_minutes(out_value)
            if in_minutes is None or out_minutes is None:
                continue
            calculated_minutes = out_minutes - in_minutes
            if calculated_minutes < 0:
                calculated_minutes += Decimal(24 * 60)
            provided_minutes = _time_minutes(total_row.values.get(date_column)) if total_row else None
            # Use the workbook total when it agrees with the IN/OUT pair. A visibly
            # stale or broken formula must not override the auditable calculation.
            total_minutes = (
                provided_minutes
                if provided_minutes is not None and abs(provided_minutes - calculated_minutes) <= Decimal(1)
                else calculated_minutes
            )
            if _duration_matches(total_minutes, condition):
                contributing = [in_row, out_row, *([total_row] if total_row else [])]
                matches.append((name, date_column, in_value, out_value, total_minutes, contributing))

    if not matches:
        return {
            "answer": "No employees matched the requested working-hours condition.",
            "question_type": "structured_analysis",
            "calculation_basis": "All readable IN/OUT pairs were evaluated by employee and date.",
            "sources": [],
            "grounded": True,
            "_context": {"kind": "structured_rows", "result_type": "records", "document_ids": [scope.document_id], "version_ids": [scope.version_id], "row_refs": []},
        }

    lines = [
        "| Employee Name | Date | IN Time | OUT Time | Total Working Hours |",
        "|---|---|---|---|---|",
    ]
    contributing_rows: list[RowRecord] = []
    for name, date, in_value, out_value, duration, records in matches:
        lines.append(
            "| " + " | ".join((_display(name), _display(date), _display(in_value), _display(out_value), _format_duration(duration))) + " |"
        )
        contributing_rows.extend(records)
    unique_rows = list({(row.sheet, row.row_number): row for row in contributing_rows}.values())
    return {
        "answer": f"Matching records ({len(matches)}):\n\n" + "\n".join(lines),
        "question_type": "structured_analysis",
        "calculation_basis": f"{len(matches)} employee-date pair(s) matched after evaluating IN, OUT, and total working hours.",
        "sources": _sources(scope, unique_rows),
        "grounded": True,
        "_context": {
            "kind": "structured_rows",
            "result_type": "records",
            "filters": {},
            "numeric_filter": {"column": "Total Working Hours", "operator": condition[0], "left": str(condition[1]), "right": str(condition[2]) if condition[2] is not None else None},
            "document_ids": [scope.document_id],
            "version_ids": [scope.version_id],
            "row_refs": [{"document_id": scope.document_id, "sheet": row.sheet, "row_number": row.row_number} for row in unique_rows],
        },
    }


def _canonical(value: object) -> str:
    normalized = _normalized(value)
    return MONTH_ALIASES.get(normalized, normalized)


def _value_matches_month(value: object, month: str) -> bool:
    """Match month names and common numeric date shapes without schema-specific rules."""
    normalized = _normalized(value)
    if MONTH_ALIASES.get(normalized) == month:
        return True
    return bool(
        re.search(rf"\b\d{{4}}\s+{month}\s+\d{{1,2}}\b", normalized)
        or re.search(rf"\b\d{{1,2}}\s+{month}\s+\d{{4}}\b", normalized)
    )


def _column_values(rows: list[RowRecord]) -> dict[str, list[object]]:
    values: dict[str, list[object]] = defaultdict(list)
    for row in rows:
        for header, value in row.values.items():
            if value is not None and str(value).strip():
                values[header].append(value)
    return values


def _row_filters(rows: list[RowRecord], question: str) -> dict[str, set[str]]:
    """Build exact filters from meaningful values mentioned in the question."""
    question_text = f" {_normalized(question)} "
    question_tokens = _tokens(question)
    filters: dict[str, set[str]] = {}
    for header, values in _column_values(rows).items():
        matched = set()
        header_requested = bool(_tokens(header) & question_tokens)
        for value in values:
            normalized_value = _normalized(value)
            if not normalized_value:
                continue
            time_marker_requested = (
                normalized_value in {"in", "out"}
                and bool(re.search(rf"\b{normalized_value}\s+times?\b", _normalized(question)))
            )
            if (
                normalized_value in STOP_WORDS
                and not header_requested
                and not time_marker_requested
            ):
                continue
            if _number(value) is not None:
                continue
            value_tokens = _tokens(normalized_value)
            exact_value = f" {normalized_value} " in question_text
            if exact_value or (value_tokens and value_tokens <= question_tokens):
                matched.add(_canonical(value))
        if matched:
            filters[header] = matched
    mentioned_months = {
        canonical for alias, canonical in MONTH_ALIASES.items()
        if re.search(rf"\b{re.escape(alias)}\b", _normalized(question))
    }
    if mentioned_months:
        for header, values in _column_values(rows).items():
            if any(any(_value_matches_month(value, month) for month in mentioned_months) for value in values):
                filters.setdefault(header, set()).update(mentioned_months)
    return filters


def _apply_filters(
    rows: list[RowRecord],
    filters: dict[str, set[str]],
    numeric_filter: tuple[str, str, Decimal, Decimal | None] | None = None,
) -> list[RowRecord]:
    if not filters and numeric_filter is None:
        return rows
    filtered = [
        row for row in rows
        if all(
            _canonical(row.values.get(header)) in accepted
            or any(_value_matches_month(row.values.get(header), value) for value in accepted)
            for header, accepted in filters.items()
        )
    ]
    if numeric_filter is None:
        return filtered
    header, operator, left, right = numeric_filter
    return [
        row for row in filtered
        if _numeric_matches(row.values.get(header), (operator, left, right))
    ]


def _operation(question: str) -> str:
    normalized = _normalized(question)
    if re.search(r"\b(total|sum)\b", normalized):
        return "total"
    if re.search(r"\b(average|avg|mean)\b", normalized):
        return "average"
    if re.search(r"\b(maximum|max|highest|largest)\b", normalized):
        return "maximum"
    if re.search(r"\b(minimum|min|lowest|smallest)\b", normalized):
        return "minimum"
    if re.search(r"\b(unique|distinct)\b", normalized):
        return "distinct"
    if re.search(r"\bhow many\b|\bcount\b", normalized):
        return "count"
    if re.search(r"\bgroup\b.+\bby\b", normalized):
        return "group"
    return "list"


def _column_score(header: str, question: str) -> int:
    header_tokens = _tokens(header)
    question_tokens = _tokens(question)
    if not header_tokens:
        return 0
    return len(header_tokens & question_tokens) * 5


def _source_evidence(scope: WorkbookScope, question: str) -> tuple[int, list[str]]:
    """Score domain-neutral evidence that this workbook is about the question."""
    question_tokens = _tokens(question)
    reasons: list[str] = []
    score = 0
    filename_hits = _tokens(scope.filename) & question_tokens
    if filename_hits:
        score += len(filename_hits) * 4
        reasons.append("filename_token_match")
    sheet_hits = set().union(*(_tokens(sheet) for sheet in scope.sheet_names), set()) & question_tokens
    if sheet_hits:
        score += len(sheet_hits) * 3
        reasons.append("sheet_token_match")
    for header, values in _column_values(scope.rows).items():
        header_score = _column_score(header, question)
        if header_score:
            score += header_score
            reasons.append("header_token_match")
        value_tokens = set()
        for value in values[:200]:
            value_tokens |= _tokens(value)
        if value_tokens & question_tokens:
            score += 2
            reasons.append("value_token_match")
    return score, sorted(set(reasons))


def _choose_column(
    scope: WorkbookScope,
    question: str,
    *,
    numeric: bool = False,
    preferred_types: set[str] | None = None,
    hint: str | None = None,
) -> str | None:
    subject = hint or question
    candidates: list[tuple[int, str]] = []
    columns = _column_values(scope.rows)
    for header, values in columns.items():
        inferred = scope.schema.get(header, {}).get("type", "text")
        if numeric and not any(_number(value) is not None for value in values):
            continue
        score = _column_score(header, subject)
        if score > 0 and preferred_types and inferred in preferred_types:
            score += 2
        if score > 0 or (numeric and len(columns) == 1):
            candidates.append((score, header))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    return candidates[0][1]


def _plan_for_scope(scope: WorkbookScope, question: str, *, explicit_scope: bool = False) -> Plan:
    operation = _operation(question)
    evidence_score, evidence_reasons = _source_evidence(scope, question)
    filters = _row_filters(scope.rows, question)
    numeric_condition = _numeric_condition(question)
    numeric_column = _choose_column(scope, question, numeric=True) if numeric_condition else None
    numeric_filter = (
        (numeric_column, numeric_condition[0], numeric_condition[1], numeric_condition[2])
        if numeric_column and numeric_condition
        else None
    )
    filtered = _apply_filters(scope.rows, filters, numeric_filter)
    if filters and not filtered:
        return Plan("unavailable", rejection_reason="filters_matched_no_rows")
    has_source_evidence = evidence_score >= 2 or bool(filters) or explicit_scope
    if operation in {"total", "average", "minimum", "maximum"}:
        column = numeric_column or _choose_column(scope, question, numeric=True)
        if not column:
            return Plan("unavailable", rejection_reason="missing_numeric_column")
        if not has_source_evidence or _column_score(column, question) <= 0:
            return Plan("unavailable", rejection_reason="weak_numeric_source_evidence")
        return Plan(operation, value_column=column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if operation == "group":
        value_hint, group_hint = question, question
        if " by " in question.casefold():
            value_hint, group_hint = question.casefold().split(" by ", 1)
        value_column = _choose_column(scope, value_hint, numeric=True)
        group_column = _choose_column(scope, group_hint, preferred_types={"category", "text", "identifier"})
        if not (value_column and group_column and has_source_evidence):
            return Plan("unavailable", rejection_reason="ambiguous_group_plan")
        return Plan("group", value_column=value_column, group_column=group_column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if operation == "distinct":
        column = _choose_column(scope, question, preferred_types={"category", "text", "identifier"})
        if not (column and has_source_evidence):
            return Plan("unavailable", rejection_reason="ambiguous_distinct_column")
        return Plan("distinct", entity_column=column, list_column=column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if operation == "count":
        numeric_quantity = _choose_column(scope, question, numeric=True)
        if numeric_quantity and _column_score(numeric_quantity, question) > 0:
            return Plan("total", value_column=numeric_quantity, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
        entity_column = _choose_column(scope, question, preferred_types={"category", "text", "identifier"})
        generic_record_count = explicit_scope and (_tokens(question) & {"record", "records", "row", "rows"})
        if not (entity_column or generic_record_count):
            return Plan("unavailable", rejection_reason="count_has_no_entity_or_filter")
        if not (filters or numeric_filter or generic_record_count or _column_score(entity_column or "", question) > 0):
            return Plan("unavailable", rejection_reason="count_has_unresolved_filter")
        if not has_source_evidence:
            return Plan("unavailable", rejection_reason="weak_count_source_evidence")
        return Plan("count", entity_column=entity_column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if numeric_filter:
        return Plan("records", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if filters and re.search(r"\ball\b", _normalized(question)):
        return Plan("records", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    list_column = _choose_column(scope, question, preferred_types={"text", "category", "identifier"})
    if list_column:
        if not has_source_evidence:
            return Plan("unavailable", rejection_reason="weak_list_source_evidence")
        return Plan("list", list_column=list_column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if filters or (_tokens(question) & {"record", "records", "row", "rows"}):
        return Plan("records", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    return Plan("unavailable", rejection_reason="no_structured_plan")


def _rank_scopes(scopes: list[WorkbookScope], question: str, *, explicit_scope: bool = False) -> list[tuple[int, WorkbookScope]]:
    ranked = []
    for scope in scopes:
        plan = _plan_for_scope(scope, question, explicit_scope=explicit_scope)
        evidence_score, _ = _source_evidence(scope, question)
        score = evidence_score
        if plan.intent != "unavailable":
            score += 3
        if plan.filters:
            score += 3
        ranked.append((score, scope))
    ranked.sort(key=lambda item: (-item[0], item[1].filename.casefold()))
    return ranked


def _format_number(value: Decimal) -> str:
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _display(value: object) -> str:
    text = " ".join(str(value).replace("\x00", "").split())
    if text.startswith(("=", "+", "-", "@")):
        text = f"'{text}"
    return escape(text, quote=False).replace("|", r"\|")


def _sources(scope: WorkbookScope, rows: list[RowRecord]) -> list[dict[str, object]]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        grouped[row.sheet].append(row.row_number)
    source_type = "csv" if scope.filename.casefold().endswith(".csv") else "excel"
    def ranges(numbers: list[int]) -> list[dict[str, int]]:
        ordered = sorted(set(numbers))
        if not ordered:
            return []
        spans = []
        start = previous = ordered[0]
        for number in ordered[1:]:
            if number == previous + 1:
                previous = number
                continue
            spans.append({"row_start": start, "row_end": previous})
            start = previous = number
        spans.append({"row_start": start, "row_end": previous})
        return spans

    return [
        {
            "document_id": scope.document_id,
            "version_id": scope.version_id,
            "filename": scope.filename,
            "source_type": source_type,
            "source_location": {
                "sheet_name": "CSV" if source_type == "csv" else sheet,
                "row_start": min(numbers),
                "row_end": max(numbers),
                "row_ranges": ranges(numbers),
            },
            "retrieval_score": None,
        }
        for sheet, numbers in grouped.items()
    ]


def _records_table(scope: WorkbookScope, rows: list[RowRecord]) -> str:
    headers = list({header: None for row in rows for header in row.values}.keys())
    lines = [
        "| " + " | ".join([*map(_display, headers), "Source"]) + " |",
        "|" + "|".join("---" for _ in [*headers, "Source"]) + "|",
    ]
    for row in rows[:settings.rag_structured_result_limit]:
        values = [_display(row.values.get(header, "")) for header in headers]
        lines.append("| " + " | ".join([*values, _display(f"{scope.filename}, {row.sheet}, row {row.row_number}")]) + " |")
    return "\n".join(lines)


def _answer_from_rows(scope: WorkbookScope, rows: list[RowRecord], plan: Plan) -> dict[str, object]:
    basis = f"{len(rows)} matching row(s) across {len({row.sheet for row in rows})} sheet(s)"
    answer: str
    contributing_values: list[object] = []
    if plan.intent == "count":
        answer = f"Count: {len(rows):,}. Calculation basis: {basis}."
    elif plan.intent == "distinct" and plan.list_column:
        values = {
            str(row.values.get(plan.list_column)).strip().casefold()
            for row in rows if row.values.get(plan.list_column) is not None and str(row.values.get(plan.list_column)).strip()
        }
        answer = f"Unique {plan.list_column}: {len(values):,}. Calculation basis: {basis}."
    elif plan.intent in {"total", "average", "minimum", "maximum"} and plan.value_column:
        numeric = [(row, _number(row.values.get(plan.value_column))) for row in rows]
        numeric = [(row, value) for row, value in numeric if value is not None]
        if not numeric:
            return _unavailable()
        values = [value for _, value in numeric]
        contributing_values = [row.values.get(plan.value_column) for row, _ in numeric]
        result = {
            "total": sum(values, Decimal(0)),
            "average": sum(values, Decimal(0)) / len(values),
            "minimum": min(values),
            "maximum": max(values),
        }[plan.intent]
        answer = f"{plan.intent.title()} {plan.value_column}: {_format_number(result)}. Calculation basis: {len(numeric)} valid '{plan.value_column}' values."
        rows = [row for row, _ in numeric]
    elif plan.intent == "group" and plan.value_column and plan.group_column:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for row in rows:
            group = str(row.values.get(plan.group_column) or "").strip()
            value = _number(row.values.get(plan.value_column))
            if group and value is not None:
                totals[group] += value
        if not totals:
            return _unavailable()
        answer = (
            f"{plan.value_column} by {plan.group_column}:\n"
            + "\n".join(f"- {_display(group)}: {_format_number(total)}" for group, total in sorted(totals.items()))
        )
    elif plan.list_column:
        values = [
            row.values.get(plan.list_column)
            for row in rows
            if row.values.get(plan.list_column) is not None and str(row.values.get(plan.list_column)).strip()
        ]
        if not values:
            return _unavailable()
        contributing_values = values
        answer = (
            f"Values found ({len(values)}):\n"
            + "\n".join(f"- {_display(value)}" for value in values[:settings.rag_structured_result_limit])
        )
    else:
        answer = f"Matching records ({len(rows)}):\n\n{_records_table(scope, rows)}"
    return {
        "answer": answer,
        "question_type": "structured_analysis",
        "calculation_basis": basis,
        "sources": _sources(scope, rows),
        "grounded": True,
        "_context": {
            "kind": "structured_rows",
            "result_type": plan.intent,
            "value_column": plan.value_column,
            "entity_column": plan.entity_column,
            "display_column": plan.list_column or plan.value_column,
            "group_column": plan.group_column,
            "filters": {key: sorted(value) for key, value in (plan.filters or {}).items()},
            "numeric_filter": (
                {
                    "column": plan.numeric_filter[0],
                    "operator": plan.numeric_filter[1],
                    "left": str(plan.numeric_filter[2]),
                    "right": str(plan.numeric_filter[3]) if plan.numeric_filter[3] is not None else None,
                }
                if plan.numeric_filter
                else None
            ),
            "document_ids": [scope.document_id],
            "version_ids": [scope.version_id],
            "confidence": plan.confidence,
            "contributing_values": contributing_values[:settings.rag_structured_result_limit],
            "row_refs": [
                {"document_id": scope.document_id, "sheet": row.sheet, "row_number": row.row_number}
                for row in rows[:settings.rag_structured_result_limit]
            ],
        },
    }


def _unavailable(reason: str = "no_structured_answer") -> dict[str, object]:
    return {
        "answer": UNAVAILABLE_ANSWER,
        "question_type": "structured_analysis",
        "grounded": False,
        "sources": [],
        "unavailable_reason": reason,
    }
