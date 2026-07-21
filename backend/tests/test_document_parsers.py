"""Parser-registry tests for tabular, presentation, and OCR formats."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.document_loader import (
    DocumentParseError,
    ExcelParser,
    OcrParser,
    PARSER_REGISTRY,
    PowerPointParser,
    SUPPORTED_EXTENSIONS,
    _extract_legacy_ppt_text,
    extract_text,
)


class DocumentParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_contains_every_required_extension(self):
        required = {".txt", ".pdf", ".docx", ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".webp"}
        self.assertEqual(SUPPORTED_EXTENSIONS, required)
        self.assertIsInstance(PARSER_REGISTRY[".xlsx"], ExcelParser)
        self.assertIsInstance(PARSER_REGISTRY[".ppt"], PowerPointParser)
        self.assertIsInstance(PARSER_REGISTRY[".webp"], OcrParser)

    def test_csv_parser_extracts_rows(self):
        path = self.root / "employees.csv"
        path.write_text("Employee ID,Name,Department\n1001,John,HR\n1002,Alice,Finance", encoding="utf-8")
        text = extract_text(path)
        self.assertIn("CSV: employees", text)
        self.assertIn("Employee ID\tName\tDepartment", text)
        self.assertIn("1002\tAlice\tFinance", text)

    def test_xlsx_parser_extracts_workbook_sheets_and_cells(self):
        from openpyxl import Workbook

        path = self.root / "employees.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Employees"
        sheet.append(["Employee ID", "Name", "Department"])
        sheet.append([1001, "John", "HR"])
        workbook.create_sheet("Summary").append(["Total", 1])
        workbook.save(path)
        workbook.close()
        text = extract_text(path)
        self.assertIn("Workbook: employees", text)
        self.assertIn("Sheet: Employees", text)
        self.assertIn("1001\tJohn\tHR", text)
        self.assertIn("Sheet: Summary", text)

    def test_pptx_parser_extracts_slide_text_and_tables(self):
        from pptx import Presentation
        from pptx.util import Inches

        path = self.root / "quarterly.pptx"
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "Quarterly Sales"
        slide.placeholders[1].text = "Revenue increased by 15%"
        table = slide.shapes.add_table(1, 2, Inches(1), Inches(4), Inches(6), Inches(1)).table
        table.cell(0, 0).text = "Region"
        table.cell(0, 1).text = "Revenue"
        presentation.save(path)
        text = extract_text(path)
        self.assertIn("Presentation: quarterly", text)
        self.assertIn("Slide 1", text)
        self.assertIn("Quarterly Sales", text)
        self.assertIn("Revenue increased by 15%", text)
        self.assertIn("Region\tRevenue", text)

    def test_legacy_ppt_text_atom_extraction(self):
        payload = "Legacy slide text".encode("utf-16-le")
        record = struct.pack("<HHI", 0, 4000, len(payload)) + payload
        self.assertEqual(_extract_legacy_ppt_text(record), ["Legacy slide text"])

    def test_ocr_parser_uses_all_image_frames(self):
        from PIL import Image

        path = self.root / "scan.png"
        Image.new("RGB", (30, 20), "white").save(path)
        with patch("pytesseract.image_to_string", return_value="Invoice total 42") as ocr:
            text = extract_text(path)
        self.assertIn("Image: scan", text)
        self.assertIn("Invoice total 42", text)
        ocr.assert_called_once()

    def test_missing_tesseract_returns_clear_error(self):
        import pytesseract
        from PIL import Image

        path = self.root / "scan.jpg"
        Image.new("RGB", (30, 20), "white").save(path)
        with patch("pytesseract.image_to_string", side_effect=pytesseract.TesseractNotFoundError()):
            with self.assertRaisesRegex(DocumentParseError, "Tesseract service is not installed"):
                extract_text(path)

    def test_corrupt_excel_and_powerpoint_return_format_errors(self):
        xlsx = self.root / "broken.xlsx"
        pptx = self.root / "broken.pptx"
        xlsx.write_bytes(b"not a workbook")
        pptx.write_bytes(b"not a presentation")
        with self.assertRaisesRegex(DocumentParseError, "Excel workbook"):
            extract_text(xlsx)
        with self.assertRaisesRegex(DocumentParseError, "PowerPoint presentation"):
            extract_text(pptx)

    def test_unsupported_extension_is_rejected(self):
        path = self.root / "document.exe"
        path.write_bytes(b"unsafe")
        with self.assertRaisesRegex(DocumentParseError, "Unsupported document type"):
            extract_text(path)


if __name__ == "__main__":
    unittest.main()
