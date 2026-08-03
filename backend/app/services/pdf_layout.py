"""Small PDF text-layout helpers shared by extraction and structured parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from app.config import settings
from app.services.document_loader import DocumentParseError


@dataclass(frozen=True)
class PdfPageText:
    """Text recovered from one PDF page with page provenance."""

    page_number: int
    text: str


def _table_text_score(text: str) -> int:
    """Prefer text that preserves visual rows and columns over flattened prose."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    column_lines = sum(
        bool("|" in line or "\t" in line or re.search(r"\S\s{2,}\S", line))
        for line in lines
    )
    return len(lines) + column_lines * 3


def _page_text(page, **kwargs) -> str:
    """Call pypdf safely across tests/fakes and installed version differences."""
    try:
        return page.extract_text(**kwargs) or ""
    except TypeError:
        return page.extract_text() or ""


def _coordinate_text(page) -> str:
    """Fallback for text PDFs where plain extraction flattens positioned cells."""
    fragments: list[tuple[float, float, str]] = []

    def visitor_text(text, _cm, tm, _font_dict, _font_size) -> None:
        value = " ".join(str(text or "").split())
        if not value:
            return
        try:
            x = float(tm[4])
            y = float(tm[5])
        except (TypeError, ValueError, IndexError):
            return
        fragments.append((y, x, value))

    try:
        page.extract_text(visitor_text=visitor_text)
    except TypeError:
        return ""
    if not fragments:
        return ""

    rows: list[list[tuple[float, str]]] = []
    for y, x, value in sorted(fragments, key=lambda item: (-item[0], item[1])):
        if not rows or abs(rows[-1][0][0] - y) > 3:
            rows.append([(y, value)])
        else:
            rows[-1].append((x, value))
    return "\n".join(
        "  ".join(value for _, value in sorted(row, key=lambda item: item[0]))
        for row in rows
    )


def extract_pdf_page_texts(path: Path) -> list[PdfPageText]:
    """Extract text pages, preferring layout-preserving output when available."""
    try:
        from pypdf import PdfReader

        pages = PdfReader(str(path)).pages
        if len(pages) > settings.max_pdf_pages:
            raise DocumentParseError("PDF exceeds the configured page limit.")
        result: list[PdfPageText] = []
        for page_number, page in enumerate(pages, start=1):
            plain = _page_text(page)
            layout = _page_text(page, extraction_mode="layout")
            coordinate = _coordinate_text(page)
            text = max(
                (plain, layout, coordinate),
                key=lambda value: (_table_text_score(value), len(value.strip())),
            )
            if text.strip():
                result.append(PdfPageText(page_number, text))
        return result
    except DocumentParseError:
        raise
    except Exception as error:
        raise DocumentParseError("The PDF file could not be read.") from error
