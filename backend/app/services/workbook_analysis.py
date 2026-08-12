"""Schema-driven structured document analysis for workbook-like tables."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from html import escape
from json import loads
import re

from app.config import settings
from app.database import get_connection
from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services.document_access import READABLE_DOCUMENT_SQL


INTENT_PATTERNS = (
    r"\bhow many\b", r"\bcount\b", r"\bno\b", r"\bnumber of\b", r"\b(total|sum)\b",
    r"\b(average|avg|mean)\b", r"\b(minimum|min|lowest|smallest)\b",
    r"\b(maximum|max|highest|largest)\b", r"\b(unique|distinct)\b",
    r"\b(percent|percentage|rate|overall)\b",
    r"\b(list|show|which|what are|give)\b", r"\b(compare|comparison|versus|vs)\b",
    r"\bgroup\b.+\bby\b",
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
        _answer_employee_profiles(scope, question) is not None
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
    ranked = _rank_scopes(scopes, question, explicit_scope=document_id is not None)
    if not ranked or ranked[0][0] < 2:
        return _unavailable("no_relevant_structured_source")
    scope = ranked[0][1]
    employee_answer = _answer_employee_profiles(scope, question)
    if employee_answer is not None:
        return employee_answer
    plan = _plan_for_scope(scope, question, explicit_scope=document_id is not None)
    if plan.intent == "unavailable":
        return _unavailable(plan.rejection_reason or "invalid_structured_plan")
    rows = _apply_filters(scope.rows, plan.filters or {}, plan.numeric_filter)
    if not rows:
        return _unavailable("structured_filters_matched_no_rows")
    return _answer_from_rows(scope, rows, plan, question)


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
        if len(token) > 4 and token.endswith("ies"):
            tokens.add(f"{token[:-3]}y")
        if len(token) > 3 and token.endswith("s"):
            tokens.add(token[:-1])
        if len(token) > 4 and token.endswith("ed"):
            tokens.add(token[:-1])
            tokens.add(token[:-2])
    return tokens


EMPLOYEE_PROFILE_FIELDS = (
    "Role",
    "Experience Level",
    "Primary Skills",
    "Secondary Skills",
    "Project Allocation",
    "Training Completed",
    "Career Goals",
    "Expected Responsibilities",
)
EMPLOYEE_COMPARE_TOKENS = {"compare", "comparison", "cumulative", "matrix", "skill", "skills", "employee", "employees"}


def _employee_profiles(scope: WorkbookScope) -> list[RowRecord]:
    """Return normalized employee-profile records produced from form-style sheets."""
    profiles = []
    seen_names: set[str] = set()
    for row in scope.rows:
        headers = set(row.values)
        name_key = _name_key(row.values.get("Employee") or row.sheet)
        if "Employee" in headers and headers & set(EMPLOYEE_PROFILE_FIELDS) and name_key not in seen_names:
            profiles.append(row)
            seen_names.add(name_key)
    return profiles


def _name_key(value: object) -> str:
    """Normalize employee names for exact and fuzzy matching."""
    return "".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _resolve_employee_names(question: str, profiles: list[RowRecord]) -> list[RowRecord]:
    """Match employee names case-insensitively and tolerate small misspellings."""
    if not profiles:
        return []
    words = re.findall(r"[a-z0-9]+", question.casefold())
    candidates = []
    for index in range(len(words)):
        for width in (2, 1):
            phrase = "".join(words[index:index + width])
            if phrase:
                candidates.append(phrase)
    matched: list[RowRecord] = []
    seen: set[str] = set()
    for profile in profiles:
        name = str(profile.values.get("Employee") or profile.sheet)
        key = _name_key(name)
        if not key:
            continue
        exact = any(
            candidate == key
            or (len(candidate) >= 4 and candidate in key)
            or (len(key) >= 4 and key in candidate)
            for candidate in candidates
        )
        fuzzy = any(
            len(candidate) >= 4 and SequenceMatcher(None, candidate, key).ratio() >= 0.82
            for candidate in candidates
        )
        if (exact or fuzzy) and key not in seen:
            matched.append(profile)
            seen.add(key)
    return matched


def _is_employee_comparison_question(question: str) -> bool:
    """Detect all-employee skill-matrix comparison requests."""
    tokens = _tokens(question)
    normalized = _normalized(question)
    return (
        bool(tokens & EMPLOYEE_COMPARE_TOKENS)
        and bool(tokens & {"skill", "skills", "employee", "employees", "matrix"})
        and (
            "all employees" in normalized
            or "skill comparison" in normalized
            or "compare" in tokens
            or "comparison" in tokens
            or "cumulative" in tokens
        )
    )


def _is_skills_only_question(question: str) -> bool:
    """Detect when the user asked for skills without profile metadata."""
    tokens = _tokens(question)
    metadata_tokens = {"role", "roles", "experience", "level", "responsibility", "responsibilities", "goals", "training", "allocation"}
    return bool(tokens & {"skill", "skills"}) and not bool(tokens & metadata_tokens)


def _skill_bullets(value: object) -> str:
    """Normalize free-form skill text into Markdown bullets without raw asterisks."""
    text = " ".join(str(value or "").replace("\x00", "").split())
    text = re.sub(r"\s*[*•]\s*", "\n", text)
    # Excel profile cells often contain inline numbered points. Split those
    # into real bullets while keeping decimal experience values like 3.5 intact.
    text = re.sub(r"(?<![\d.])(?:^|\s)\d{1,2}\.\s*(?=[A-Za-z])", "\n", text)
    parts = [
        re.sub(r"^\d{1,2}\.\s*", "", part.strip(" -;\t")).strip()
        for part in re.split(r"\n|;|,(?=\s*[A-Z][A-Za-z ]{2,})", text)
        if part.strip(" -;\t")
    ]
    if not parts:
        return ""
    return "\n".join(f"- {_display(part)}" for part in parts)


def _skill_items(value: object) -> list[str]:
    """Return every readable skill item without hiding evidence behind counts."""
    bullets = _skill_bullets(value)
    items = [
        line.removeprefix("- ").strip()
        for line in bullets.splitlines()
        if line.strip().startswith("- ")
    ]
    if not items and value is not None and str(value).strip():
        items = [_display(value)]
    return items


def _skill_section(label: str, value: object) -> list[str]:
    """Render a skill field as a clear labeled bullet section."""
    items = _skill_items(value)
    if not items:
        return []
    return [f"   **{label}**", *[f"   - {item}" for item in items]]


def _employee_profile_answer(profile: RowRecord) -> str:
    """Format one employee profile without exposing spreadsheet helper columns."""
    name = _display(profile.values.get("Employee") or profile.sheet)
    lines = [name, ""]
    role = profile.values.get("Role")
    experience = profile.values.get("Experience Level")
    if role is not None and str(role).strip():
        lines.append(f"Role: {_display(role)}")
    if experience is not None and str(experience).strip():
        lines.append(f"Experience: {_display(experience)}")
    for field in EMPLOYEE_PROFILE_FIELDS:
        if field in {"Role", "Experience Level"}:
            continue
        value = profile.values.get(field)
        if value is None or not str(value).strip():
            continue
        label = field.replace(" Skills", " skills").replace("Completed", "completed").replace("Goals", "goals").replace("Responsibilities", "responsibilities")
        bullets = _skill_bullets(value) if "Skills" in field else ""
        if not bullets:
            cleaned = re.sub(r"\s*[*•]\s*", " ", str(value)).strip()
            bullets = f"- {_display(cleaned)}"
        lines.extend(["", f"{label}:", bullets or f"- {_display(value)}"])
    return "\n".join(line for line in lines if line != "")


def _employee_skills_answer(profiles: list[RowRecord]) -> str:
    """Render skills-only comparisons without narrow table cells."""
    sections = []
    for index, profile in enumerate(profiles, start=1):
        values = profile.values
        lines = [f"{index}. **{_display(values.get('Employee') or profile.sheet)}**"]
        primary = _skill_section("Primary skills", values.get("Primary Skills"))
        secondary = _skill_section("Secondary skills", values.get("Secondary Skills"))
        if primary:
            lines.extend(["", *primary])
        if secondary:
            lines.extend(["", *secondary])
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _employee_comparison_answer(profiles: list[RowRecord], *, skills_only: bool = False) -> str:
    """Build a compact complete comparison across all profile records."""
    if skills_only:
        return _employee_skills_answer(profiles)
    headers = ["Employee", "Role", "Experience", "Primary skills", "Secondary skills"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for profile in profiles:
        values = profile.values
        lines.append("| " + " | ".join(
            _display(value)
            for value in (
                values.get("Employee") or profile.sheet,
                values.get("Role") or "",
                values.get("Experience Level") or "",
                values.get("Primary Skills") or "",
                values.get("Secondary Skills") or "",
            )
        ) + " |")
    return "\n".join(lines)


def _employee_sources(scope: WorkbookScope, profiles: list[RowRecord]) -> list[dict[str, object]]:
    """Create sheet-level citations for employee profiles while hiding cell scaffolding."""
    sources = _sources(scope, profiles)
    for source in sources:
        location = source.get("source_location")
        if isinstance(location, dict):
            location["hide_row_range"] = True
    return sources


def _answer_employee_profiles(scope: WorkbookScope, question: str) -> dict[str, object] | None:
    """Answer profile-specific employee matrix questions from complete structured rows."""
    profiles = _employee_profiles(scope)
    if not profiles:
        return None
    selected = _resolve_employee_names(question, profiles)
    comparison = _is_employee_comparison_question(question)
    if not selected and not comparison:
        return None
    rows = profiles if comparison and not selected else selected
    if not rows:
        return _unavailable("employee_profiles_matched_no_rows")
    answer = (
        _employee_comparison_answer(rows, skills_only=_is_skills_only_question(question))
        if len(rows) > 1
        else _employee_profile_answer(rows[0])
    )
    all_names = [str(row.values.get("Employee") or row.sheet) for row in profiles]
    selected_names = [str(row.values.get("Employee") or row.sheet) for row in rows]
    provenance = {
        "document_id": scope.document_id,
        "version_id": scope.version_id,
        "workbook_filename": scope.filename,
        "sheets": _row_ranges(rows),
        "columns_used": ["Employee", "Primary Skills", "Secondary Skills"],
        "filters_applied": {},
        "numeric_filter": None,
        "aggregation": "employee_profile",
        "contributing_row_count": len(rows),
    }
    return {
        "answer": answer,
        "question_type": "structured_analysis",
        "calculation_basis": f"{len(rows)} employee profile(s) from {scope.filename}.",
        "sources": _employee_sources(scope, rows),
        "provenance": provenance,
        "grounded": True,
        "_context": {
            "kind": "employee_profiles",
            "result_type": "employee_comparison" if len(rows) > 1 else "employee_profile",
            "skills_only": _is_skills_only_question(question),
            "document_ids": [scope.document_id],
            "version_ids": [scope.version_id],
            "result_plan": provenance,
            "all_employee_names": all_names,
            "selected_employee_names": selected_names,
            "row_refs": [
                {"document_id": scope.document_id, "sheet": row.sheet, "row_number": row.row_number}
                for row in rows
            ],
        },
    }


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not re.fullmatch(r"\(?\s*[-+]?\s*[^\w\s-]*\s*\d[\d,]*(?:\.\d+)?\s*%?\s*\)?", text):
        return None
    negative = text.startswith("(") and text.endswith(")")
    try:
        number = Decimal(re.sub(r"[^\d.\-]", "", text))
        return -number if negative and number > 0 else number
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
    """Parse explicit inclusive/exclusive numeric comparisons with Decimal bounds."""
    normalized = _normalized(question)
    numbers = _question_numbers(question)
    if not numbers:
        return None
    if (
        "between" in normalized
        or re.search(r"\bfrom\b.+\bto\b", normalized)
    ) and len(numbers) >= 2:
        low, high = sorted((numbers[0], numbers[1]))
        # "between" and "from ... to ..." include both endpoints.
        return "between", low, high
    # below/less-than and above/greater-than exclude the boundary.
    if re.search(r"\b(below|under|less than)\b", normalized):
        return "lt", numbers[0], None
    if re.search(r"\b(at most|up to|no more than)\b", normalized):
        return "le", numbers[0], None
    if re.search(r"\b(above|over|greater than|more than)\b", normalized):
        return "gt", numbers[0], None
    if re.search(r"\b(at least|minimum of|not less than)\b", normalized):
        return "ge", numbers[0], None
    return None


def _percentage_condition_for_column(
    condition: tuple[str, Decimal, Decimal | None],
    values: list[object],
    question: str,
) -> tuple[str, Decimal, Decimal | None]:
    """Match percentage-point questions against fraction-stored numeric columns."""
    if not re.search(r"%|\b(?:percent|percentage)\b", question.casefold()):
        return condition
    numeric_values = [abs(value) for value in (_number(value) for value in values) if value is not None]
    if not numeric_values or max(numeric_values) > Decimal(1):
        return condition
    operator, left, right = condition
    return operator, left / Decimal(100), right / Decimal(100) if right is not None else None


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
    matched_sheets = {
        _canonical(row.sheet)
        for row in rows
        if _tokens(row.sheet) & question_tokens
    }
    if matched_sheets:
        filters["__sheet__"] = matched_sheets
    for header, values in _column_values(rows).items():
        matched = set()
        header_requested = bool(_tokens(header) & question_tokens)
        for value in values:
            normalized_value = _normalized(value)
            if not normalized_value:
                continue
            if normalized_value in STOP_WORDS and not header_requested:
                continue
            if _number(value) is not None:
                continue
            value_tokens = _tokens(normalized_value)
            exact_value = f" {normalized_value} " in question_text
            if exact_value or (
                value_tokens
                and any(token in question_tokens for token in value_tokens)
                and value_tokens - question_tokens <= {f"{token}s" for token in question_tokens}
            ):
                matched.add(_canonical(value))
        if matched:
            filters[header] = matched
    directional_value = _requested_direction_value(question)
    if directional_value:
        direction_columns = [
            header
            for header, values in _column_values(rows).items()
            if directional_value in {_canonical(value) for value in values}
        ]
        # Apply an implicit IN/OUT filter only when the workbook supplies one
        # unambiguous matching column; header names are domain-independent.
        if len(direction_columns) == 1:
            filters.setdefault(direction_columns[0], set()).add(directional_value)
    mentioned_months = {
        canonical for alias, canonical in MONTH_ALIASES.items()
        if re.search(rf"\b{re.escape(alias)}\b", _normalized(question))
    }
    if mentioned_months:
        for header, values in _column_values(rows).items():
            if any(any(_value_matches_month(value, month) for month in mentioned_months) for value in values):
                filters.setdefault(header, set()).update(mentioned_months)
    return filters


def _requested_direction_value(question: str) -> str | None:
    """Map common in/out time wording to row values in direction-style tables."""
    normalized = _normalized(question)
    if re.search(r"\bin\s+time\b|\btime\s+in\b", normalized):
        return "in"
    if re.search(r"\bout\s+time\b|\btime\s+out\b", normalized):
        return "out"
    return None


def _requests_time_records(question: str, filters: dict[str, set[str]]) -> bool:
    """Prefer full rows when a question asks for times spread across date columns."""
    if not filters or _requested_direction_value(question) is None:
        return False
    return "time" in _tokens(question)


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
            (
                _canonical(row.sheet) in accepted
                if header == "__sheet__"
                else _canonical(row.values.get(header)) in accepted
                or any(_value_matches_month(row.values.get(header), value) for value in accepted)
            )
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


def _resolve_filter_conflicts(
    rows: list[RowRecord],
    filters: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Drop one contradictory inferred filter when mixed sheet layouts collide."""
    for header in list(filters):
        match = re.fullmatch(r"(.+)\s+\(\d+\)", header)
        if not match or match.group(1) not in filters:
            continue
        # Prefer the primary column when duplicate Excel headers create
        # suffixed variants such as "Rating" and "Rating (2)".
        candidate = {key: value for key, value in filters.items() if key != header}
        if _apply_filters(rows, candidate):
            filters = candidate
    if not filters or _apply_filters(rows, filters):
        return filters
    candidates: list[tuple[int, int, int, dict[str, set[str]]]] = []
    for header in filters:
        if header == "__sheet__":
            continue
        candidate = {key: value for key, value in filters.items() if key != header}
        matched = _apply_filters(rows, candidate)
        if matched:
            candidates.append((len(candidate), len(matched), min(row.row_number for row in matched), candidate))
    if not candidates:
        return filters
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return candidates[0][3]


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
    if re.search(r"\b(compare|comparison|versus|vs)\b", normalized):
        return "comparison"
    if re.search(r"\b(percent|percentage|rate|overall)\b", normalized):
        return "average"
    if re.search(r"\bhow many\b|\bcount\b|\bno\b|\bnumber of\b", normalized):
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
    if _employee_profiles(scope) and (_tokens(question) & EMPLOYEE_COMPARE_TOKENS):
        score += 6
        reasons.append("employee_profile_match")
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
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _candidate_columns(scope: WorkbookScope, *, numeric: bool = False, preferred_types: set[str] | None = None) -> list[str]:
    """Find columns that can safely stand in when document scope is strong."""
    output: list[str] = []
    for header, values in _column_values(scope.rows).items():
        inferred = scope.schema.get(header, {}).get("type", "text")
        if numeric and not any(_number(value) is not None for value in values):
            continue
        if preferred_types and inferred not in preferred_types:
            continue
        output.append(header)
    return sorted(output, key=str.casefold)


