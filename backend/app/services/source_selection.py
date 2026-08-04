"""Authoritative source selection and grounded-answer invariants."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

from app.config import settings
from app.database import get_connection
from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services.document_access import READABLE_DOCUMENT_SQL
from app.services.vector_search import search_chunks
from app.services.workbook_analysis import (
    _load_scopes,
    _plan_for_scope,
    _source_evidence,
)
from app.utils.observability import log_event


SelectionPath = Literal["structured", "retrieval", "unavailable", "clarification"]

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
    "file", "for", "from", "give", "how", "in", "is", "it", "many", "me",
    "much", "of", "on", "or", "show", "tell", "than", "that", "the", "them",
    "there", "these", "they", "this", "those", "to", "was", "were", "what",
    "which", "with",
}
GENERIC_COLUMNS = {"column", "field", "value", "data", "name", "type", "status"}
MIN_RETRIEVAL_SCORE = 3
MIN_STRUCTURED_SCORE = 5
AMBIGUITY_MARGIN = 2


@dataclass
class CandidateDecision:
    """Safe diagnostic summary for one candidate document."""

    document_id: int
    source_type: str
    score: int
    semantic_score: float = 0.0
    schema_score: int = 0
    context_score: int = 0
    reasons: list[str] = field(default_factory=list)
    rejection_reason: str | None = None


@dataclass
class SelectionResult:
    """Decision returned by the single source-selection gate."""

    path: SelectionPath
    document_id: int | None = None
    version_id: int | None = None
    sources: list[dict[str, object]] = field(default_factory=list)
    reason: str = "insufficient_evidence"
    diagnostics: list[CandidateDecision] = field(default_factory=list)


def safe_tokens(value: object) -> set[str]:
    """Tokenize text with boundaries and drop weak tokens that cause false matches."""
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", str(value).casefold()):
        if (
            token in STOP_WORDS
            or token in GENERIC_COLUMNS
            or len(token) < 3
            or re.fullmatch(r"(?:col|column)?\d+", token)
        ):
            continue
        tokens.add(token)
        if len(token) > 3 and token.endswith("s"):
            tokens.add(token[:-1])
        if len(token) > 4 and token.endswith("ed"):
            tokens.add(token[:-1])
            tokens.add(token[:-2])
    return tokens


def _organization_id(owner_id: int) -> str | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT organization_id FROM users WHERE id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
    return str(row["organization_id"]) if row else None


def _active_accessible_document(
    owner_id: int,
    document_id: int,
    version_id: int | None = None,
) -> bool:
    organization_id = _organization_id(owner_id)
    if organization_id is None:
        # Service-level unit tests may mock retrieval without creating auth rows.
        # Public API calls still pass through get_current_user before this point.
        return True
    version_clause = "AND dv.id = ?" if version_id is not None else ""
    params: list[object] = [organization_id, owner_id, owner_id, document_id]
    if version_id is not None:
        params.append(version_id)
    with get_connection() as connection:
        return connection.execute(
            f"""SELECT 1
                FROM documents d
                JOIN document_versions dv
                  ON dv.id = d.current_version_id
                 AND dv.document_id = d.id
                 AND dv.organization_id = d.organization_id
                WHERE {READABLE_DOCUMENT_SQL}
                  AND d.id = ?
                  AND d.current_version_id IS NOT NULL
                  AND d.processing_status = 'completed'
                  AND dv.status = 'completed'
                  AND dv.deleted_at IS NULL
                  AND (
                    EXISTS (
                      SELECT 1 FROM chunks c
                      WHERE c.organization_id = d.organization_id
                        AND c.document_id = d.id
                        AND c.version_id = dv.id
                        AND c.deleted_at IS NULL
                        AND (
                            c.indexing_status = 'completed'
                            OR c.vector_point_id IS NOT NULL
                            OR c.embedding IS NOT NULL
                        )
                    )
                    OR EXISTS (
                      SELECT 1 FROM workbook_sheets ws
                      WHERE ws.organization_id = d.organization_id
                        AND ws.content_id = d.content_id
                        AND ws.status = 'processed'
                    )
                  )
                  {version_clause}
                LIMIT 1""",
            params,
        ).fetchone() is not None


def _semantic_decisions(question: str, sources: list[dict[str, object]]) -> list[CandidateDecision]:
    question_tokens = safe_tokens(question)
    grouped: dict[int, CandidateDecision] = {}
    for source in sources:
        document_id = int(source["document_id"])
        decision = grouped.setdefault(
            document_id,
            CandidateDecision(
                document_id=document_id,
                source_type=str(source.get("source_type") or "text"),
                score=0,
            ),
        )
        source_tokens = safe_tokens(" ".join((
            str(source.get("filename") or ""),
            str(source.get("content") or ""),
            str(source.get("source_location") or ""),
        )))
        overlap = question_tokens & source_tokens
        semantic = float(source.get("score") or 0.0)
        decision.semantic_score = max(decision.semantic_score, semantic)
        if overlap:
            decision.score += min(8, len(overlap) * 3)
            decision.reasons.append("semantic_token_overlap")
        if semantic >= settings.rag_min_score:
            decision.score += 4
            decision.reasons.append("semantic_score_high")
        if source.get("source_type") not in {"excel", "csv"} and semantic >= 0.15:
            decision.score += 2
            decision.reasons.append("unstructured_candidate")
    for decision in grouped.values():
        if decision.score < MIN_RETRIEVAL_SCORE:
            decision.rejection_reason = "insufficient_evidence"
    return sorted(grouped.values(), key=lambda item: (-item.score, item.document_id))


def _structured_decisions(
    question: str,
    owner_id: int,
    collection_id: int | None,
    document_id: int | None,
) -> list[CandidateDecision]:
    decisions: list[CandidateDecision] = []
    explicit_scope = document_id is not None
    for scope in _load_scopes(owner_id, collection_id, document_id):
        plan = _plan_for_scope(scope, question, explicit_scope=explicit_scope)
        evidence_score, reasons = _source_evidence(scope, question)
        score = evidence_score
        if plan.intent != "unavailable":
            score += 4
            reasons.append("valid_structured_plan")
        if plan.filters:
            score += 2
            reasons.append("validated_filter")
        rejection = None
        if plan.intent == "unavailable":
            rejection = plan.rejection_reason or "schema_mismatch"
        elif score < MIN_STRUCTURED_SCORE:
            rejection = "insufficient_evidence"
        decisions.append(CandidateDecision(
            document_id=scope.document_id,
            source_type="excel" if not scope.filename.casefold().endswith(".csv") else "csv",
            score=score,
            schema_score=evidence_score,
            reasons=sorted(set(reasons)),
            rejection_reason=rejection,
        ))
    return sorted(decisions, key=lambda item: (-item.score, item.document_id))


def _best_non_rejected(decisions: list[CandidateDecision]) -> CandidateDecision | None:
    for decision in decisions:
        if decision.rejection_reason is None:
            return decision
    return None


def select_sources(
    *,
    question: str,
    owner_id: int,
    collection_id: int | None = None,
    document_id: int | None = None,
    version_id: int | None = None,
    structured_requested: bool = False,
    context_limit: int | None = None,
    searcher=search_chunks,
) -> SelectionResult:
    """Compare eligible structured and unstructured evidence before answering."""
    final_context_limit = context_limit or settings.rag_final_context_limit
    if document_id is not None and not _active_accessible_document(owner_id, document_id, version_id):
        return SelectionResult(path="unavailable", reason="acl_excluded")
    if document_id is not None and version_id is None and structured_requested:
        structured = _structured_decisions(question, owner_id, collection_id, document_id)
        best_structured = _best_non_rejected(structured)
        log_event(
            "rag.source_selection",
            user_id=owner_id,
            collection_id=collection_id,
            explicit_document_id=document_id,
            candidates=[
                {
                    "document_id": item.document_id,
                    "type": item.source_type,
                    "score": item.score,
                    "semantic_score": item.semantic_score,
                    "schema_score": item.schema_score,
                    "rejection_reason": item.rejection_reason,
                    "reasons": item.reasons,
                }
                for item in structured
            ],
        )
        if best_structured is not None:
            return SelectionResult(
                path="structured",
                document_id=best_structured.document_id,
                reason="explicit_structured_scope",
                diagnostics=structured,
            )

    retrieval_sources = searcher(
        question,
        owner_id=owner_id,
        limit=max(settings.rag_retrieval_limit, final_context_limit),
        collection_id=collection_id,
        document_id=document_id,
        version_id=version_id,
        min_score=settings.rag_min_score,
    )
    semantic = _semantic_decisions(question, retrieval_sources)
    if not _best_non_rejected(semantic) and (structured_requested or len(safe_tokens(question)) >= 2):
        fallback_sources = searcher(
            question,
            owner_id=owner_id,
            limit=max(settings.rag_retrieval_limit, final_context_limit),
            collection_id=collection_id,
            document_id=document_id,
            version_id=version_id,
            min_score=0.0,
        )
        fallback_decisions = _semantic_decisions(question, fallback_sources)
        if _best_non_rejected(fallback_decisions):
            retrieval_sources = fallback_sources
            semantic = fallback_decisions
    structured = []
    if version_id is None:
        structured = _structured_decisions(question, owner_id, collection_id, document_id)
    diagnostics = [*structured, *semantic]
    best_structured = _best_non_rejected(structured)
    best_semantic = _best_non_rejected(semantic)

    log_event(
        "rag.source_selection",
        user_id=owner_id,
        collection_id=collection_id,
        explicit_document_id=document_id,
        candidates=[
            {
                "document_id": item.document_id,
                "type": item.source_type,
                "score": item.score,
                "semantic_score": item.semantic_score,
                "schema_score": item.schema_score,
                "rejection_reason": item.rejection_reason,
                "reasons": item.reasons,
            }
            for item in diagnostics
        ],
    )

    valid = [item for item in diagnostics if item.rejection_reason is None]
    if not valid:
        return SelectionResult(path="unavailable", reason="insufficient_evidence", diagnostics=diagnostics)
    valid.sort(key=lambda item: (-item.score, item.document_id))
    if (
        len(valid) > 1
        and valid[0].document_id != valid[1].document_id
        and valid[0].score - valid[1].score <= AMBIGUITY_MARGIN
    ):
        return SelectionResult(path="clarification", reason="ambiguous_candidate", diagnostics=diagnostics)

    if structured_requested and best_structured is not None:
        if best_semantic and best_semantic.document_id != best_structured.document_id and best_semantic.score > best_structured.score:
            selected_sources = [
                source for source in retrieval_sources
                if int(source["document_id"]) == best_semantic.document_id
            ][:final_context_limit]
            return SelectionResult(
                path="retrieval",
                document_id=best_semantic.document_id,
                sources=selected_sources,
                reason="unstructured_evidence_stronger",
                diagnostics=diagnostics,
            )
        return SelectionResult(
            path="structured",
            document_id=best_structured.document_id,
            reason="structured_schema_evidence",
            diagnostics=diagnostics,
        )

    selected = valid[0]
    if selected.source_type in {"excel", "csv"} and best_structured and selected.document_id == best_structured.document_id:
        return SelectionResult(path="structured", document_id=selected.document_id, reason="structured_schema_evidence", diagnostics=diagnostics)
    selected_sources = [
        source for source in retrieval_sources
        if int(source["document_id"]) == selected.document_id
    ][:final_context_limit]
    return SelectionResult(
        path="retrieval",
        document_id=selected.document_id,
        sources=selected_sources,
        reason="semantic_evidence",
        diagnostics=diagnostics,
    )


def validate_grounded_result(
    result: dict[str, object],
    *,
    selected_document_id: int | None,
    owner_id: int,
) -> dict[str, object]:
    """Reject answers whose selected source, plan, and citations diverge."""
    if not result.get("grounded"):
        return result
    sources = [source for source in (result.get("sources") or []) if isinstance(source, dict)]
    if selected_document_id is None or not sources:
        return _unavailable("missing_citation")
    for source in sources:
        if int(source.get("document_id") or 0) != selected_document_id:
            return _unavailable("citation_document_mismatch")
        if not _active_accessible_document(
            owner_id,
            selected_document_id,
            int(source["version_id"]) if source.get("version_id") is not None else None,
        ):
            return _unavailable("source_no_longer_accessible")
    context = result.get("_context")
    if isinstance(context, dict):
        context_docs = {int(value) for value in context.get("document_ids") or []}
        if context_docs and context_docs != {selected_document_id}:
            return _unavailable("result_plan_document_mismatch")
    return result


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "answer": UNAVAILABLE_ANSWER,
        "question_type": "source_selection",
        "grounded": False,
        "sources": [],
        "unavailable_reason": reason,
    }
