"""Deterministic, owner-scoped analysis over structured workbook rows."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from json import loads
from html import escape
import re

from app.database import get_connection
from app.services.document_access import READABLE_DOCUMENT_SQL


ANALYTICAL_PATTERNS = (
    r"\bhow many\b",
    r"\bcount\b",
    r"\b(total|sum)\b",
    r"\b(average|avg|mean)\b",
    r"\b(minimum|lowest|smallest)\b",
    r"\b(maximum|highest|largest)\b",
    r"\b(unique|distinct)\b",
    r"\b(summarize|summary|group|breakdown)\b.*\bby\b",
    r"\blist all\b",
    r"\bshow all\b",
    r"\bbetween\b.+\band\b",
    r"\bfrom\b.+\bto\b",
    r"\b(below|under|less than|up to|at most|above|over|greater than|at least)\b",
    r"\blist\b.*\b(pending|open|closed|overdue|active|inactive|approved|rejected)\b",
)

ALIASES = (
    {
        "cost", "costs", "expense", "expenses", "amount", "spend", "spending",
        "price", "priced", "prices", "pricing", "value",
    },
    {"employee", "employees", "staff", "person", "people", "resource", "resources", "name"},
    {"vendor", "vendors", "supplier", "suppliers"},
    {"product", "products", "item", "items", "sku", "skus"},
    {"department", "departments", "division", "divisions", "team", "teams"},
    {"invoice", "invoices", "bill", "bills"},
    {"project", "projects", "initiative", "initiatives"},
    {"status", "state"},
    {"customer", "customers", "client", "clients"},
)

MONTH_ALIASES = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}
MONTH_LABELS = {
    f"{month:02d}": label
    for month, label in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}


@dataclass
class RowRecord:
    sheet: str
    row_number: int
    values: dict[str, object]


@dataclass
class WorkbookScope:
    document_id: int
    filename: str
    rows: list[RowRecord]
    sheet_names: list[str]


def is_analytical_question(question: str) -> bool:
    normalized = " ".join(question.casefold().split())
    return any(re.search(pattern, normalized) for pattern in ANALYTICAL_PATTERNS)


def is_structured_lookup_question(
    question: str,
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> bool:
    """Return whether accessible structured rows can answer a direct lookup."""
    return any(
        _lookup_plan(scope.rows, question) is not None
        for scope in _load_scopes(owner_id, collection_id, document_id)
    )


def has_structured_workbook(
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> bool:
    with get_connection() as connection:
        return connection.execute(
            """
            SELECT 1
            FROM documents d
            JOIN workbook_sheets ws ON ws.content_id = d.content_id
            WHERE d.owner_id = ? AND ws.owner_id = ?
              AND (? IS NULL OR d.collection_id = ?)
              AND (? IS NULL OR d.id = ?)
            LIMIT 1
            """,
            (owner_id, owner_id, collection_id, collection_id, document_id, document_id),
        ).fetchone() is not None


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _normalized_text(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _canonical_scalar(value: object) -> str:
    normalized = _normalized_text(value)
    return MONTH_ALIASES.get(normalized, normalized)


def _question_mentions_value(question: str, value: object) -> bool:
    normalized_value = _normalized_text(value)
    if not normalized_value:
        return False
    normalized_question = _normalized_text(question)
    canonical_value = MONTH_ALIASES.get(normalized_value)
    if canonical_value is not None:
        return any(
            canonical == canonical_value and re.search(
                rf"\b{re.escape(alias)}\b", normalized_question
            )
            for alias, canonical in MONTH_ALIASES.items()
        )
    return bool(re.search(
        rf"(?:^|\s){re.escape(normalized_value)}(?:\s|$)",
        normalized_question,
    ))


def _row_filters(
    rows: list[RowRecord],
    question: str,
) -> dict[str, set[str]]:
    """Find exact categorical values named by the question, grouped by column."""
    columns = _column_values(rows)
    filters: dict[str, set[str]] = {}
    for header, values in columns.items():
        matches = {
            _canonical_scalar(value)
            for value in values
            if not isinstance(value, (int, float, Decimal, bool))
            and _question_mentions_value(question, value)
        }
        if matches:
            filters[header] = matches
    normalized_question = _normalized_text(question)
    mentioned_months = {
        canonical
        for alias, canonical in MONTH_ALIASES.items()
        if re.search(rf"\b{re.escape(alias)}\b", normalized_question)
    }
    if mentioned_months:
        for header in columns:
            if _tokens(header) & {"month", "months"}:
                filters.setdefault(header, set()).update(mentioned_months)
    return filters


def _apply_row_filters(
    rows: list[RowRecord],
    filters: dict[str, set[str]],
) -> list[RowRecord]:
    if not filters:
        return rows
    return [
        row
        for row in rows
        if all(
            _canonical_scalar(row.values.get(header)) in accepted
            for header, accepted in filters.items()
        )
    ]


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        result = Decimal(cleaned)
        return -result if negative and result > 0 else result
    except InvalidOperation:
        return None


def _cell_number(value: object) -> Decimal | None:
    """Parse real numeric cells without treating identifiers like RT-200 as numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return _number(value)
    text = str(value).strip()
    if not re.fullmatch(
        r"[$€£₹]?\s*\(?-?\d[\d,]*(?:\.\d+)?\)?",
        text,
    ):
        return None
    return _number(text)