def _requested_value_column(scope: WorkbookScope, question: str, filters: dict[str, set[str]]) -> str | None:
    """Choose the requested return column for a filtered row lookup."""
    question_tokens = _tokens(question)
    filter_tokens = set().union(*(_tokens(header) for header in filters if header != "__sheet__"), set())
    candidates: list[tuple[int, int, str]] = []
    for header, values in _column_values(scope.rows).items():
        if header in filters or not any(str(value).strip() for value in values if value is not None):
            continue
        header_tokens = _tokens(header)
        score = _column_score(header, question)
        if "score" in header_tokens and (header_tokens & filter_tokens):
            score += 4
        if "score" in header_tokens and "score" in question_tokens:
            score += 4
        if score > 0:
            candidates.append((score, len(header_tokens), header))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1], item[2].casefold()))
    if len(candidates) > 1 and candidates[0][:2] == candidates[1][:2]:
        return None
    return candidates[0][2]


def _plan_for_scope(scope: WorkbookScope, question: str, *, explicit_scope: bool = False) -> Plan:
    operation = _operation(question)
    evidence_score, evidence_reasons = _source_evidence(scope, question)
    filters = _resolve_filter_conflicts(scope.rows, _row_filters(scope.rows, question))
    numeric_condition = _numeric_condition(question)
    numeric_column = _choose_column(scope, question, numeric=True) if numeric_condition else None
    if numeric_column and numeric_condition:
        operator, left, right = _percentage_condition_for_column(
            numeric_condition,
            _column_values(scope.rows).get(numeric_column, []),
            question,
        )
        numeric_filter = (numeric_column, operator, left, right)
    else:
        numeric_filter = None
    filtered = _apply_filters(scope.rows, filters, numeric_filter)
    if filters and not filtered:
        return Plan("unavailable", rejection_reason="filters_matched_no_rows")
    has_source_evidence = evidence_score >= 2 or bool(filters) or explicit_scope
    if operation in {"total", "average", "minimum", "maximum"}:
        column = numeric_column or _choose_column(scope, question, numeric=True)
        if not column:
            if filters:
                return Plan("records", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
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
        entity_column = _choose_column(scope, question, preferred_types={"category", "text", "identifier"})
        numeric_quantity = _choose_column(scope, question, numeric=True)
        if numeric_quantity and _column_score(numeric_quantity, question) > 0:
            return Plan("total", value_column=numeric_quantity, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
        if (
            entity_column
            and _column_score(entity_column, question) > 0
            and not (_tokens(entity_column) & {"amount", "qty", "quantity", "count", "rate", "total", "reject", "rejection"})
        ):
            return Plan("count", entity_column=entity_column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
        generic_record_count = explicit_scope and (_tokens(question) & {"record", "records", "row", "rows"})
        scoped_entity_count = (
            not entity_column
            and evidence_score >= 4
            and any(reason in evidence_reasons for reason in {"filename_token_match", "sheet_token_match"})
            and len(_candidate_columns(scope, preferred_types={"category", "text", "identifier"})) == 1
        )
        if not (entity_column or generic_record_count):
            if scoped_entity_count:
                entity_column = _candidate_columns(scope, preferred_types={"category", "text", "identifier"})[0]
            else:
                return Plan("unavailable", rejection_reason="count_has_no_entity_or_filter")
        if not (filters or numeric_filter or generic_record_count or scoped_entity_count or _column_score(entity_column or "", question) > 0):
            return Plan("unavailable", rejection_reason="count_has_unresolved_filter")
        if not has_source_evidence:
            return Plan("unavailable", rejection_reason="weak_count_source_evidence")
        return Plan("count", entity_column=entity_column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if operation == "comparison":
        if not has_source_evidence:
            return Plan("unavailable", rejection_reason="weak_comparison_source_evidence")
        return Plan("comparison", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if numeric_filter:
        return Plan("records", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    if _requests_time_records(question, filters):
        return Plan("records", filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
    value_column = _requested_value_column(scope, question, filters) if filters else None
    if value_column:
        return Plan("records", value_column=value_column, filters=filters, numeric_filter=numeric_filter, confidence=evidence_score)
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
    filename = scope.filename.casefold()
    source_type = "csv" if filename.endswith(".csv") else "pdf" if filename.endswith(".pdf") else "excel"
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

    def location(sheet: str, numbers: list[int]) -> dict[str, object]:
        base: dict[str, object] = {
            "sheet_name": "CSV" if source_type == "csv" else sheet,
            "row_start": min(numbers),
            "row_end": max(numbers),
            "row_ranges": ranges(numbers),
        }
        if source_type == "pdf":
            match = re.search(r"\bpage\s+(\d+)\b", sheet.casefold())
            if match:
                page = int(match.group(1))
                base.update({"page_start": page, "page_end": page})
            table = re.search(r"\btable\s+(\d+)\b", sheet.casefold())
            if table:
                base["table_name"] = f"Table {int(table.group(1))}"
        return base

    return [
        {
            "document_id": scope.document_id,
            "version_id": scope.version_id,
            "filename": scope.filename,
            "source_type": source_type,
            "source_location": location(sheet, numbers),
            "retrieval_score": None,
        }
        for sheet, numbers in grouped.items()
    ]


def _row_ranges(rows: list[RowRecord]) -> list[dict[str, object]]:
    """Return compact worksheet row spans without serializing workbook cell values."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        grouped[row.sheet].append(row.row_number)
    spans = []
    for sheet, numbers in grouped.items():
        ordered = sorted(set(numbers))
        start = previous = ordered[0]
        ranges = []
        for number in ordered[1:]:
            if number == previous + 1:
                previous = number
                continue
            ranges.append({"row_start": start, "row_end": previous})
            start = previous = number
        ranges.append({"row_start": start, "row_end": previous})
        spans.append({"sheet_name": sheet, "row_ranges": ranges})
    return spans


def _provenance(scope: WorkbookScope, plan: Plan, rows: list[RowRecord], contributing_row_count: int) -> dict[str, object]:
    """Describe the structured calculation inputs without exposing row contents."""
    columns = [
        column for column in (plan.value_column, plan.entity_column, plan.list_column, plan.group_column)
        if column
    ]
    columns.extend(key for key in (plan.filters or {}) if key != "__sheet__")
    if plan.numeric_filter:
        columns.append(plan.numeric_filter[0])
    return {
        "document_id": scope.document_id,
        "version_id": scope.version_id,
        "workbook_filename": scope.filename,
        "sheets": _row_ranges(rows),
        "columns_used": list(dict.fromkeys(columns)),
        "filters_applied": {
            key: sorted(value) for key, value in (plan.filters or {}).items()
        },
        "numeric_filter": (
            {
                "column": plan.numeric_filter[0],
                "operator": plan.numeric_filter[1],
                "left": str(plan.numeric_filter[2]),
                "right": str(plan.numeric_filter[3]) if plan.numeric_filter[3] is not None else None,
            }
            if plan.numeric_filter else None
        ),
        "aggregation": plan.intent,
        "contributing_row_count": contributing_row_count,
    }


def _record_headers(rows: list[RowRecord], plan: Plan) -> list[str]:
    """Choose only the columns needed for a readable row-style answer."""
    if plan.list_column:
        return [plan.list_column]
    if plan.value_column and plan.intent == "records":
        return [plan.value_column]
    if plan.intent == "records" and len(rows) == 1 and plan.filters:
        filter_headers = [header for header in rows[0].values if header in plan.filters]
        if len(filter_headers) == 1:
            ordered = list(rows[0].values)
            for header in ordered[ordered.index(filter_headers[0]) + 1:]:
                if rows[0].values.get(header) is not None and str(rows[0].values.get(header)).strip():
                    return [header]
        headers = [
            header for header in rows[0].values
            if header not in plan.filters and rows[0].values.get(header) is not None
        ]
        if headers:
            return headers
    return list({header: None for row in rows for header in row.values}.keys())


def _records_table(rows: list[RowRecord], plan: Plan) -> str:
    headers = _record_headers(rows, plan)
    lines = [
        "| " + " | ".join(map(_display, headers)) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows[:settings.rag_structured_result_limit]:
        values = [_display(row.values.get(header, "")) for header in headers]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _single_row_answer(rows: list[RowRecord], plan: Plan) -> str | None:
    """Return a direct value answer when the question asks one row for one field."""
    headers = _record_headers(rows, plan)
    if len(rows) != 1 or len(headers) != 1:
        return None
    value = rows[0].values.get(headers[0])
    if value is None or not str(value).strip():
        return None
    label: object = headers[0]
    filter_headers = [header for header in (plan.filters or {}) if header != "__sheet__"]
    if str(headers[0]).startswith("Column ") and len(filter_headers) == 1:
        label = rows[0].values.get(filter_headers[0]) or headers[0]
    return f"{_display(label)}: {_display(value)}"


def _score_scale_rows(rows: list[RowRecord], value_column: str) -> list[RowRecord]:
    """Prefer rating-scale rows over occurrence rows for score label lookups."""
    value_tokens = _tokens(value_column)
    if "score" not in value_tokens:
        return []
    scale_rows = []
    for row in rows:
        headers = set(row.values)
        range_headers = {
            header for header in headers
            if _tokens(header) & {"minimum", "maximum", "min", "max"}
            and row.values.get(header) is not None
            and str(row.values.get(header)).strip()
        }
        if len(range_headers) >= 2:
            scale_rows.append(row)
    return scale_rows


def _follow_up_display_column(scope: WorkbookScope, plan: Plan, question: str) -> str | None:
    """Choose an explicitly referenced non-numeric entity column for result replay."""
    if plan.list_column or plan.entity_column:
        return plan.list_column or plan.entity_column
    excluded = {plan.value_column}
    if plan.numeric_filter:
        excluded.add(plan.numeric_filter[0])
    candidates = [
        header
        for header, values in _column_values(scope.rows).items()
        if header not in excluded
        and not all(_number(value) is not None for value in values if value is not None)
        and _column_score(header, question) > 0
    ]
    return candidates[0] if len(candidates) == 1 else None


def _answer_from_rows(
    scope: WorkbookScope,
    rows: list[RowRecord],
    plan: Plan,
    question: str,
) -> dict[str, object]:
    basis = f"{len(rows)} matching row(s) across {len({row.sheet for row in rows})} sheet(s)"
    answer: str
    contributing_values: list[object] = []
    if plan.intent == "count":
        # Retain the counted entities so a later reference can display the same set.
        if plan.entity_column:
            contributing_values = [
                row.values.get(plan.entity_column)
                for row in rows
                if row.values.get(plan.entity_column) is not None
                and str(row.values.get(plan.entity_column)).strip()
            ]
        if (
            plan.entity_column
            and "employee" in _tokens(plan.entity_column)
            and "employee" in _tokens(scope.filename + " " + plan.entity_column)
        ):
            values = {
                str(row.values.get(plan.entity_column)).strip().casefold()
                for row in rows
                if row.values.get(plan.entity_column) is not None
                and str(row.values.get(plan.entity_column)).strip()
                and _number(row.values.get(plan.entity_column)) is None
            }
            answer = f"Count: {len(values):,}. Calculation basis: distinct employee-name values in '{plan.entity_column}'."
        else:
            answer = f"Count: {len(rows):,}. Calculation basis: {basis}."
    elif plan.intent == "distinct" and plan.list_column:
        rows = [
            row for row in rows
            if row.values.get(plan.list_column) is not None and str(row.values.get(plan.list_column)).strip()
        ]
        values = {
            str(row.values.get(plan.list_column)).strip().casefold()
            for row in rows
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
        contributing_rows = []
        for row in rows:
            group = str(row.values.get(plan.group_column) or "").strip()
            value = _number(row.values.get(plan.value_column))
            if group and value is not None:
                totals[group] += value
                contributing_rows.append(row)
        if not totals:
            return _unavailable()
        answer = (
            f"{plan.value_column} by {plan.group_column}:\n"
            + "\n".join(f"- {_display(group)}: {_format_number(total)}" for group, total in sorted(totals.items()))
        )
        rows = contributing_rows
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
        if plan.intent == "records" and plan.value_column:
            rows = [
                row for row in rows
                if row.values.get(plan.value_column) is not None
                and str(row.values.get(plan.value_column)).strip()
            ]
            scale_rows = _score_scale_rows(rows, plan.value_column)
            if scale_rows:
                rows = scale_rows
            if not rows:
                return _unavailable()
        answer = _single_row_answer(rows, plan)
        if answer is None:
            answer = f"Matching records ({len(rows)}):\n\n{_records_table(rows, plan)}"
    display_column = _follow_up_display_column(scope, plan, question)
    if display_column:
        # Follow-up replay needs the displayed entity set, not aggregate operands.
        contributing_values = [
            row.values.get(display_column)
            for row in rows
            if row.values.get(display_column) is not None
            and str(row.values.get(display_column)).strip()
        ]
    return {
        "answer": answer,
        "question_type": "structured_analysis",
        "calculation_basis": basis,
        "sources": _sources(scope, rows),
        "provenance": _provenance(scope, plan, rows, len(rows)),
        "grounded": True,
        "_context": {
            "kind": "structured_rows",
            "result_type": plan.intent,
            "value_column": plan.value_column,
            "entity_column": plan.entity_column,
            "display_column": display_column or plan.value_column,
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
            "result_plan": _provenance(scope, plan, rows, len(rows)),
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
