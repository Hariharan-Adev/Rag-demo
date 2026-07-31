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
from app.services import vector_store
from app.services import structured_ingestion
from app.services.vector_store import reset_vector_store_for_tests
from app.services.workbook_analysis import (
    analyze_workbook_question,
    is_structured_lookup_question,
)


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
        self.stack.enter_context(patch.object(database, "UPLOAD_DIRECTORY", self.upload_path))
        self.stack.enter_context(patch.object(vector_store.settings, "qdrant_local_path", ""))
        self.stack.enter_context(patch.object(upload, "UPLOAD_DIRECTORY", self.upload_path))
        self.stack.enter_context(patch.object(upload, "enforce_request_limit", lambda *args, **kwargs: None))
        self.stack.enter_context(patch.object(upload, "log_audit_event", lambda **kwargs: None))
        self.stack.enter_context(
            patch.object(
                upload,
                "create_embeddings",
                lambda chunks: [
                    ([1.0, 0.0] if "FINAL_ONLY" in chunk else [0.0, 1.0])
                    + [0.0] * 382
                    for chunk in chunks
                ],
            )
        )
        database.initialize_database()
        reset_vector_store_for_tests()
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

    def test_month_value_lookup_projects_requested_column_without_llm(self):
        result = self.upload_workbook(
            [
                (
                    "Financial Metrics",
                    [
                        ["Month", "Revenue", "Net Income"],
                        ["Jan", 100000, 12000],
                        ["Feb", 108000, 15335],
                    ],
                    "visible",
                ),
            ],
            "monthly-finance.xlsx",
        )
        document_id = int(result["document_id"])

        self.assertTrue(is_structured_lookup_question(
            "give me the feb month revenue?",
            1,
            document_id=document_id,
        ))
        answer = analyze_workbook_question(
            "give me the feb month revenue?",
            1,
            document_id=document_id,
        )
        expanded = analyze_workbook_question(
            "February revenue",
            1,
            document_id=document_id,
        )

        self.assertEqual(answer["question_type"], "structured_lookup")
        self.assertIn("108,000", answer["answer"])
        self.assertIn("B3", answer["answer"])
        self.assertEqual(
            answer["sources"][0]["source_location"]["cell_range"],
            "B3:B3",
        )
        self.assertIn("108,000", expanded["answer"])

    def test_filtered_total_applies_month_before_aggregation(self):
        result = self.upload_workbook(
            [
                (
                    "Financial Metrics",
                    [
                        ["Month", "Revenue"],
                        ["Jan", 100000],
                        ["Feb", 108000],
                    ],
                    "visible",
                ),
            ],
            "monthly-total.xlsx",
        )
        answer = analyze_workbook_question(
            "What is the total revenue for February?",
            1,
            document_id=int(result["document_id"]),
        )

        self.assertEqual(answer["question_type"], "structured_analysis")
        self.assertIn("108,000", answer["answer"])
        self.assertNotIn("208,000", answer["answer"])

    def test_lookup_searches_every_matching_workbook(self):
        first = self.upload_workbook(
            [("Metrics", [["Month", "Revenue"], ["Feb", 10]], "visible")],
            "north.xlsx",
        )
        second = self.upload_workbook(
            [("Metrics", [["Month", "Revenue"], ["Feb", 20]], "visible")],
            "south.xlsx",
        )

        answer = analyze_workbook_question("February revenue", 1)

        self.assertIn("north.xlsx", answer["answer"])
        self.assertIn("south.xlsx", answer["answer"])
        self.assertIn("10", answer["answer"])
        self.assertIn("20", answer["answer"])
        self.assertEqual(answer["matched_document_count"], 2)
        self.assertEqual(
            {source["document_id"] for source in answer["sources"]},
            {int(first["document_id"]), int(second["document_id"])},
        )

    def test_missing_month_is_an_exhaustive_structured_no_match(self):
        result = self.upload_workbook(
            [("Metrics", [["Month", "Revenue"], ["Jan", 10]], "visible")],
            "missing-month.xlsx",
        )
        answer = analyze_workbook_question(
            "February revenue",
            1,
            document_id=int(result["document_id"]),
        )

        self.assertEqual(answer["question_type"], "structured_lookup")
        self.assertEqual(answer["matched_row_count"], 0)
        self.assertIn("No matching structured rows", answer["answer"])

    def test_spreadsheet_reindex_preserves_point_ids_and_labels_rows(self):
        result = self.upload_workbook(
            [("Metrics", [["Month", "Revenue"], ["Feb", 108000]], "visible")],
            "reindex.xlsx",
        )
        document_id = int(result["document_id"])
        with database.get_connection() as connection:
            before = connection.execute(
                """SELECT vector_point_id FROM chunks
                   WHERE document_id = ? ORDER BY chunk_index""",
                (document_id,),
            ).fetchall()
        vectors = {
            str(row["vector_point_id"]): [0.0, 1.0] + [0.0] * 382
            for row in before
        }

        class RecordingStore:
            def __init__(self):
                self.batches = []

            def get_vectors(self, point_ids):
                return {
                    point_id: vectors[point_id]
                    for point_id in point_ids
                    if point_id in vectors
                }

            def upsert_chunks(self, points):
                self.batches.append(points)

        store = RecordingStore()
        with patch.object(
            structured_ingestion,
            "get_vector_store",
            return_value=store,
        ), patch.object(
            structured_ingestion,
            "create_embeddings",
            return_value=[
                [1.0, 0.0] + [0.0] * 382
                for _ in range(len(before))
            ],
        ):
            first = structured_ingestion.reindex_existing_spreadsheet_document(
                document_id=document_id,
                owner_id=1,
                organization_id="00000000-0000-4000-8000-000000000001",
            )
            second = structured_ingestion.reindex_existing_spreadsheet_document(
                document_id=document_id,
                owner_id=1,
                organization_id="00000000-0000-4000-8000-000000000001",
            )

        with database.get_connection() as connection:
            after = connection.execute(
                """SELECT vector_point_id, text FROM chunks
                   WHERE document_id = ? ORDER BY chunk_index""",
                (document_id,),
            ).fetchall()
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual(
            [row["vector_point_id"] for row in after],
            [row["vector_point_id"] for row in before],
        )
        indexed_text = "\n".join(str(row["text"]) for row in after)
        self.assertIn("Month: Feb", indexed_text)
        self.assertIn("Revenue: 108000", indexed_text)
        self.assertEqual(len(store.batches), 2)

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

    def test_price_range_is_inclusive_and_cites_selected_rows(self):
        result = self.upload_workbook(
            [
                (
                    "Equipment",
                    [
                        ["Equipment", "Price"],
                        ["Seed Drill", 45000],
                        ["Tractor", 550000],
                        ["Power Tiller", 125000],
                        ["Irrigation Pump", 75000],
                        ["Harvester", 200000],
                        ["Agricultural Drone", 210000],
                    ],
                    "visible",
                ),
            ],
            "agriculture.xlsx",
        )
        answer = analyze_workbook_question(
            "Show all equipment priced between ₹50,000 and ₹200,000.",
            1,
            document_id=int(result["document_id"]),
        )

        text = str(answer["answer"])
        self.assertIn("Power Tiller", text)
        self.assertIn("Irrigation Pump", text)
        self.assertIn("Harvester", text)
        self.assertNotIn("Seed Drill", text)
        self.assertNotIn("Tractor", text)
        self.assertNotIn("Agricultural Drone", text)
        self.assertEqual(answer["sources"][0]["source_location"]["row_start"], 4)
        self.assertEqual(answer["sources"][0]["source_location"]["row_end"], 6)

    def test_below_price_filter_returns_markdown_table_only_with_valid_rows(self):
        result = self.upload_workbook(
            [
                (
                    "Tools",
                    [
                        ["Name", "Model", "HP Type", "Price"],
                        ["Rotavator", "RT-200", 45, 95000],
                        ["Cultivator", "CV-150", 40, 45000],
                        ["Disc Harrow", "DH-300", 55, 120000],
                        ["MB Plough", "MBP-250", 50, 85000],
                        ["Thresher", "TH-600", 60, 180000],
                    ],
                    "visible",
                ),
            ],
            "farming-tools.xlsx",
        )
        answer = analyze_workbook_question(
            "List all farming tools priced below ₹100,000.",
            1,
            document_id=int(result["document_id"]),
        )
        lakh_answer = analyze_workbook_question(
            "tell me the model which is under 1 lak",
            1,
            document_id=int(result["document_id"]),
        )
        plural_lakh_answer = analyze_workbook_question(
            "tell me the model which is under 1 laks",
            1,
            document_id=int(result["document_id"]),
        )

        text = str(answer["answer"])
        self.assertIn("Matching records (3):", text)
        self.assertIn("| Name | Model | HP Type | Price | Source |", text)
        self.assertIn("|---|---|---|---|---|", text)
        self.assertIn("Rotavator", text)
        self.assertIn("Cultivator", text)
        self.assertIn("MB Plough", text)
        self.assertNotIn("Disc Harrow", text)
        self.assertNotIn("Thresher", text)
        self.assertNotIn("- Tools, row", text)
        lakh_text = str(lakh_answer["answer"])
        self.assertIn("Matching records (3):", lakh_text)
        self.assertIn("RT-200", lakh_text)
        self.assertIn("CV-150", lakh_text)
        self.assertIn("MBP-250", lakh_text)
        self.assertNotIn("DH-300", lakh_text)
        self.assertNotIn("TH-600", lakh_text)
        plural_lakh_text = str(plural_lakh_answer["answer"])
        self.assertIn("Matching records (3):", plural_lakh_text)
        self.assertIn("RT-200", plural_lakh_text)
        self.assertIn("CV-150", plural_lakh_text)
        self.assertIn("MBP-250", plural_lakh_text)
        self.assertNotIn("DH-300", plural_lakh_text)
        self.assertNotIn("TH-600", plural_lakh_text)

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
        with patch.object(vector_search, "create_embeddings", return_value=[[1.0, 0.0] + [0.0] * 382]):
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