def _quantity_number(value: object) -> Decimal | None:
    """Parse query quantities, including common Indian and international units."""
    number = _number(value)
    if number is None:
        return None
    normalized = _normalized_text(value)
    multipliers = (
        ({"crore", "crores", "cr"}, Decimal("10000000")),
        ({"lakh", "lakhs", "lac", "lacs", "lak", "laks"}, Decimal("100000")),
        ({"million", "millions", "mn"}, Decimal("1000000")),
        ({"thousand", "thousands", "k"}, Decimal("1000")),
    )
    tokens = set(normalized.split())
    for aliases, multiplier in multipliers:
        if tokens & aliases:
            return number * multiplier
    return number


def _has_monetary_limit(question: str) -> bool:
    normalized = _normalized_text(question)
    tokens = set(normalized.split())
    return (
        bool(tokens & {
            "lakh", "lakhs", "lac", "lacs", "lak", "laks",
            "crore", "crores", "cr", "million", "millions", "mn",
        })
        or any(symbol in question for symbol in ("₹", "$", "€", "£"))
    )


def _numeric_range(question: str) -> tuple[Decimal, Decimal] | None:
    normalized = " ".join(question.split())
    match = re.search(
        r"\bbetween\b(.+?)\band\b(.+?)(?:[?.!]|$)",
        normalized,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r"\bfrom\b(.+?)\bto\b(.+?)(?:[?.!]|$)",
            normalized,
            flags=re.IGNORECASE,
        )
    if match is None:
        return None
    lower = _quantity_number(match.group(1))
    upper = _quantity_number(match.group(2))
    if lower is None or upper is None:
        return None
    return (min(lower, upper), max(lower, upper))


def _numeric_bound(question: str) -> tuple[str, Decimal] | None:
    """Parse one-sided comparisons such as below, at most, above, or at least."""
    normalized = " ".join(question.split())
    quantity = (
        r"([(-]?\d[\d,.\s]*"
        r"(?:crores?|cr|lakhs?|lacs?|laks?|millions?|mn|thousands?|k)?)"
    )
    patterns = (
        ("lt", rf"\b(?:below|under|less\s+than)\b[^\d(-]*{quantity}"),
        ("lte", rf"\b(?:up\s+to|at\s+most|no\s+more\s+than)\b[^\d(-]*{quantity}"),
        ("gt", rf"\b(?:above|over|greater\s+than|more\s+than)\b[^\d(-]*{quantity}"),
        ("gte", rf"\b(?:at\s+least|no\s+less\s+than)\b[^\d(-]*{quantity}"),
    )
    for operator, pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match is not None and (
            value := _quantity_number(match.group(1))
        ) is not None:
            return operator, value
    return None


