"""Domain-neutral, multi-sheet workbook ingestion and analysis tests."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from openpyxl import Workbook
from starlette.requests import Request

from app import database
from app.routes import upload
from app.services import vector_search
from app.services.workbook_analysis import analyze_workbook_question


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/documents/upload",
        "headers": [],
        "client": ("test", 1),
    })


def _workbook_bytes(sheets: list[tuple[str, list[list[object]], str]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows, state in sheets:
        sheet = workbook.create_sheet(name)
        sheet.sheet_state = state
        for row in rows:
            sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class WorkbookDomainNeutralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_path = root / "test.db"
        self.upload_path = root / "uploads"
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, "DATABASE_PATH", self.database_path))
        self.stack.enter_context(patch.object(upload, "UPLOAD_DIRECTORY", self.upload_path))
        self.stack.enter_context(patch.object(upload, "enforce_request_limit", lambda *args, **kwargs: None))
        self.stack.enter_context(patch.object(upload, "log_audit_event", lambda **kwargs: None))
        self.stack.enter_context(
            patch.object(
                upload,
                "create_embeddings",
                lambda chunks: [
                    [1.0, 0.0] if "FINAL_ONLY" in chunk else [0.0, 1.0]
                    for chunk in chunks
                ],
            )
        )
        database.initialize_database()
        with database.get_connection() as connection:
            connection.executemany(
                "INSERT INTO users (id, email, password_hash) VALUES (?, ?, 'hash')",
                [(1, "owner@example.com"), (2, "other@example.com")],
            )

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def upload_workbook(
        self,
        sheets: list[tuple[str, list[list[object]], str]],
        filename: str,
        owner_id: int = 1,
    ) -> dict[str, object]:
        file = UploadFile(file=BytesIO(_workbook_bytes(sheets)), filename=filename)
        return asyncio.run(upload._process_document_upload(
            _request(),
            file,
            {"id": owner_id},
        ))

    def test_all_thirteen_employee_tabs_are_processed_and_counted(self):
        sheets = [
            (
                f"Team {index}",
                [["Employee ID", "Name"], [f"{index:02d}01", f"Person {index}"]],
                "hidden" if index == 13 else "visible",
            )
            for index in range(1, 14)
        ]
        result = self.upload_workbook(sheets, "employees.xlsx")
        answer = analyze_workbook_question(
            "How many employees are there?",
            owner_id=1,
            document_id=int(result["document_id"]),
        )
        self.assertEqual(len(result["workbook"]["processed_sheets"]), 13)
        self.assertIn("Count: 13", answer["answer"])
        self.assertEqual(len(answer["sources"]), 13)

    def test_finance_totals_averages_and_single_sheet_scope(self):
        sheets = [
            ("Q1", [["Category", "Actual Cost"], ["Cloud", 100], ["Travel", 200]], "visible"),
            ("Q2", [["Category", "Actual Cost"], ["Cloud", 300], ["Travel", 400]], "visible"),
        ]
        result = self.upload_workbook(sheets, "finance.xlsx")
        document_id = int(result["document_id"])
        total = analyze_workbook_question(
            "What is the total actual cost?",
            1,
            document_id=document_id,
        )
        average = analyze_workbook_question(
            "What is the average actual cost?",
            1,
            document_id=document_id,
        )
        q2 = analyze_workbook_question(
            "What is the total actual cost in tab Q2?",
            1,
            document_id=document_id,
        )
        self.assertIn("1,000", total["answer"])
        self.assertIn("250", average["answer"])
        self.assertIn("700", q2["answer"])
        self.assertEqual([source["sheet_name"] for source in q2["sources"]], ["Q2"])

    def test_inventory_distinct_count_deduplicates_only_when_requested(self):
        sheets = [
            ("North", [["Product ID", "Stock"], ["001", 5], ["002", 7]], "visible"),
            ("South", [["Product ID", "Stock"], ["001", 4], ["003", 9]], "visible"),
        ]
        result = self.upload_workbook(sheets, "inventory.xlsx")
        document_id = int(result["document_id"])
        records = analyze_workbook_question("How many records are there?", 1, document_id=document_id)
        unique = analyze_workbook_question(
            "How many unique products are there?",
            1,
            document_id=document_id,
        )
        self.assertIn("Count: 4", records["answer"])
        self.assertIn("3", unique["answer"])

    def test_empty_tabs_do_not_affect_calculations_and_are_reported(self):
        result = self.upload_workbook(
            [
                ("Data", [["Invoice", "Amount"], ["INV-1", 10]], "visible"),
                ("Blank", [], "visible"),
            ],
            "invoices.xlsx",
        )
        answer = analyze_workbook_question(
            "What is the total amount?",
            1,
            document_id=int(result["document_id"]),
        )
        self.assertEqual(result["workbook"]["empty_sheets"], ["Blank"])
        self.assertIn("10", answer["answer"])
        self.assertEqual([source["sheet_name"] for source in answer["sources"]], ["Data"])

    def test_search_finds_a_row_only_in_the_final_sheet(self):
        result = self.upload_workbook(
            [
                ("First", [["Code", "Description"], ["A-1", "ordinary"]], "visible"),
                ("Final", [["Code", "Description"], ["Z-9", "FINAL_ONLY"]], "visible"),
            ],
            "projects.xlsx",
        )
        with patch.object(vector_search, "create_embeddings", return_value=[[1.0, 0.0]]):
            matches = vector_search.search_chunks(
                "FINAL_ONLY",
                owner_id=1,
                document_id=int(result["document_id"]),
                limit=1,
            )
        self.assertEqual(matches[0]["sheet_name"], "Final")
        self.assertIn("FINAL_ONLY", matches[0]["content"])

    def test_another_owner_cannot_analyze_a_selected_workbook(self):
        result = self.upload_workbook(
            [("Private", [["Invoice", "Amount"], ["SECRET", 500]], "visible")],
            "private.xlsx",
        )
        answer = analyze_workbook_question(
            "What is the total amount?",
            owner_id=2,
            document_id=int(result["document_id"]),
        )
        self.assertEqual(answer["sources"], [])
        self.assertNotIn("500", answer["answer"])
        self.assertIn("No accessible structured workbook", answer["answer"])


if __name__ == "__main__":
    unittest.main()
