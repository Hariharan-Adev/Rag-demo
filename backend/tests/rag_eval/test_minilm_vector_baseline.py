"""Local all-MiniLM-L6-v2 retrieval baseline for identifier-heavy queries."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from statistics import median
import tempfile
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from starlette.requests import Request

from app import database
from app.config import settings
from app.routes import upload
from app.services import vector_search, vector_store
from app.services.vector_search import search_chunks
from app.services.vector_store import reset_vector_store_for_tests
from tests.rag_eval.metrics import expected_chunk_rank, mean_reciprocal_rank, precision_at_k, recall_at_k


@dataclass(frozen=True)
class VectorProbe:
    """One synthetic exact-reference query and its intended chunk text."""

    query_class: str
    query: str
    filename: str
    text: str


PROBES = (
    VectorProbe("employee_name", "Maren Voss", "employee-record.txt", "Employee record: Maren Voss supports reliability operations."),
    VectorProbe("employee_id", "EMP-7429", "employee-id.txt", "Employee ID EMP-7429 has approved field access."),
    VectorProbe("code", "ZXQ-19", "asset-code.txt", "Asset code ZXQ-19 is assigned to the calibration station."),
    VectorProbe("filename", "nimbus-ledger-2026 file", "nimbus-ledger-2026.txt", "The Nimbus ledger records monthly operating balances."),
    VectorProbe("exact_label", "NEEDS_CALIBRATION", "status-label.txt", "Status label NEEDS_CALIBRATION requires review before dispatch."),
    VectorProbe("number", "847291", "invoice-number.txt", "Invoice number 847291 belongs to the service renewal."),
    VectorProbe("column_heading", "Escalation Owner", "escalation-register.txt", "Escalation Owner\nAsha Patel\nEscalation Level\nCritical"),
    VectorProbe("unusual_abbreviation", "RZT", "telemetry.txt", "The telemetry abbreviation RZT means remote zero touch provisioning."),
)


CATEGORY_PROBES = (
    VectorProbe("semantic_paraphrase", "Which policy lets staff work remotely twice weekly?", "flex-work.txt", "The flexible-work policy permits employees to work from home two days each week."),
    VectorProbe("semantic_paraphrase", "How should a worker request time away?", "absence-process.txt", "Submit leave requests through the absence portal before the planned absence."),
    VectorProbe("names", "Maren Voss", "maren-voss.txt", "Maren Voss leads the reliability operations team."),
    VectorProbe("names", "Daria Cole", "daria-cole.txt", "Daria Cole owns the supplier quality review."),
    VectorProbe("numbers", "invoice 847291", "invoice-847291.txt", "Invoice 847291 is the annual service renewal."),
    VectorProbe("numbers", "batch 90317", "batch-90317.txt", "Batch 90317 completed the environmental qualification run."),
    VectorProbe("workbook_labels", "Where is the Net Revenue field?", "forecast-workbook-labels.txt", "Workbook Forecast sheet: Net Revenue column contains the monthly value."),
    VectorProbe("workbook_labels", "Which column records attendance status?", "attendance-workbook-labels.txt", "Workbook Attendance sheet: Attendance Status column records each shift."),
    VectorProbe("document_headings", "How are incidents escalated?", "incident-procedure.txt", "Incident Escalation Procedure\nNotify the on-call coordinator and classify the severity."),
    VectorProbe("document_headings", "What is the equipment inspection process?", "inspection-procedure.txt", "Equipment Inspection Checklist\nInspect guards, controls, and calibration labels before use."),
    VectorProbe("abbreviations", "What does RZT mean?", "rzt-telemetry.txt", "RZT means remote zero touch provisioning for telemetry devices."),
    VectorProbe("abbreviations", "Explain the LQL acronym.", "lql-quality.txt", "LQL means lot quality limit in the incoming inspection procedure."),
    VectorProbe("cross_domain_terminology", "Which instrument measures blood oxygen?", "clinical-oxygen.txt", "A pulse oximeter records oxygen saturation during patient monitoring."),
    VectorProbe("cross_domain_terminology", "What device tracks warehouse humidity?", "warehouse-humidity.txt", "A hygrometer measures humidity in the warehouse storage area."),
)
DECOY_VALUES = {
    "employee_name": ("Juno Hale", "Ravi Shah", "Lena Ortiz", "Daria Cole", "Noah Reed", "Mika Stone", "Asha Jain", "Theo Park"),
    "employee_id": ("EMP-1101", "EMP-1102", "EMP-1103", "EMP-1104", "EMP-1105", "EMP-1106", "EMP-1107", "EMP-1108"),
    "code": ("ZXQ-11", "ZXQ-12", "ZXQ-13", "ZXQ-14", "ZXQ-15", "ZXQ-16", "ZXQ-17", "ZXQ-18"),
    "exact_label": ("PENDING_REVIEW", "ON_HOLD", "READY_FOR_TEST", "NEEDS_APPROVAL", "AWAITING_PARTS", "OUT_OF_SCOPE", "PENDING_SIGNOFF", "REQUIRES_ESCALATION"),
    "number": ("847201", "847202", "847203", "847204", "847205", "847206", "847207", "847208"),
    "column_heading": ("Service Owner", "Incident Owner", "Review Owner", "Asset Owner", "Change Owner", "Risk Owner", "Policy Owner", "Project Owner"),
    "unusual_abbreviation": ("RZP", "RZQ", "RZR", "RZS", "RZU", "RZV", "RZW", "RZX"),
}
DECOYS_PER_CLASS = 8


def _request() -> Request:
    """Build the minimal upload request used by the existing ingestion helper."""
    return Request({"type": "http", "method": "POST", "path": "/documents/upload", "headers": [], "client": ("rag-eval", 1)})


class MiniLMVectorRetrievalBaselineTests(unittest.TestCase):
    """Measure dense retrieval only; these probes intentionally set no quality threshold."""

    def setUp(self) -> None:
        """Create isolated local storage while retaining the configured local embedding model."""
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(database, "DATABASE_PATH", root / "vector-baseline.db"))
        self.stack.enter_context(patch.object(database, "UPLOAD_DIRECTORY", root / "uploads"))
        self.stack.enter_context(patch.object(vector_store.settings, "vector_store", "sqlite"))
        self.stack.enter_context(patch.object(vector_store.settings, "vector_store_provider", "sqlite"))
        self.stack.enter_context(patch.object(vector_store.settings, "qdrant_local_path", ""))
        self.stack.enter_context(patch.object(upload, "UPLOAD_DIRECTORY", root / "uploads"))
        self.stack.enter_context(patch.object(upload, "enforce_request_limit", lambda *args, **kwargs: None))
        self.stack.enter_context(patch.object(upload, "log_audit_event", lambda **kwargs: None))
        database.initialize_database()
        reset_vector_store_for_tests()
        with database.get_connection() as connection:
            connection.execute("INSERT INTO users (id, email, password_hash) VALUES (1, 'vector-eval@example.com', 'hash')")

    def tearDown(self) -> None:
        """Close database patches before removing the temporary corpus."""
        self.stack.close()
        self.temporary.cleanup()

    def _upload_text(self, filename: str, text: str) -> tuple[int, int]:
        """Ingest one short synthetic document and return its stable first chunk ID."""
        uploaded = UploadFile(file=BytesIO(text.encode("utf-8")), filename=filename)
        result = asyncio.run(upload._process_document_upload(_request(), uploaded, {"id": 1}))
        document_id = int(result["document_id"])
        with database.get_connection() as connection:
            chunk = connection.execute(
                "SELECT id FROM chunks WHERE document_id = ? AND deleted_at IS NULL ORDER BY chunk_index LIMIT 1",
                (document_id,),
            ).fetchone()
        self.assertIsNotNone(chunk)
        return document_id, int(chunk["id"])

    def _seed_near_duplicate_corpus(self) -> dict[str, int]:
        """Create the stable 72-chunk corpus shared by rank and score probes."""
        expected_chunks = {}
        for probe in PROBES:
            if probe.query_class == "filename":
                for year in range(2017, 2025):
                    self._upload_text(
                        f"nimbus-ledger-{year}.txt",
                        "The Nimbus ledger records monthly operating balances.",
                    )
            else:
                marker = next(value for value in (probe.query, "Escalation Owner") if value in probe.text)
                for index, replacement in enumerate(DECOY_VALUES[probe.query_class], start=1):
                    self._upload_text(
                        f"{probe.query_class}-decoy-{index}.txt",
                        probe.text.replace(marker, replacement),
                    )
            _, chunk_id = self._upload_text(probe.filename, probe.text)
            expected_chunks[probe.query_class] = chunk_id
        return expected_chunks

    def test_all_minilm_category_benchmark(self) -> None:
        """Benchmark vector-only retrieval by query form using the configured local model."""
        self.assertEqual(
            settings.local_embedding_model,
            "sentence-transformers/all-MiniLM-L6-v2",
        )
        expected_chunks = {}
        for index, probe in enumerate(CATEGORY_PROBES):
            _, chunk_id = self._upload_text(probe.filename, probe.text)
            expected_chunks[index] = chunk_id

        category_rankings: dict[str, list[list[int]]] = {}
        category_relevant: dict[str, list[set[int]]] = {}
        with (
            patch.object(vector_search.settings, "rag_retrieval_mode", "vector"),
            patch.object(
                vector_search.settings,
                "rag_vector_candidate_limit",
                len(CATEGORY_PROBES),
            ),
        ):
            for index, probe in enumerate(CATEGORY_PROBES):
                results = search_chunks(
                    probe.query,
                    owner_id=1,
                    limit=len(CATEGORY_PROBES),
                    min_score=None,
                )
                category_rankings.setdefault(probe.query_class, []).append(
                    [int(result["chunk_id"]) for result in results]
                )
                category_relevant.setdefault(probe.query_class, []).append(
                    {expected_chunks[index]}
                )

        print("\nall-MiniLM-L6-v2 vector-only category benchmark:")
        for category in sorted(category_rankings):
            rankings = category_rankings[category]
            relevant = category_relevant[category]
            recall_5 = sum(
                recall_at_k(ranking, expected, 5)
                for ranking, expected in zip(rankings, relevant)
            ) / len(rankings)
            recall_10 = sum(
                recall_at_k(ranking, expected, 10)
                for ranking, expected in zip(rankings, relevant)
            ) / len(rankings)
            mrr = mean_reciprocal_rank(rankings, relevant)
            ranks = [expected_chunk_rank(ranking, expected) for ranking, expected in zip(rankings, relevant)]
            print(
                f"  {category}: Recall@5={recall_5:.2f}, "
                f"Recall@10={recall_10:.2f}, MRR={mrr:.4f}, ranks={ranks}"
            )

        self.assertEqual(set(category_rankings), {
            "semantic_paraphrase", "names", "numbers", "workbook_labels",
            "document_headings", "abbreviations", "cross_domain_terminology",
        })
        self.assertTrue(all(len(rankings) == 2 for rankings in category_rankings.values()))

    def test_all_minilm_identifier_retrieval_baseline(self) -> None:
        """Report dense-search metrics against near-duplicate identifier records."""
        expected_chunks = self._seed_near_duplicate_corpus()

        before = []
        after = []
        for probe in PROBES:
            with patch.object(vector_search, "search_keyword_chunks", return_value=[]):
                vector_only = search_chunks(probe.query, owner_id=1, limit=len(PROBES) * (DECOYS_PER_CLASS + 1), min_score=None)
            hybrid = search_chunks(probe.query, owner_id=1, limit=len(PROBES) * (DECOYS_PER_CLASS + 1), min_score=None)
            expected = {expected_chunks[probe.query_class]}
            for output, results in ((before, vector_only), (after, hybrid)):
                ranked = [int(result["chunk_id"]) for result in results]
                output.append((probe.query_class, recall_at_k(ranked, expected, 5), recall_at_k(ranked, expected, 10), expected_chunk_rank(ranked, expected)))

        print("\nall-MiniLM-L6-v2 vector versus hybrid baseline (synthetic local corpus):")
        for vector_metrics, hybrid_metrics in zip(before, after):
            query_class, recall_5, recall_10, rank = vector_metrics
            _, hybrid_recall_5, hybrid_recall_10, hybrid_rank = hybrid_metrics
            print(f"  {query_class}: vector=({recall_5:.0f}/{recall_10:.0f}/rank {rank or 'not retrieved'}), hybrid=({hybrid_recall_5:.0f}/{hybrid_recall_10:.0f}/rank {hybrid_rank or 'not retrieved'})")
        print(f"  model={settings.local_embedding_model}")

        self.assertEqual({item[0] for item in after}, {probe.query_class for probe in PROBES})
        for vector_metrics, hybrid_metrics in zip(before, after):
            with self.subTest(query_class=vector_metrics[0]):
                self.assertGreaterEqual(hybrid_metrics[1], vector_metrics[1])
                self.assertGreaterEqual(hybrid_metrics[2], vector_metrics[2])
        filename_before = next(item for item in before if item[0] == "filename")
        filename_after = next(item for item in after if item[0] == "filename")
        self.assertEqual(filename_before[1:], (0.0, 1.0, 9))
        self.assertEqual(filename_after[1:3], (1.0, 1.0))
        self.assertLessEqual(int(filename_after[3] or 99), 5)

    def test_vector_score_distribution_by_query_type(self) -> None:
        """Report raw vector-score overlap before the configurable threshold is applied."""
        expected_chunks = self._seed_near_duplicate_corpus()
        cases = (
            ("relevant", "Maren Voss", expected_chunks["employee_name"]),
            ("exact_lookup", "ZXQ-19", expected_chunks["code"]),
            ("broad", "Which record covers the calibration station?", expected_chunks["code"]),
            ("irrelevant", "How does lunar orbit work?", None),
            ("unanswerable", "What is the Phoenix warranty duration?", None),
        )
        distributions = {}
        with (
            patch.object(vector_search.settings, "rag_retrieval_mode", "vector"),
            patch.object(vector_search.settings, "rag_vector_candidate_limit", len(PROBES) * (DECOYS_PER_CLASS + 1)),
        ):
            for category, query, expected_chunk_id in cases:
                results = search_chunks(query, owner_id=1, limit=len(PROBES) * (DECOYS_PER_CLASS + 1), min_score=None)
                scores = [float(result["vector_score"]) for result in results if result.get("vector_score") is not None]
                relevant_score = next(
                    (float(result["vector_score"]) for result in results if result["chunk_id"] == expected_chunk_id),
                    None,
                )
                distributions[category] = {
                    "relevant_score": relevant_score,
                    "top_score": max(scores) if scores else None,
                    "median_score": median(scores) if scores else None,
                    "above_threshold": sum(score >= settings.rag_min_score for score in scores),
                }

        print(f"\nraw vector score distribution at RAG_MIN_SCORE={settings.rag_min_score}:")
        for category, values in distributions.items():
            print(f"  {category}: relevant={values['relevant_score']}, top={values['top_score']}, median={values['median_score']}, above_threshold={values['above_threshold']}")
        self.assertIsNotNone(distributions["relevant"]["relevant_score"])
        self.assertIsNotNone(distributions["exact_lookup"]["relevant_score"])
        self.assertIsNotNone(distributions["broad"]["relevant_score"])
        self.assertGreaterEqual(float(distributions["relevant"]["relevant_score"] or 0), settings.rag_min_score)
        self.assertGreaterEqual(float(distributions["exact_lookup"]["relevant_score"] or 0), settings.rag_min_score)
        self.assertGreaterEqual(float(distributions["broad"]["relevant_score"] or 0), settings.rag_min_score)
        self.assertLess(float(distributions["irrelevant"]["top_score"] or 0), settings.rag_min_score)
        self.assertLess(float(distributions["unanswerable"]["top_score"] or 0), settings.rag_min_score)

    def test_ranking_quality_does_not_justify_a_reranker(self) -> None:
        """Measure whether retrieved correct chunks remain too low for the final context."""
        expected_chunks = self._seed_near_duplicate_corpus()
        cases = [
            (probe.query_class, probe.query, expected_chunks[probe.query_class])
            for probe in PROBES
        ]
        for name in ("Apollo", "Orion", "Vega", "Nova"):
            self._upload_text(
                f"{name.casefold()}-review.txt",
                f"The {name} program calibration station uses a routine safety review.",
            )
        _, broad_chunk_id = self._upload_text(
            "sentinel-review.txt",
            "The Sentinel program calibration station uses a three-stage safety review.",
        )
        cases.append(("broad_sentinel", "What does the Sentinel calibration station safety review include?", broad_chunk_id))
        vector_rankings: list[list[int]] = []
        hybrid_rankings: list[list[int]] = []
        relevant_sets: list[set[int]] = []
        vector_ranks = {}
        hybrid_ranks = {}
        for query_class, query, expected_chunk_id in cases:
            with patch.object(vector_search, "search_keyword_chunks", return_value=[]):
                vector_results = search_chunks(query, owner_id=1, limit=len(PROBES) * (DECOYS_PER_CLASS + 1), min_score=None)
            hybrid_results = search_chunks(query, owner_id=1, limit=len(PROBES) * (DECOYS_PER_CLASS + 1), min_score=None)
            expected = {expected_chunk_id}
            vector_ids = [int(result["chunk_id"]) for result in vector_results]
            hybrid_ids = [int(result["chunk_id"]) for result in hybrid_results]
            vector_rankings.append(vector_ids)
            hybrid_rankings.append(hybrid_ids)
            relevant_sets.append(expected)
            vector_ranks[query_class] = expected_chunk_rank(vector_ids, expected)
            hybrid_ranks[query_class] = expected_chunk_rank(hybrid_ids, expected)

        vector_mrr = mean_reciprocal_rank(vector_rankings, relevant_sets)
        hybrid_mrr = mean_reciprocal_rank(hybrid_rankings, relevant_sets)
        vector_precision_5 = sum(precision_at_k(ranking, relevant, 5) for ranking, relevant in zip(vector_rankings, relevant_sets)) / len(cases)
        hybrid_precision_5 = sum(precision_at_k(ranking, relevant, 5) for ranking, relevant in zip(hybrid_rankings, relevant_sets)) / len(cases)
        print("\nranking-quality report (one expected chunk per query):")
        print(f"  vector: MRR={vector_mrr:.4f}, Precision@5={vector_precision_5:.4f}, ranks={vector_ranks}")
        print(f"  hybrid: MRR={hybrid_mrr:.4f}, Precision@5={hybrid_precision_5:.4f}, ranks={hybrid_ranks}")

        self.assertGreaterEqual(hybrid_mrr, vector_mrr)
        self.assertGreaterEqual(hybrid_precision_5, vector_precision_5)
        self.assertTrue(all(rank is not None and rank <= 5 for rank in hybrid_ranks.values()))

    def test_fusion_configuration_controls_candidates_and_vector_rollback(self) -> None:
        """Candidate cutoffs are independent and vector mode bypasses lexical retrieval."""
        self._upload_text("nimbus-ledger-2026.txt", "The Nimbus ledger records monthly operating balances.")
        vector_limits = []

        class EmptyVectorStore:
            """Capture the vector candidate limit without adding vector results."""

            def search(self, *_args: object, **kwargs: object) -> list[dict[str, object]]:
                vector_limits.append(int(kwargs["limit"]))
                return []

        with (
            patch.object(vector_search.settings, "rag_retrieval_mode", "hybrid"),
            patch.object(vector_search.settings, "rag_vector_candidate_limit", 7),
            patch.object(vector_search.settings, "rag_keyword_candidate_limit", 9),
            patch.object(vector_search, "create_embeddings", return_value=[[1.0] + [0.0] * 383]),
            patch.object(vector_search, "get_vector_store", return_value=EmptyVectorStore()),
            patch.object(vector_search, "search_keyword_chunks", wraps=vector_search.search_keyword_chunks) as keyword_search,
        ):
            hybrid = search_chunks("nimbus ledger 2026", owner_id=1, limit=3, min_score=None)

        self.assertEqual(vector_limits, [7])
        self.assertEqual(keyword_search.call_args.kwargs["limit"], 9)
        self.assertTrue(hybrid)
        self.assertIsNone(hybrid[0]["vector_score"])
        self.assertIsNotNone(hybrid[0]["keyword_score"])

        with (
            patch.object(vector_search.settings, "rag_retrieval_mode", "vector"),
            patch.object(vector_search, "create_embeddings", return_value=[[1.0] + [0.0] * 383]),
            patch.object(vector_search, "get_vector_store", return_value=EmptyVectorStore()),
            patch.object(vector_search, "search_keyword_chunks", side_effect=AssertionError("vector rollback must not query keywords")),
        ):
            self.assertEqual(search_chunks("nimbus ledger 2026", owner_id=1, limit=3, min_score=None), [])


if __name__ == "__main__":
    unittest.main()