def _load_scopes(
    owner_id: int,
    collection_id: int | None,
    document_id: int | None,
) -> list[WorkbookScope]:
    with get_connection() as connection:
        user = connection.execute(
            "SELECT organization_id, role FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
        if user is None:
            return []
        documents = connection.execute(
            f"""
            SELECT DISTINCT d.id, d.display_filename, d.content_id
            FROM documents d
            JOIN workbook_sheets ws ON ws.content_id = d.content_id
            WHERE {READABLE_DOCUMENT_SQL}
              AND ws.organization_id = ?
              AND (? IS NULL OR d.collection_id = ?)
              AND (? IS NULL OR d.id = ?)
            ORDER BY d.id
            """,
            (
                user["organization_id"], owner_id, owner_id,
                user["organization_id"],
                collection_id, collection_id, document_id, document_id,
            ),
        ).fetchall()
        scopes: list[WorkbookScope] = []
        for document in documents:
            sheets = connection.execute(
                """
                SELECT id, name
                FROM workbook_sheets
                WHERE content_id = ? AND organization_id = ? AND status = 'processed'
                ORDER BY sheet_index
                """,
                (document["content_id"], user["organization_id"]),
            ).fetchall()
            rows: list[RowRecord] = []
            for sheet in sheets:
                stored_rows = connection.execute(
                    """
                    SELECT row_number, values_json
                    FROM workbook_rows
                    WHERE sheet_id = ? AND content_id = ? AND organization_id = ?
                    ORDER BY row_number
                    """,
                    (sheet["id"], document["content_id"], user["organization_id"]),
                ).fetchall()
                rows.extend(
                    RowRecord(
                        sheet=str(sheet["name"]),
                        row_number=int(row["row_number"]),
                        values=loads(str(row["values_json"])),
                    )
                    for row in stored_rows
                )
            scopes.append(
                WorkbookScope(
                    document_id=int(document["id"]),
                    filename=str(document["display_filename"]),
                    rows=rows,
                    sheet_names=[str(sheet["name"]) for sheet in sheets],
                )
            )
    return scopes


def _choose_workbook(scopes: list[WorkbookScope], question: str) -> WorkbookScope | str | None:
    if not scopes:
        return None
    if len(scopes) == 1:
        return scopes[0]
    normalized = question.casefold()
    named = [
        scope
        for scope in scopes
        if scope.filename.casefold() in normalized
        or scope.filename.rsplit(".", 1)[0].casefold() in normalized
    ]
    if len(named) == 1:
        return named[0]
    filenames = ", ".join(scope.filename for scope in scopes)
    return f"Please select or name one workbook to analyze: {filenames}."


def _sheet_filter(scope: WorkbookScope, question: str) -> tuple[list[RowRecord], list[str]]:
    normalized = question.casefold()
    selected: list[str] = []
    for name in scope.sheet_names:
        sheet = name.casefold()
        explicitly_labeled = bool(re.search(
            rf"\b(?:sheet|tab)\s*[:\-]?\s*{re.escape(sheet)}\b",
            normalized,
        ))
        descriptive_name = len(_tokens(name)) > 1 and sheet in normalized
        if explicitly_labeled or descriptive_name:
            selected.append(name)
    if not selected:
        return scope.rows, scope.sheet_names
    selected_keys = {name.casefold() for name in selected}
    return [row for row in scope.rows if row.sheet.casefold() in selected_keys], selected


def _column_values(rows: list[RowRecord]) -> dict[str, list[object]]:
    values: dict[str, list[object]] = defaultdict(list)
    for row in rows:
        for header, value in row.values.items():
            if value is not None and str(value).strip():
                values[header].append(value)
    return values


def _column_score(header: str, question: str) -> int:
    header_tokens = _tokens(header)
    question_tokens = _tokens(question)
    score = len(header_tokens & question_tokens) * 5
    normalized_header = " ".join(re.findall(r"[a-z0-9]+", header.casefold()))
    if normalized_header and normalized_header in " ".join(question.casefold().split()):
        score += 10
    for group in ALIASES:
        if header_tokens & group and question_tokens & group:
            score += 4
    return score


def _resolve_column(
    rows: list[RowRecord],
    question: str,
    *,
    numeric: bool = False,
    hint: str | None = None,
) -> tuple[str | None, str | None]:
    columns = _column_values(rows)
    subject = hint or question
    candidates: list[tuple[int, str]] = []
    for header, values in columns.items():
        if numeric and not any(_cell_number(value) is not None for value in values):
            continue
        score = _column_score(header, subject)
        if score > 0:
            candidates.append((score, header))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    top_score = candidates[0][0]
    tied = [header for score, header in candidates if score == top_score]
    if len(tied) > 1:
        return None, f"Which column should I use: {', '.join(tied)}?"
    return candidates[0][1], None


def _lookup_plan(
    rows: list[RowRecord],
    question: str,
) -> tuple[str, dict[str, set[str]]] | None:
    """Resolve a requested output column plus one or more exact row filters."""
    filters = _row_filters(rows, question)
    if not filters:
        return None
    candidates: list[tuple[int, str]] = []
    for header in _column_values(rows):
        if header in filters:
            continue
        score = _column_score(header, question)
        if score > 0:
            candidates.append((score, header))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    top_score = candidates[0][0]
    if sum(score == top_score for score, _ in candidates) != 1:
        return None
    return candidates[0][1], filters


def _operation(question: str) -> str:
    normalized = question.casefold()
    if re.search(r"\b(summarize|summary|group|breakdown)\b.*\bby\b", normalized):
        return "group"
    if re.search(r"\b(unique|distinct)\b", normalized):
        return "distinct"
    if re.search(r"\b(average|avg|mean)\b", normalized):
        return "average"
    if re.search(r"\b(total|sum)\b", normalized):
        return "total"
    if re.search(r"\b(maximum|highest|largest)\b", normalized):
        return "maximum"
    if re.search(r"\b(minimum|lowest|smallest)\b", normalized):
        return "minimum"
    if re.search(r"\bhow many\b|\bcount\b", normalized):
        return "count"
    return "list"


def _format_number(value: Decimal) -> str:
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _display(value: object) -> str:
    """Render workbook-controlled labels as inert Markdown text."""
    text = " ".join(str(value).replace("\x00", "").split())
    if text.startswith(("=", "+", "-", "@")):
        text = f"'{text}"
    return escape(text, quote=False).replace("|", r"\|")


def _records_table(
    scope: WorkbookScope,
    rows: list[RowRecord],
) -> str:
    headers: list[str] = []
    for row in rows:
        for header in row.values:
            if header not in headers:
                headers.append(header)
    display_rows = rows[:100]
    table_headers = [*headers, "Source"]
    lines = [
        "| " + " | ".join(_display(header) for header in table_headers) + " |",
        "|" + "|".join("---" for _ in table_headers) + "|",
    ]
    for row in display_rows:
        values = []
        for header in headers:
            value = row.values.get(header)
            numeric = _cell_number(value)
            values.append(
                _format_number(numeric)
                if numeric is not None
                else _display("" if value is None else value)
            )
        source = (
            f"{scope.filename}, row {row.row_number}"
            if scope.filename.casefold().endswith(".csv")
            else f"{scope.filename}, {row.sheet}, row {row.row_number}"
        )
        lines.append(
            "| " + " | ".join([*values, _display(source)]) + " |"
        )
    if len(rows) > 100:
        lines.append("")
        lines.append(f"_{len(rows) - 100} additional matching rows omitted._")
    return "\n".join(lines)


def _column_letter(index: int) -> str:
    output = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _sources(
    scope: WorkbookScope,
    sheets: list[str],
    rows: list[RowRecord] | None = None,
    value_column: str | None = None,
):
    row_lookup: dict[str, list[int]] = {sheet: [] for sheet in sheets}
    for row in rows or []:
        row_lookup.setdefault(row.sheet, []).append(row.row_number)
    source_type = "csv" if scope.filename.casefold().endswith(".csv") else "excel"
    column_index = None
    if value_column and rows:
        headers = list(rows[0].values)
        if value_column in headers:
            column_index = headers.index(value_column) + 1
    return [
        {
            "document_id": scope.document_id,
            "filename": scope.filename,
            "source_type": source_type,
            "source_location": {
                "sheet_name": "CSV" if source_type == "csv" else sheet,
                "row_start": (
                    min(row_lookup[sheet]) if row_lookup.get(sheet) else None
                ),
                "row_end": (
                    max(row_lookup[sheet]) if row_lookup.get(sheet) else None
                ),
                **(
                    {
                        "cell_range": (
                            f"{_column_letter(column_index)}"
                            f"{min(row_lookup[sheet])}:"
                            f"{_column_letter(column_index)}"
                            f"{max(row_lookup[sheet])}"
                        )
                    }
                    if column_index is not None and row_lookup.get(sheet)
                    else {}
                ),
            },
            # Deprecated response aliases retained for older clients.
            "sheet_name": "CSV" if source_type == "csv" else sheet,
            "row_number": (
                min(row_lookup[sheet]) if row_lookup.get(sheet) else None
            ),
            "retrieval_score": None,
        }
        for sheet in sheets
    ]


def _filter_label(filters: dict[str, set[str]]) -> str:
    return ", ".join(
        f"{_display(header)}={_display('/'.join(
            MONTH_LABELS.get(value, value) for value in sorted(values)
        ))}"
        for header, values in filters.items()
    )


