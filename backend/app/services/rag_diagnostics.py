"""Opt-in, content-free diagnostics for one internal RAG request."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Iterable

from app.database import get_connection
from app.services.document_access import READABLE_DOCUMENT_SQL


MAX_DIAGNOSTIC_TEXT = 2000
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[ _-]?key|access[ _-]?token|refresh[ _-]?token|auth[ _-]?token|token|password|"
    r"client[ _-]?secret|secret|authorization|proxy[ _-]?authorization|cookie|set[ _-]?cookie|"
    r"session(?:[ _-]?id)?|jwt)"
    r"(\s*(?:is|=|:)\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_AUTH_SCHEME_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_JWT_VALUE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_UUID_CONVERSATION_ID = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ALLOWED_METADATA_FILTERS = {"collection_id", "document_id", "version_id"}


def _safe_text(value: object) -> str:
    """Bound diagnostic text and redact common credential assignment forms."""
    text = " ".join(str(value or "").replace("\x00", "").split())
    text = _AUTH_SCHEME_VALUE.sub("[REDACTED_AUTH]", text)
    text = _JWT_VALUE.sub("[REDACTED_JWT]", text)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return text[:MAX_DIAGNOSTIC_TEXT]


def _optional_int(value: object) -> int | None:
    """Convert identifier-like values without retaining arbitrary metadata."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    """Convert score-like values while dropping malformed values safely."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_conversation_id(value: str | None) -> str | None:
    """Keep generated IDs readable and fingerprint arbitrary user-controlled IDs."""
    if not value:
        return None
    if _UUID_CONVERSATION_ID.fullmatch(value):
        return value
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


