"""Safe, domain-neutral spreadsheet extraction and row chunk generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from json import dumps
from pathlib import Path
import math
import re

from app.services.document_loader import DocumentParseError


@dataclass
class WorkbookRow:
    row_number: int
    values: dict[str, object]


@dataclass
class WorkbookSheet:
    name: str
    state: str
    status: str
    header_row: int | None = None
    headers: list[str] = field(default_factory=list)
    rows: list[WorkbookRow] = field(default_factory=list)
    error: str | None = None


@dataclass
class WorkbookData:
    sheets: list[WorkbookSheet]

    @property
    def non_empty_sheets(self) -> list[WorkbookSheet]:
        return [sheet for sheet in self.sheets if sheet.status == "processed"]

    @property
    def skipped_sheets(self) -> list[str]:
        return [sheet.name for sheet in self.sheets if sheet.status == "empty"]

    @property
    def failed_sheets(self) -> list[str]:
        return [sheet.name for sheet in self.sheets if sheet.status == "failed"]


def _normalized_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() else value
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _xlsx_value(cell) -> object:
    value = _normalized_value(cell.value)
    if isinstance(value, int) and value >= 0:
        number_format = str(getattr(cell, "number_format", "") or "")
        if re.fullmatch(r"0+", number_format) and len(number_format) > len(str(value)):
            return str(value).zfill(len(number_format))
    return value


def _is_nonempty(value: object) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _detect_header(rows: list[tuple[int, list[object]]]) -> int:
    """Choose a probable header from the leading rows without domain assumptions."""
    candidates = rows[:20]
    best_index = 0
    best_score = float("-inf")
    for index, (_, values) in enumerate(candidates):
        present = [value for value in values if _is_nonempty(value)]
        if not present:
            continue
        text_count = sum(isinstance(value, str) for value in present)
        distinct = len({str(value).strip().casefold() for value in present})
        following_width = 0
        if index + 1 < len(rows):
            following_width = sum(_is_nonempty(value) for value in rows[index + 1][1])
        score = len(present) * 3 + text_count * 2 + distinct + min(following_width, len(present))
        if len(present) == 1 and len(rows) > index + 1:
            score -= 4
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _headers(values: list[object], width: int) -> list[str]:
    output: list[str] = []
    used: dict[str, int] = {}
    for index in range(width):
        raw = values[index] if index < len(values) else None
        base = str(raw).strip() if _is_nonempty(raw) else f"Column {index + 1}"
        count = used.get(base.casefold(), 0) + 1
        used[base.casefold()] = count
        output.append(base if count == 1 else f"{base} ({count})")
    return output


def _make_sheet(name: str, state: str, rows: list[tuple[int, list[object]]]) -> WorkbookSheet:
    nonempty = [
        (number, values)
        for number, values in rows
        if any(_is_nonempty(value) for value in values)
    ]
    if not nonempty:
        return WorkbookSheet(name=name, state=state, status="empty")

    header_index = _detect_header(nonempty)
    header_number, header_values = nonempty[header_index]
    data_rows = nonempty[header_index + 1 :]
    width = max([len(header_values), *(len(values) for _, values in data_rows)])
    headers = _headers(header_values, width)
    structured = []
    for row_number, values in data_rows:
        row = {
            header: values[index] if index < len(values) else None
            for index, header in enumerate(headers)
        }
        if any(_is_nonempty(value) for value in row.values()):
            structured.append(WorkbookRow(row_number=row_number, values=row))
    return WorkbookSheet(
        name=name,
        state=state,
        status="processed",
        header_row=header_number,
        headers=headers,
        rows=structured,
    )


def _extract_xlsx(path: Path, include_hidden: bool) -> WorkbookData:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    sheets: list[WorkbookSheet] = []
    try:
        for worksheet in workbook.worksheets:
            state = str(worksheet.sheet_state)
            if not include_hidden and state != "visible":
                sheets.append(WorkbookSheet(worksheet.title, state, "disabled"))
                continue
            try:
                rows = [
                    (index, [_xlsx_value(cell) for cell in row])
                    for index, row in enumerate(worksheet.iter_rows(), start=1)
                ]
                sheets.append(_make_sheet(worksheet.title, state, rows))
            except Exception:
                sheets.append(
                    WorkbookSheet(
                        worksheet.title,
                        state,
                        "failed",
                        error="This worksheet could not be read.",
                    )
                )
    finally:
        workbook.close()
    return WorkbookData(sheets)


def _extract_xls(path: Path, include_hidden: bool) -> WorkbookData:
    import xlrd

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    sheets: list[WorkbookSheet] = []
    try:
        for index in range(workbook.nsheets):
            worksheet = workbook.sheet_by_index(index)
            visibility = int(workbook.sheet_visibility[index])
            state = "visible" if visibility == 0 else "hidden"
            if not include_hidden and visibility != 0:
                sheets.append(WorkbookSheet(worksheet.name, state, "disabled"))
                continue
            try:
                rows: list[tuple[int, list[object]]] = []
                for row_index in range(worksheet.nrows):
                    values: list[object] = []
                    for column_index in range(worksheet.ncols):
                        cell = worksheet.cell(row_index, column_index)
                        value: object = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, workbook.datemode)
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            value = bool(value)
                        elif cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                            value = None
                        values.append(_normalized_value(value))
                    rows.append((row_index + 1, values))
                sheets.append(_make_sheet(worksheet.name, state, rows))
            except Exception:
                sheets.append(
                    WorkbookSheet(
                        worksheet.name,
                        state,
                        "failed",
                        error="This worksheet could not be read.",
                    )
                )
    finally:
        workbook.release_resources()
    return WorkbookData(sheets)


def extract_workbook(path: Path, include_hidden: bool = True) -> WorkbookData:
    """Read all configured sheets without evaluating formulas, links, or macros."""
    try:
        if path.suffix.lower() == ".xlsx":
            workbook = _extract_xlsx(path, include_hidden)
        elif path.suffix.lower() == ".xls":
            workbook = _extract_xls(path, include_hidden)
        else:
            raise DocumentParseError("Unsupported spreadsheet type.")
    except DocumentParseError:
        raise
    except Exception as error:
        label = "legacy Excel workbook" if path.suffix.lower() == ".xls" else "Excel workbook"
        raise DocumentParseError(f"The {label} could not be read.") from error
    if not workbook.non_empty_sheets:
        failed = ", ".join(workbook.failed_sheets)
        detail = f" Affected sheets: {failed}." if failed else ""
        raise DocumentParseError(f"The workbook contains no readable non-empty worksheets.{detail}")
    return workbook


def _plain_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\x00", "").split())
    if text.startswith(("=", "+", "-", "@")):
        text = f"'{text}"
    return text.replace("|", r"\|")


def workbook_chunks(workbook: WorkbookData, filename: str) -> list[tuple[str, str, int | None]]:
    """Create row-oriented chunks, each carrying workbook and sheet provenance."""
    chunks: list[tuple[str, str, int | None]] = []
    for sheet in workbook.non_empty_sheets:
        if not sheet.rows:
            header_text = " | ".join(_plain_text(header) for header in sheet.headers)
            chunks.append((
                f"Workbook: {_plain_text(filename)} | Sheet: {_plain_text(sheet.name)} | "
                f"Headers: {header_text}",
                sheet.name,
                sheet.header_row,
            ))
            continue
        for row in sheet.rows:
            fields = " | ".join(
                f"{_plain_text(header)}: {_plain_text(value)}"
                for header, value in row.values.items()
                if _is_nonempty(value)
            )
            chunks.append((
                f"Workbook: {_plain_text(filename)} | Sheet: {_plain_text(sheet.name)} | "
                f"Row: {row.row_number} | {fields}",
                sheet.name,
                row.row_number,
            ))
    return chunks


def workbook_text(workbook: WorkbookData, filename: str) -> str:
    metadata = {
        "empty_sheets": workbook.skipped_sheets,
        "failed_sheets": workbook.failed_sheets,
    }
    chunks = [text for text, _, _ in workbook_chunks(workbook, filename)]
    return "\n".join([f"Workbook processing metadata: {dumps(metadata)}", *chunks])