def _structured_lookup_response(
    scopes: list[WorkbookScope],
    question: str,
) -> dict[str, object] | None:
    matches: list[tuple[WorkbookScope, RowRecord, str, dict[str, set[str]]]] = []
    planned_scopes = 0
    matched_filters: list[tuple[WorkbookScope, str, dict[str, set[str]]]] = []
    for scope in scopes:
        plan = _lookup_plan(scope.rows, question)
        if plan is None:
            continue
        planned_scopes += 1
        value_column, filters = plan
        matched_filters.append((scope, value_column, filters))
        for row in _apply_row_filters(scope.rows, filters):
            value = row.values.get(value_column)
            if value is not None and str(value).strip():
                matches.append((scope, row, value_column, filters))
    if not planned_scopes:
        return None
    if not matches:
        sources = [
            source
            for scope, value_column, _ in matched_filters
            for source in _sources(
                scope,
                scope.sheet_names,
                scope.rows,
                value_column=value_column,
            )
        ]
        return {
            "answer": "No matching structured rows were found in the accessible workbooks.",
            "question_type": "structured_lookup",
            "calculation_basis": (
                f"Exhaustively checked {planned_scopes} matching workbook(s)."
            ),
            "sources": sources,
            "grounded": bool(sources),
            "matched_document_count": planned_scopes,
            "matched_row_count": 0,
        }

    lines = [
        "| File | Sheet | Match | Field | Value | Location |",
        "|---|---|---|---|---:|---|",
    ]
    sources: list[dict[str, object]] = []
    grouped: dict[tuple[int, str], list[RowRecord]] = defaultdict(list)
    grouped_columns: dict[tuple[int, str], str] = {}
    for scope, row, value_column, filters in matches:
        key = (scope.document_id, row.sheet)
        grouped[key].append(row)
        grouped_columns[key] = value_column
    operation = _operation(question)
    if operation in {"total", "average", "maximum", "minimum"}:
        aggregate_groups: dict[
            tuple[int, str, str],
            list[tuple[WorkbookScope, RowRecord]],
        ] = defaultdict(list)
        for scope, row, value_column, filters in matches:
            aggregate_groups[
                (scope.document_id, value_column, _filter_label(filters))
            ].append((scope, row))
        for (_, value_column, filter_text), entries in aggregate_groups.items():
            numeric = [
                value
                for _, row in entries
                if (value := _cell_number(row.values.get(value_column))) is not None
            ]
            if not numeric:
                continue
            result = {
                "total": sum(numeric, Decimal(0)),
                "average": sum(numeric, Decimal(0)) / len(numeric),
                "maximum": max(numeric),
                "minimum": min(numeric),
            }[operation]
            scope = entries[0][0]
            sheets = ", ".join(sorted({row.sheet for _, row in entries}))
            lines.append(
                f"| {_display(scope.filename)} | {_display(sheets)} | "
                f"{filter_text} | {_display(operation.title() + ' ' + value_column)} | "
                f"{_format_number(result)} | {len(entries)} matching row(s) |"
            )
    else:
        for scope, row, value_column, filters in matches:
            value = _cell_number(row.values.get(value_column))
            rendered = (
                _format_number(value)
                if value is not None
                else _display(row.values.get(value_column))
            )
            headers = list(row.values)
            column_index = headers.index(value_column) + 1
            cell = f"{_column_letter(column_index)}{row.row_number}"
            lines.append(
                f"| {_display(scope.filename)} | {_display(row.sheet)} | "
                f"{_filter_label(filters)} | {_display(value_column)} | "
                f"{rendered} | row {row.row_number}, {cell} |"
            )
    for scope in scopes:
        for (document_id, sheet), selected_rows in grouped.items():
            if document_id != scope.document_id:
                continue
            sources.extend(_sources(
                scope,
                [sheet],
                selected_rows,
                value_column=grouped_columns[(document_id, sheet)],
            ))
    document_count = len({scope.document_id for scope, _, _, _ in matches})
    return {
        "answer": "\n".join(lines),
        "question_type": (
            "structured_analysis"
            if operation in {"total", "average", "maximum", "minimum"}
            else "structured_lookup"
        ),
        "calculation_basis": (
            f"{len(matches)} matching row(s) across {document_count} workbook(s)."
        ),
        "sources": sources,
        "grounded": bool(sources),
        "matched_document_count": document_count,
        "matched_row_count": len(matches),
    }


