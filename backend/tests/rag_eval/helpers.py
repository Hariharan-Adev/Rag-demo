"""Test-only helpers for separating RAG retrieval and generation evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
import unittest

from app.prompts.rag_prompt import UNAVAILABLE_ANSWER


CASE_FILE = Path(__file__).with_name("cases.json")
VALID_CATEGORIES = {
    "factual_lookup",
    "exact_name_lookup",
    "exact_number_lookup",
    "workbook_lookup",
    "aggregation",
    "comparison",
    "ambiguous_source",
    "unanswerable_query",
    "follow_up_query",
}
VALID_ROUTES = {"structured", "unstructured", "clarification", "unavailable", "follow_up"}


@dataclass(frozen=True)
class ExpectedDocument:
    """Stable document expectation; IDs are optional because fixtures can drift."""

    filename: str | None = None
    document_id: int | None = None


@dataclass(frozen=True)
class SourceIds:
    """Optional stable IDs used when a fixture controls database identifiers."""

    document_id: int | None = None
    version_id: int | None = None


@dataclass(frozen=True)
class RagEvalCase:
    """Portable case definition that can evaluate routing, retrieval, and answers."""

    id: str
    category: str
    query: str
    expected_route: str
    expected_document: ExpectedDocument | None
    expected_answer_facts: tuple[str, ...]
    expected_unavailable: bool
    expected_source_ids: SourceIds | None = None
    expected_keywords: tuple[str, ...] = ()
    expected_retrieved_chunk_ids: tuple[int, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RagEvalCase":
        """Validate one JSON object before a regression test uses it."""
        expected_document = value.get("expected_document")
        expected_source_ids = value.get("expected_source_ids")
        case = cls(
            id=str(value["id"]),
            category=str(value["category"]),
            query=str(value["query"]),
            expected_route=str(value["expected_route"]),
            expected_document=(
                ExpectedDocument(
                    filename=expected_document.get("filename"),
                    document_id=expected_document.get("document_id"),
                )
                if isinstance(expected_document, dict)
                else None
            ),
            expected_answer_facts=tuple(str(item) for item in value["expected_answer_facts"]),
            expected_unavailable=bool(value["expected_unavailable"]),
            expected_source_ids=(
                SourceIds(
                    document_id=expected_source_ids.get("document_id"),
                    version_id=expected_source_ids.get("version_id"),
                )
                if isinstance(expected_source_ids, dict)
                else None
            ),
            expected_keywords=tuple(str(item) for item in value.get("expected_keywords", [])),
            expected_retrieved_chunk_ids=tuple(
                int(item) for item in value.get("expected_retrieved_chunk_ids", [])
            ),
        )
        case.validate()
        return case

    def validate(self) -> None:
        """Fail early when a case is incomplete or uses an unknown category."""
        if not self.id.strip():
            raise ValueError("RAG eval case id is required.")
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"Unknown RAG eval category: {self.category}")
        if self.expected_route not in VALID_ROUTES:
            raise ValueError(f"Unknown RAG route: {self.expected_route}")
        if not self.query.strip():
            raise ValueError(f"RAG eval case {self.id} has an empty query.")
        if self.expected_unavailable and self.expected_answer_facts:
            raise ValueError(
                f"RAG eval case {self.id} cannot require facts and unavailable behavior."
            )


@dataclass(frozen=True)
class RetrievedChunk:
    """Normalized retrieval result from vector or structured search."""

    chunk_id: int | None
    document_id: int | None
    version_id: int | None
    filename: str
    text: str
    score: float | None = None


@dataclass(frozen=True)
class RetrievalObservation:
    """Observed retrieval/routing output before the answer-generation step."""

    route: str
    chunks: tuple[RetrievedChunk, ...] = ()
    selected_document_id: int | None = None
    unavailable: bool = False


@dataclass(frozen=True)
class GenerationObservation:
    """Observed answer output after context is supplied to the generator."""

    answer: str
    grounded: bool
    sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def load_eval_cases(path: Path = CASE_FILE) -> list[RagEvalCase]:
    """Load all JSON cases without importing app services or external evaluators."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("RAG eval cases must be a JSON array.")
    return [RagEvalCase.from_dict(item) for item in raw]


def cases_by_category(cases: list[RagEvalCase]) -> dict[str, list[RagEvalCase]]:
    """Group cases so future suites can run one category at a time."""
    grouped = {category: [] for category in sorted(VALID_CATEGORIES)}
    for case in cases:
        grouped[case.category].append(case)
    return grouped


def assert_retrieval_matches(
    test: unittest.TestCase,
    case: RagEvalCase,
    observation: RetrievalObservation,
) -> None:
    """Assert route, source, keyword, and optional chunk-ID retrieval expectations."""
    test.assertEqual(observation.route, case.expected_route)
    if case.expected_unavailable:
        test.assertTrue(observation.unavailable)
        return

    if case.expected_document is not None:
        filenames = {chunk.filename for chunk in observation.chunks}
        document_ids = {chunk.document_id for chunk in observation.chunks}
        if case.expected_document.filename is not None:
            test.assertIn(case.expected_document.filename, filenames)
        if case.expected_document.document_id is not None:
            test.assertIn(case.expected_document.document_id, document_ids)

    if case.expected_source_ids is not None:
        if case.expected_source_ids.document_id is not None:
            test.assertEqual(observation.selected_document_id, case.expected_source_ids.document_id)
        if case.expected_source_ids.version_id is not None:
            version_ids = {chunk.version_id for chunk in observation.chunks}
            test.assertIn(case.expected_source_ids.version_id, version_ids)

    if case.expected_retrieved_chunk_ids:
        chunk_ids = {chunk.chunk_id for chunk in observation.chunks}
        for chunk_id in case.expected_retrieved_chunk_ids:
            test.assertIn(chunk_id, chunk_ids)

    searchable_text = " ".join(chunk.text for chunk in observation.chunks).casefold()
    for keyword in case.expected_keywords:
        test.assertIn(keyword.casefold(), searchable_text)


def assert_generation_matches(
    test: unittest.TestCase,
    case: RagEvalCase,
    observation: GenerationObservation,
) -> None:
    """Assert answer facts separately from retrieval, including unavailable cases."""
    if case.expected_unavailable:
        test.assertFalse(observation.grounded)
        test.assertEqual(observation.answer, UNAVAILABLE_ANSWER)
        test.assertEqual(observation.sources, ())
        return

    test.assertTrue(observation.grounded)
    answer = observation.answer.casefold()
    for fact in case.expected_answer_facts:
        test.assertIn(fact.casefold(), answer)