@dataclass
class RagRequestDiagnostic:
    """Safe internal trace; callers must opt in by passing an instance."""

    original_query: str | None = None
    resolved_follow_up_query: str | None = None
    conversation_id: str | None = None
    routing_decision: str | None = None
    routing_reason: str | None = None
    selected_document_ids: list[int] = field(default_factory=list)
    metadata_filters: dict[str, int] = field(default_factory=dict)
    retrieval_limit: int | None = None
    minimum_similarity_score: float | None = None
    retrieval_attempts: list[dict[str, object]] = field(default_factory=list)
    retrieved_chunk_ids: list[int] = field(default_factory=list)
    source_document_ids: list[int] = field(default_factory=list)
    similarity_scores: list[float | None] = field(default_factory=list)
    vector_scores: list[float | None] = field(default_factory=list)
    keyword_scores: list[float | None] = field(default_factory=list)
    fusion_scores: list[float | None] = field(default_factory=list)
    reranking_scores: list[float | None] = field(default_factory=list)
    structured_analysis_path: str | None = None
    final_selected_context_chunk_ids: list[int] = field(default_factory=list)
    grounded: bool | None = None
    unavailable: bool | None = None

    def _reset_observations(self) -> None:
        """Prevent accidental instance reuse from mixing separate requests."""
        self.resolved_follow_up_query = None
        self.routing_decision = None
        self.routing_reason = None
        self.selected_document_ids.clear()
        self.retrieval_limit = None
        self.minimum_similarity_score = None
        self.retrieval_attempts.clear()
        self.retrieved_chunk_ids.clear()
        self.source_document_ids.clear()
        self.similarity_scores.clear()
        self.vector_scores.clear()
        self.keyword_scores.clear()
        self.fusion_scores.clear()
        self.reranking_scores.clear()
        self.structured_analysis_path = None
        self.final_selected_context_chunk_ids.clear()
        self.grounded = None
        self.unavailable = None

    def start_request(
        self,
        *,
        query: str,
        conversation_id: str | None,
        collection_id: int | None,
        document_id: int | None,
        version_id: int | None,
    ) -> None:
        """Initialize safe request metadata without auth or header values."""
        self._reset_observations()
        # Free-form query text cannot be reliably scrubbed of every credential
        # or pasted private passage, so diagnostics retain only its schema slot.
        self.original_query = None
        self.conversation_id = _safe_conversation_id(conversation_id)
        supplied_filters = {
            "collection_id": collection_id,
            "document_id": document_id,
            "version_id": version_id,
        }
        self.metadata_filters = {
            key: int(value)
            for key, value in supplied_filters.items()
            if value is not None and key in _ALLOWED_METADATA_FILTERS
        }

    def record_retrieval_attempt(
        self,
        *,
        limit: int,
        min_score: float | None,
        sources: Iterable[dict[str, object]],
    ) -> None:
        """Record one primary or fallback retrieval attempt without source text."""
        source_list = list(sources)
        if self.retrieval_limit is None:
            self.retrieval_limit = int(limit)
            self.minimum_similarity_score = _optional_float(min_score)
        attempt_chunk_ids: list[int] = []
        attempt_document_ids: list[int] = []
        attempt_similarity_scores: list[float | None] = []
        attempt_vector_scores: list[float | None] = []
        attempt_keyword_scores: list[float | None] = []
        attempt_fusion_scores: list[float | None] = []
        attempt_reranking_scores: list[float | None] = []
        for source in source_list:
            chunk_id = _optional_int(source.get("chunk_id"))
            document_id = _optional_int(source.get("document_id"))
            if chunk_id is not None and chunk_id not in attempt_chunk_ids:
                attempt_chunk_ids.append(chunk_id)
                attempt_similarity_scores.append(_optional_float(source.get("score")))
                attempt_vector_scores.append(_optional_float(source.get("vector_score")))
                attempt_keyword_scores.append(_optional_float(source.get("keyword_score")))
                attempt_fusion_scores.append(_optional_float(source.get("fusion_score")))
                attempt_reranking_scores.append(_optional_float(source.get("reranking_score")))
            if document_id is not None and document_id not in attempt_document_ids:
                attempt_document_ids.append(document_id)
        self.retrieval_attempts.append({
            "limit": int(limit),
            "minimum_similarity_score": _optional_float(min_score),
            "retrieved_chunk_ids": attempt_chunk_ids,
            "source_document_ids": attempt_document_ids,
            "similarity_scores": attempt_similarity_scores,
            "vector_scores": attempt_vector_scores,
            "keyword_scores": attempt_keyword_scores,
            "fusion_scores": attempt_fusion_scores,
            "reranking_scores": attempt_reranking_scores,
        })
        self.record_retrieved_sources(source_list)

    def record_retrieved_sources(self, sources: Iterable[dict[str, object]]) -> None:
        """Copy only identifiers and scores from retrieved source dictionaries."""
        for source in sources:
            chunk_id = _optional_int(source.get("chunk_id"))
            document_id = _optional_int(source.get("document_id"))
            if chunk_id is not None and chunk_id not in self.retrieved_chunk_ids:
                self.retrieved_chunk_ids.append(chunk_id)
                self.similarity_scores.append(_optional_float(source.get("score")))
                self.vector_scores.append(_optional_float(source.get("vector_score")))
                self.keyword_scores.append(_optional_float(source.get("keyword_score")))
                self.fusion_scores.append(_optional_float(source.get("fusion_score")))
                self.reranking_scores.append(_optional_float(source.get("reranking_score")))
            if document_id is not None and document_id not in self.source_document_ids:
                self.source_document_ids.append(document_id)

    def record_selection(
        self,
        *,
        decision: str,
        reason: str,
        document_id: int | None,
    ) -> None:
        """Record the source-selection outcome without candidate content."""
        self.routing_decision = _safe_text(decision)
        self.routing_reason = _safe_text(reason)
        selected = _optional_int(document_id)
        if selected is not None and selected not in self.selected_document_ids:
            self.selected_document_ids.append(selected)

    def record_follow_up(
        self,
        *,
        result: dict[str, object],
        sources: Iterable[dict[str, object]],
        effective_query: str | None = None,
    ) -> None:
        """Record the actual follow-up outcome without retaining free-form text."""
        # Keep the field stable for a future privacy-safe representation; raw
        # rewritten queries have the same secret and private-content risks.
        self.resolved_follow_up_query = None
        self.routing_decision = "follow_up"
        if result.get("grounded"):
            self.routing_reason = "resolved_from_conversation_context"
        elif str(result.get("question_type") or "") == "clarification":
            self.routing_reason = "follow_up_requires_clarification"
        else:
            self.routing_reason = "follow_up_context_unavailable"
        self.record_retrieved_sources(sources)
        for document_id in self.source_document_ids:
            if document_id not in self.selected_document_ids:
                self.selected_document_ids.append(document_id)

    def record_structured_result(self, result: dict[str, object]) -> None:
        """Record structured planner type and document IDs, excluding row values."""
        context = result.get("_context")
        if not isinstance(context, dict):
            self.structured_analysis_path = "workbook_analysis"
        else:
            kind = _safe_text(context.get("kind") or "structured")
            result_type = _safe_text(context.get("result_type") or "unknown")
            self.structured_analysis_path = f"workbook_analysis:{kind}:{result_type}"
            for value in context.get("document_ids") or []:
                document_id = _optional_int(value)
                if document_id is not None and document_id not in self.selected_document_ids:
                    self.selected_document_ids.append(document_id)
        for source in result.get("sources") or []:
            if not isinstance(source, dict):
                continue
            document_id = _optional_int(source.get("document_id"))
            if document_id is not None and document_id not in self.source_document_ids:
                self.source_document_ids.append(document_id)

    def record_final_context(self, sources: Iterable[dict[str, object]]) -> None:
        """Record final LLM context chunk IDs without retaining chunk text."""
        for source in sources:
            chunk_id = _optional_int(source.get("chunk_id"))
            if chunk_id is not None and chunk_id not in self.final_selected_context_chunk_ids:
                self.final_selected_context_chunk_ids.append(chunk_id)

    def finalize(self, result: dict[str, object]) -> None:
        """Capture final grounding state while distinguishing clarification."""
        self.grounded = bool(result.get("grounded"))
        self.unavailable = (
            not self.grounded
            and str(result.get("question_type") or "") != "clarification"
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable allowlisted diagnostic representation."""
        return {
            "original_query": self.original_query,
            "resolved_follow_up_query": self.resolved_follow_up_query,
            "conversation_id": self.conversation_id,
            "routing_decision": self.routing_decision,
            "routing_reason": self.routing_reason,
            "selected_document_ids": list(self.selected_document_ids),
            "metadata_filters": dict(self.metadata_filters),
            "retrieval_limit": self.retrieval_limit,
            "minimum_similarity_score": self.minimum_similarity_score,
            "retrieval_attempts": [dict(attempt) for attempt in self.retrieval_attempts],
            "retrieved_chunk_ids": list(self.retrieved_chunk_ids),
            "source_document_ids": list(self.source_document_ids),
            "similarity_scores": list(self.similarity_scores),
            "vector_scores": list(self.vector_scores),
            "keyword_scores": list(self.keyword_scores),
            "fusion_scores": list(self.fusion_scores),
            "reranking_scores": list(self.reranking_scores),
            "structured_analysis_path": self.structured_analysis_path,
            "final_selected_context_chunk_ids": list(self.final_selected_context_chunk_ids),
            "grounded": self.grounded,
            "unavailable": self.unavailable,
        }


def _allowed_trace_ids(
    *,
    user: dict[str, object],
    document_ids: set[int],
    chunk_ids: set[int],
) -> tuple[set[int], set[int]]:
    """Recheck every traced identifier against the caller's current ACL."""
    organization_id = str(user.get("organization_id") or "")
    user_id = _optional_int(user.get("id"))
    if not organization_id or user_id is None:
        return set(), set()
    allowed_documents: set[int] = set()
    allowed_chunks: set[int] = set()
    with get_connection() as connection:
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            rows = connection.execute(
                f"""SELECT d.id FROM documents d
                    WHERE {READABLE_DOCUMENT_SQL}
                      AND d.id IN ({placeholders})""",
                (
                    organization_id,
                    user_id,
                    user_id,
                    *sorted(document_ids),
                ),
            ).fetchall()
            allowed_documents.update(int(row["id"]) for row in rows)
        if chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            rows = connection.execute(
                f"""SELECT c.id, c.document_id
                    FROM chunks c
                    JOIN documents d
                      ON d.id = c.document_id
                     AND d.organization_id = c.organization_id
                    JOIN document_versions dv
                      ON dv.id = c.version_id
                     AND dv.document_id = d.id
                     AND dv.organization_id = d.organization_id
                    WHERE {READABLE_DOCUMENT_SQL}
                      AND c.deleted_at IS NULL
                      AND dv.deleted_at IS NULL
                      AND c.id IN ({placeholders})""",
                (
                    organization_id,
                    user_id,
                    user_id,
                    *sorted(chunk_ids),
                ),
            ).fetchall()
            allowed_chunks.update(int(row["id"]) for row in rows)
            allowed_documents.update(int(row["document_id"]) for row in rows)
    return allowed_documents, allowed_chunks


def _filter_ranked_chunks(
    chunk_ids: Iterable[object],
    similarity_scores: Iterable[object],
    reranking_scores: Iterable[object],
    vector_scores: Iterable[object],
    keyword_scores: Iterable[object],
    fusion_scores: Iterable[object],
    allowed_chunks: set[int],
) -> tuple[list[int], list[object], list[object], list[object], list[object], list[object]]:
    """Keep score arrays aligned while removing unauthorized chunk IDs."""
    chunk_list = list(chunk_ids)
    similarity_list = list(similarity_scores)
    reranking_list = list(reranking_scores)
    vector_list = list(vector_scores)
    keyword_list = list(keyword_scores)
    fusion_list = list(fusion_scores)
    kept_chunks: list[int] = []
    kept_similarity: list[object] = []
    kept_reranking: list[object] = []
    kept_vector: list[object] = []
    kept_keyword: list[object] = []
    kept_fusion: list[object] = []
    for index, raw_chunk_id in enumerate(chunk_list):
        chunk_id = _optional_int(raw_chunk_id)
        if chunk_id is None or chunk_id not in allowed_chunks:
            continue
        kept_chunks.append(chunk_id)
        kept_similarity.append(
            similarity_list[index] if index < len(similarity_list) else None
        )
        kept_reranking.append(
            reranking_list[index] if index < len(reranking_list) else None
        )
        kept_vector.append(vector_list[index] if index < len(vector_list) else None)
        kept_keyword.append(keyword_list[index] if index < len(keyword_list) else None)
        kept_fusion.append(fusion_list[index] if index < len(fusion_list) else None)
    return kept_chunks, kept_similarity, kept_reranking, kept_vector, kept_keyword, kept_fusion


def authorized_diagnostic_payload(
    diagnostic: RagRequestDiagnostic,
    user: dict[str, object],
) -> dict[str, object]:
    """Build a content-free endpoint response with defense-in-depth ACL filtering."""
    trace = diagnostic.to_dict()
    attempts = [
        attempt for attempt in trace["retrieval_attempts"]
        if isinstance(attempt, dict)
    ]
    document_ids = {
        document_id
        for value in (
            *trace["selected_document_ids"],
            *trace["source_document_ids"],
            *(item for attempt in attempts for item in attempt["source_document_ids"]),
        )
        if (document_id := _optional_int(value)) is not None
    }
    chunk_ids = {
        chunk_id
        for value in (
            *trace["retrieved_chunk_ids"],
            *trace["final_selected_context_chunk_ids"],
            *(item for attempt in attempts for item in attempt["retrieved_chunk_ids"]),
        )
        if (chunk_id := _optional_int(value)) is not None
    }
    allowed_documents, allowed_chunks = _allowed_trace_ids(
        user=user,
        document_ids=document_ids,
        chunk_ids=chunk_ids,
    )
    retrieved_chunks, similarity_scores, reranking_scores, vector_scores, keyword_scores, fusion_scores = _filter_ranked_chunks(
        trace["retrieved_chunk_ids"],
        trace["similarity_scores"],
        trace["reranking_scores"],
        trace["vector_scores"],
        trace["keyword_scores"],
        trace["fusion_scores"],
        allowed_chunks,
    )
    safe_attempts: list[dict[str, object]] = []
    for attempt in attempts:
        attempt_chunks, attempt_similarity, attempt_reranking, attempt_vector, attempt_keyword, attempt_fusion = _filter_ranked_chunks(
            attempt["retrieved_chunk_ids"],
            attempt["similarity_scores"],
            attempt["reranking_scores"],
            attempt["vector_scores"],
            attempt["keyword_scores"],
            attempt["fusion_scores"],
            allowed_chunks,
        )
        safe_attempts.append({
            "limit": attempt["limit"],
            "minimum_similarity_score": attempt["minimum_similarity_score"],
            "retrieved_chunk_ids": attempt_chunks,
            "source_document_ids": [
                value for value in attempt["source_document_ids"]
                if _optional_int(value) in allowed_documents
            ],
            "similarity_scores": attempt_similarity,
            "reranking_scores": attempt_reranking,
            "vector_scores": attempt_vector,
            "keyword_scores": attempt_keyword,
            "fusion_scores": attempt_fusion,
        })
    unavailable = bool(trace["unavailable"])
    return {
        "capability": "rag_diagnostic",
        "development_capability": True,
        "query": None,
        "query_omitted": True,
        "routing_decision": trace["routing_decision"],
        "routing_reason": trace["routing_reason"],
        "selected_sources": {
            "selected_document_ids": [
                value for value in trace["selected_document_ids"]
                if _optional_int(value) in allowed_documents
            ],
            "source_document_ids": [
                value for value in trace["source_document_ids"]
                if _optional_int(value) in allowed_documents
            ],
        },
        "metadata_filters": trace["metadata_filters"],
        "retrieval": {
            "limit": trace["retrieval_limit"],
            "minimum_similarity_score": trace["minimum_similarity_score"],
            "attempts": safe_attempts,
        },
        "retrieved_chunks": {
            "chunk_ids": retrieved_chunks,
            "similarity_scores": similarity_scores,
            "reranking_scores": reranking_scores,
            "vector_scores": vector_scores,
            "keyword_scores": keyword_scores,
            "fusion_scores": fusion_scores,
            "text_previews": [],
        },
        "structured_analysis_path": trace["structured_analysis_path"],
        "final_context_selection": {
            "chunk_ids": [
                value for value in trace["final_selected_context_chunk_ids"]
                if _optional_int(value) in allowed_chunks
            ],
        },
        "grounded": trace["grounded"],
        "unavailable": unavailable,
        "unavailable_reason": trace["routing_reason"] if unavailable else None,
    }