def _response(
    scope: WorkbookScope,
    answer: str,
    sheets: list[str],
    basis: str,
    rows: list[RowRecord] | None = None,
) -> dict[str, object]:
    sources = _sources(scope, sheets, rows)
    return {
        "answer": answer,
        "question_type": "analytical",
        "calculation_basis": basis,
        "sources": sources,
        "grounded": bool(sources),
    }


def analyze_workbook_question(
    question: str,
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> dict[str, object]:
    """Answer an analytical question without sampling vector-search results."""
    scopes = _load_scopes(owner_id, collection_id, document_id)
    lookup = _structured_lookup_response(scopes, question)
    if lookup is not None:
        return lookup
    chosen = _choose_workbook(scopes, question)
    if chosen is None:
        return {
            "answer": "No accessible structured workbook was found in the selected scope.",
            "question_type": "analytical",
            "calculation_basis": "No owned workbook rows were available.",
            "sources": [],
            "grounded": False,
        }
    if isinstance(chosen, str):
        return {
            "answer": chosen,
            "question_type": "clarification",
            "calculation_basis": "Multiple workbooks are in scope.",
            "sources": [],
            "grounded": False,
        }

    scope = chosen
    rows, relevant_sheets = _sheet_filter(scope, question)
    if not rows:
        return _response(
            scope,
            "The selected worksheet scope contains no structured data rows.",
            relevant_sheets,
            "0 non-empty data rows.",
        )

    operation = _operation(question)
    filters = _row_filters(rows, question)
    if filters:
        rows = _apply_row_filters(rows, filters)
        if not rows:
            return _response(
                scope,
                "No matching structured rows were found in the selected workbook.",
                relevant_sheets,
                f"0 rows matched {_filter_label(filters)}.",
            )
    sheet_count = len({row.sheet for row in rows})
    scope_basis = f"{len(rows)} data rows across {sheet_count} worksheet(s)"

    if operation == "count":
        return _response(
            scope,
            f"Count: {len(rows):,}. Calculation basis: {scope_basis}.",
            relevant_sheets,
            scope_basis,
        )

    if operation == "distinct":
        column, clarification = _resolve_column(rows, question)
        if clarification:
            return _response(scope, clarification, relevant_sheets, scope_basis)
        if column is None:
            return _response(
                scope,
                "The workbook does not contain an identifiable column for that distinct count.",
                relevant_sheets,
                scope_basis,
            )
        values = {
            str(row.values.get(column)).strip().casefold()
            for row in rows
            if row.values.get(column) is not None and str(row.values.get(column)).strip()
        }
        basis = f"{len(rows)} rows; distinct non-empty values in '{column}'"
        return _response(
            scope,
            f"Unique {column}: {len(values):,}. Calculation basis: {basis}.",
            relevant_sheets,
            basis,
        )

    if operation in {"total", "average", "maximum", "minimum", "group"}:
        value_hint = question.split(" by ", 1)[0] if operation == "group" else question
        value_column, clarification = _resolve_column(
            rows,
            question,
            numeric=True,
            hint=value_hint,
        )
        if clarification:
            return _response(scope, clarification, relevant_sheets, scope_basis)
        if value_column is None:
            return _response(
                scope,
                "The workbook does not contain an identifiable numeric column for this calculation.",
                relevant_sheets,
                scope_basis,
            )
        numeric_rows = [
            (row, _cell_number(row.values.get(value_column)))
            for row in rows
            if _cell_number(row.values.get(value_column)) is not None
        ]
        if not numeric_rows:
            return _response(
                scope,
                f"No valid numeric values were found in '{value_column}'.",
                relevant_sheets,
                scope_basis,
            )

        if operation == "total":
            result = sum((value for _, value in numeric_rows), Decimal(0))
            basis = f"{len(numeric_rows)} valid '{value_column}' values across {sheet_count} worksheet(s)"
            return _response(
                scope,
                f"Total {value_column}: {_format_number(result)}. Calculation basis: {basis}.",
                relevant_sheets,
                basis,
            )
        if operation == "average":
            result = sum((value for _, value in numeric_rows), Decimal(0)) / len(numeric_rows)
            basis = f"{len(numeric_rows)} valid '{value_column}' values across {sheet_count} worksheet(s)"
            return _response(
                scope,
                f"Average {value_column}: {_format_number(result)}. Calculation basis: {basis}.",
                relevant_sheets,
                basis,
            )
        if operation in {"maximum", "minimum"}:
            selected_row, result = (
                max(numeric_rows, key=lambda item: item[1])
                if operation == "maximum"
                else min(numeric_rows, key=lambda item: item[1])
            )
            labels = [
                f"{_display(header)}: {_display(value)}"
                for header, value in selected_row.values.items()
                if (
                    header != value_column
                    and value is not None
                    and _cell_number(value) is None
                )
            ][:2]
            label = f" ({'; '.join(labels)})" if labels else ""
            basis = f"{len(numeric_rows)} valid '{value_column}' values"
            return _response(
                scope,
                f"{operation.title()} {value_column}: {_format_number(result)}{label}. "
                f"Source: {scope.filename} → {selected_row.sheet}, row {selected_row.row_number}.",
                [selected_row.sheet],
                basis,
                [selected_row],
            )

        group_hint = question.casefold().split(" by ", 1)[1] if " by " in question.casefold() else question
        group_column, clarification = _resolve_column(rows, group_hint)
        if clarification:
            return _response(scope, clarification, relevant_sheets, scope_basis)
        if group_column is None or group_column == value_column:
            return _response(
                scope,
                "The workbook does not contain an identifiable grouping column.",
                relevant_sheets,
                scope_basis,
            )
        totals: dict[str, Decimal] = defaultdict(Decimal)
        included = 0
        for row, value in numeric_rows:
            group = row.values.get(group_column)
            if group is None or not str(group).strip():
                continue
            totals[str(group).strip()] += value
            included += 1
        lines = [f"- {_display(group)}: {_format_number(total)}" for group, total in sorted(totals.items())]
        basis = f"{included} valid '{value_column}' values grouped by '{group_column}'"
        return _response(
            scope,
            f"{value_column} by {group_column}:\n" + "\n".join(lines) + f"\nCalculation basis: {basis}.",
            relevant_sheets,
            basis,
        )

    # Lists are deterministic filters over all rows, never a top-K semantic sample.
    status_column, _ = _resolve_column(rows, "status state")
    status_words = {
        word
        for word in ("pending", "open", "closed", "overdue", "active", "inactive", "approved", "rejected")
        if re.search(rf"\b{word}\b", question.casefold())
    }
    selected = rows
    range_bounds = _numeric_range(question)
    if range_bounds is not None:
        range_column, clarification = _resolve_column(
            rows,
            question,
            numeric=True,
        )
        if clarification:
            return _response(
                scope,
                clarification,
                relevant_sheets,
                scope_basis,
            )
        if range_column is None:
            return _response(
                scope,
                "The table does not contain an identifiable numeric column for this range.",
                relevant_sheets,
                scope_basis,
            )
        lower, upper = range_bounds
        selected = [
            row
            for row in rows
            if (
                (value := _cell_number(row.values.get(range_column))) is not None
                and lower <= value <= upper
            )
        ]
    bound = _numeric_bound(question)
    if bound is not None:
        bound_column, clarification = _resolve_column(
            rows,
            question,
            numeric=True,
        )
        if bound_column is None and clarification is None and _has_monetary_limit(
            question
        ):
            bound_column, clarification = _resolve_column(
                rows,
                "price cost amount value",
                numeric=True,
            )
        if clarification:
            return _response(scope, clarification, relevant_sheets, scope_basis)
        if bound_column is None:
            return _response(
                scope,
                "The table does not contain an identifiable numeric column for this limit.",
                relevant_sheets,
                scope_basis,
            )
        operator, threshold = bound
        comparisons = {
            "lt": lambda value: value < threshold,
            "lte": lambda value: value <= threshold,
            "gt": lambda value: value > threshold,
            "gte": lambda value: value >= threshold,
        }
        selected = [
            row
            for row in selected
            if (
                (value := _cell_number(row.values.get(bound_column))) is not None
                and comparisons[operator](value)
            )
        ]
    if status_words:
        if status_column is None:
            return _response(
                scope,
                "The workbook does not contain an identifiable status column for this list.",
                relevant_sheets,
                scope_basis,
            )
        selected = [
            row
            for row in selected
            if str(row.values.get(status_column, "")).strip().casefold() in status_words
        ]
    basis = f"{len(selected)} matching rows from {scope_basis}"
    return _response(
        scope,
        (
            f"Matching records ({len(selected)}):\n\n"
            + _records_table(scope, selected)
            if selected
            else "Matching records (0): No rows satisfied the requested filters."
        ),
        (
            sorted({row.sheet for row in selected})
            if selected
            else relevant_sheets
        ),
        basis,
        selected if selected else rows,
    )
