"""Deterministic, owner-scoped analysis over structured workbook rows."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from json import loads
from html import escape
import re

from app.database import get_connection


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
    r"\blist\b.*\b(pending|open|closed|overdue|active|inactive|approved|rejected)\b",
)

ALIASES = (
    {"cost", "costs", "expense", "expenses", "amount", "spend", "spending", "price", "value"},
    {"employee", "employees", "staff", "person", "people", "resource", "resources", "name"},
    {"vendor", "vendors", "supplier", "suppliers"},
    {"product", "products", "item", "items", "sku", "skus"},
    {"department", "departments", "division", "divisions", "team", "teams"},
    {"invoice", "invoices", "bill", "bills"},
    {"project", "projects", "initiative", "initiatives"},
    {"status", "state"},
    {"customer", "customers", "client", "clients"},
)


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


def _load_scopes(
    owner_id: int,
    collection_id: int | None,
    document_id: int | None,
) -> list[WorkbookScope]:
    with get_connection() as connection:
        documents = connection.execute(
            """
            SELECT DISTINCT d.id, d.display_filename, d.content_id
            FROM documents d
            JOIN workbook_sheets ws ON ws.content_id = d.content_id
            WHERE d.owner_id = ? AND ws.owner_id = ?
              AND (? IS NULL OR d.collection_id = ?)
              AND (? IS NULL OR d.id = ?)
            ORDER BY d.id
            """,
            (owner_id, owner_id, collection_id, collection_id, document_id, document_id),
        ).fetchall()
        scopes: list[WorkbookScope] = []
        for document in documents:
            sheets = connection.execute(
                """
                SELECT id, name
                FROM workbook_sheets
                WHERE content_id = ? AND owner_id = ? AND status = 'processed'
                ORDER BY sheet_index
                """,
                (document["content_id"], owner_id),
            ).fetchall()
            rows: list[RowRecord] = []
            for sheet in sheets:
                stored_rows = connection.execute(
                    """
                    SELECT row_number, values_json
                    FROM workbook_rows
                    WHERE sheet_id = ? AND content_id = ? AND owner_id = ?
                    ORDER BY row_number
                    """,
                    (sheet["id"], document["content_id"], owner_id),
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
        if numeric and not any(_number(value) is not None for value in values):
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


def _sources(scope: WorkbookScope, sheets: list[str], rows: list[RowRecord] | None = None):
    row_lookup: dict[str, int | None] = {sheet: None for sheet in sheets}
    for row in rows or []:
        row_lookup.setdefault(row.sheet, row.row_number)
        if row_lookup[row.sheet] is None:
            row_lookup[row.sheet] = row.row_number
    return [
        {
            "document_id": scope.document_id,
            "filename": scope.filename,
            "sheet_name": sheet,
            "row_number": row_lookup.get(sheet),
            "score": 1.0,
        }
        for sheet in sheets
    ]


def _response(
    scope: WorkbookScope,
    answer: str,
    sheets: list[str],
    basis: str,
    rows: list[RowRecord] | None = None,
) -> dict[str, object]:
    return {
        "answer": answer,
        "question_type": "analytical",
        "calculation_basis": basis,
        "sources": _sources(scope, sheets, rows),
    }


def analyze_workbook_question(
    question: str,
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
) -> dict[str, object]:
    """Answer an analytical question without sampling vector-search results."""
    chosen = _choose_workbook(
        _load_scopes(owner_id, collection_id, document_id),
        question,
    )
    if chosen is None:
        return {
            "answer": "No accessible structured workbook was found in the selected scope.",
            "question_type": "analytical",
            "calculation_basis": "No owned workbook rows were available.",
            "sources": [],
        }
    if isinstance(chosen, str):
        return {
            "answer": chosen,
            "question_type": "clarification",
            "calculation_basis": "Multiple workbooks are in scope.",
            "sources": [],
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
            (row, _number(row.values.get(value_column)))
            for row in rows
            if _number(row.values.get(value_column)) is not None
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
                if header != value_column and value is not None and _number(value) is None
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
            for row in rows
            if str(row.values.get(status_column, "")).strip().casefold() in status_words
        ]
    lines = []
    for row in selected[:100]:
        fields = " | ".join(
            f"{_display(header)}: {_display(value)}"
            for header, value in row.values.items()
            if value is not None and str(value).strip()
        )
        lines.append(f"- {row.sheet}, row {row.row_number}: {fields}")
    suffix = "\n- …additional rows omitted" if len(selected) > 100 else ""
    basis = f"{len(selected)} matching rows from {scope_basis}"
    return _response(
        scope,
        f"Matching records ({len(selected)}):\n" + "\n".join(lines) + suffix,
        sorted({row.sheet for row in selected}),
        basis,
        selected,
    )
