"""Tests for opt-in, content-free RAG request diagnostics."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.services.rag_diagnostics import RagRequestDiagnostic
from app.services.rag_service import answer_question
from app.services.source_selection import SelectionResult, select_sources


class RagRequestDiagnosticTests(unittest.TestCase):
    """Verify trace metadata and the strict diagnostic allowlist."""

    def test_serialized_trace_has_only_the_explicit_safe_schema(self) -> None:
        diagnostic = RagRequestDiagnostic()
        diagnostic.start_request(
            query="private query",
            conversation_id="conversation-allowlist",
            collection_id=1,
            document_id=2,
            version_id=3,
        )

        payload = diagnostic.to_dict()

        self.assertEqual(
            set(payload),
            {
                "original_query",
                "resolved_follow_up_query",
                "conversation_id",
                "routing_decision",
                "routing_reason",
                "selected_document_ids",
                "metadata_filters",
                "retrieval_limit",
                "minimum_similarity_score",
                "retrieval_attempts",
                "retrieved_chunk_ids",
                "source_document_ids",
                "similarity_scores",
                "vector_scores",
                "keyword_scores",
                "fusion_scores",
                "reranking_scores",
                "structured_analysis_path",
                "final_selected_context_chunk_ids",
                "grounded",
                "unavailable",
            },
        )
        serialized = json.dumps(payload).casefold()
        for forbidden_name in (
            "api_key",
            "access_token",
            "password",
            "client_secret",
            "authorization",
            "authentication_headers",
            "document_content",
        ):
            self.assertNotIn(forbidden_name, serialized)

    def test_trace_records_retrieval_metadata_without_contents_or_secrets(self) -> None:
        diagnostic = RagRequestDiagnostic()
        sources = [
            {
                "chunk_id": 101,
                "document_id": 12,
                "score": 0.81,
                "vector_score": 0.81,
                "keyword_score": 2.15,
                "fusion_score": 0.0325,
                "reranking_score": 0.92,
                "content": "full private document contents",
                "api_key": "source-secret",
            },
            {
                "chunk_id": 102,
                "document_id": 12,
                "score": 0.72,
                "content": "another private passage",
            },
        ]

        diagnostic.start_request(
            query=(
                "Find policy password=hunter2 api_key=key-123 client_secret=client-789 "
                "Authorization: Bearer auth-456 Basic dXNlcjpwYXNz Cookie: session=abc123 "
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123 "
                "OpenAI key sk-proj-secretvalue AWS key AKIAIOSFODNN7EXAMPLE "
                "password is correct horse battery staple Digest username=private"
            ),
            conversation_id="sk-proj-conversation-secret",
            collection_id=3,
            document_id=12,
            version_id=14,
        )
        diagnostic.record_retrieval_attempt(limit=15, min_score=0.3, sources=sources)
        diagnostic.record_selection(decision="retrieval", reason="semantic_evidence", document_id=12)
        diagnostic.record_final_context(sources[:1])
        diagnostic.finalize({"grounded": True, "sources": []})
        payload = diagnostic.to_dict()
        serialized = json.dumps(payload)

        self.assertEqual(payload["retrieval_limit"], 15)
        self.assertEqual(payload["minimum_similarity_score"], 0.3)
        self.assertEqual(len(payload["retrieval_attempts"]), 1)
        self.assertEqual(payload["retrieved_chunk_ids"], [101, 102])
        self.assertEqual(payload["source_document_ids"], [12])
        self.assertEqual(payload["similarity_scores"], [0.81, 0.72])
        self.assertEqual(payload["vector_scores"], [0.81, None])
        self.assertEqual(payload["keyword_scores"], [2.15, None])
        self.assertEqual(payload["fusion_scores"], [0.0325, None])
        self.assertEqual(payload["reranking_scores"], [0.92, None])
        self.assertEqual(payload["final_selected_context_chunk_ids"], [101])
        self.assertTrue(payload["grounded"])
        self.assertFalse(payload["unavailable"])
        for forbidden_value in (
            "hunter2",
            "key-123",
            "auth-456",
            "client-789",
            "dXNlcjpwYXNz",
            "abc123",
            "eyJhbGciOiJIUzI1NiJ9",
            "sk-proj-secretvalue",
            "AKIAIOSFODNN7EXAMPLE",
            "horse battery staple",
            "username=private",
            "sk-proj-conversation-secret",
            "chat-1234567890-skprojsecretvalue",
            "source-secret",
            "full private document contents",
            "another private passage",
        ):
            self.assertNotIn(forbidden_value, serialized)
        for forbidden_key in ("api_key", "token", "password", "headers", "content"):
            self.assertNotIn(forbidden_key, payload)

    def test_answer_question_populates_retrieval_trace_when_explicitly_enabled(self) -> None:
        diagnostic = RagRequestDiagnostic()
        conversation_id = "123e4567-e89b-42d3-a456-426614174000"
        sources = [
            {
                "chunk_id": 201,
                "document_id": 22,
                "version_id": 23,
                "filename": "synthetic-policy.txt",
                "content": "Synthetic policy evidence.",
                "source_type": "text",
                "source_location": {"section": "body"},
                "score": 0.88,
            }
        ]

        def select_stub(**kwargs):
            trace = kwargs["diagnostic"]
            trace.record_retrieval_attempt(limit=15, min_score=0.3, sources=sources)
            return SelectionResult(
                path="retrieval",
                document_id=22,
                sources=sources,
                reason="semantic_evidence",
            )

        with patch("app.services.rag_service.resolve_follow_up", return_value=None), patch(
            "app.services.rag_service.has_structured_workbook", return_value=False
        ), patch("app.services.rag_service.select_sources", side_effect=select_stub), patch(
            "app.services.rag_service.generate_answer",
            return_value={"answer": "Grounded answer.", "prompt_tokens": 1, "completion_tokens": 1},
        ), patch("app.services.rag_service.validate_grounded_result", side_effect=lambda result, **kwargs: result), patch(
            "app.services.rag_service.reserve_groq_call"
        ), patch("app.services.rag_service.record_groq_tokens"), patch(
            "app.services.rag_service.save_grounded_context"
        ), patch("app.services.rag_service.log_audit_event"):
            result = answer_question(
                "What does the policy say?",
                7,
                collection_id=4,
                conversation_id=conversation_id,
                diagnostic=diagnostic,
            )

        payload = diagnostic.to_dict()
        self.assertTrue(result["grounded"])
        self.assertIsNone(payload["original_query"])
        self.assertEqual(payload["conversation_id"], conversation_id)
        self.assertEqual(payload["routing_decision"], "retrieval")
        self.assertEqual(payload["routing_reason"], "semantic_evidence")
        self.assertEqual(payload["selected_document_ids"], [22])
        self.assertEqual(payload["metadata_filters"], {"collection_id": 4})
        self.assertEqual(payload["retrieved_chunk_ids"], [201])
        self.assertEqual(payload["final_selected_context_chunk_ids"], [201])
        self.assertTrue(payload["grounded"])
        self.assertFalse(payload["unavailable"])

    def test_structured_trace_records_path_without_private_filter_values(self) -> None:
        diagnostic = RagRequestDiagnostic()
        structured = {
            "answer": "Count: 2",
            "question_type": "structured_analysis",
            "grounded": True,
            "sources": [{"document_id": 31, "version_id": 32, "filename": "rows.xlsx"}],
            "_context": {
                "kind": "structured_rows",
                "result_type": "count",
                "document_ids": [31],
                "filters": {"Employee": ["Private Person"]},
                "contributing_values": ["private-cell-value"],
            },
        }
        selection = SelectionResult(
            path="structured",
            document_id=31,
            reason="structured_schema_evidence",
        )

        with patch("app.services.rag_service.resolve_follow_up", return_value=None), patch(
            "app.services.rag_service.has_structured_workbook", return_value=True
        ), patch("app.services.rag_service.is_structured_lookup_question", return_value=True), patch(
            "app.services.rag_service.select_sources", return_value=selection
        ), patch("app.services.rag_service.analyze_workbook_question", return_value=structured), patch(
            "app.services.rag_service.validate_grounded_result", side_effect=lambda result, **kwargs: result
        ), patch("app.services.rag_service.save_grounded_context") as save_context, patch(
            "app.services.rag_service.log_audit_event"
        ):
            result = answer_question(
                "How many records?",
                7,
                document_id=31,
                diagnostic=diagnostic,
                persist_context=False,
            )

        payload = diagnostic.to_dict()
        serialized = json.dumps(payload)
        self.assertTrue(result["grounded"])
        self.assertEqual(payload["routing_decision"], "structured")
        self.assertEqual(payload["selected_document_ids"], [31])
        self.assertEqual(payload["source_document_ids"], [31])
        self.assertEqual(
            payload["structured_analysis_path"],
            "workbook_analysis:structured_rows:count",
        )
        self.assertNotIn("Private Person", serialized)
        self.assertNotIn("private-cell-value", serialized)
        save_context.assert_not_called()

    def test_structured_trace_keeps_source_ids_without_internal_context(self) -> None:
        diagnostic = RagRequestDiagnostic()

        diagnostic.record_structured_result({"sources": [{"document_id": 33}]})

        payload = diagnostic.to_dict()
        self.assertEqual(payload["structured_analysis_path"], "workbook_analysis")
        self.assertEqual(payload["source_document_ids"], [33])

    def test_follow_up_trace_records_effective_query_and_status(self) -> None:
        diagnostic = RagRequestDiagnostic()
        follow_up = {
            "answer": "Prior grounded values.",
            "question_type": "follow_up",
            "grounded": True,
            "sources": [{"document_id": 41, "version_id": 42, "filename": "rows.xlsx"}],
        }

        with patch("app.services.rag_service.resolve_follow_up", return_value=follow_up), patch(
            "app.services.rag_service.validate_grounded_result", side_effect=lambda result, **kwargs: result
        ), patch("app.services.rag_service.log_audit_event"):
            answer_question(
                "show those",
                7,
                conversation_id="chat-follow-up",
                diagnostic=diagnostic,
            )

        payload = diagnostic.to_dict()
        self.assertIsNone(payload["resolved_follow_up_query"])
        self.assertEqual(payload["routing_decision"], "follow_up")
        self.assertEqual(payload["routing_reason"], "resolved_from_conversation_context")
        self.assertEqual(payload["selected_document_ids"], [41])
        self.assertTrue(payload["grounded"])

    def test_unavailable_follow_up_is_not_reported_as_context_resolution(self) -> None:
        diagnostic = RagRequestDiagnostic()
        follow_up = {"answer": "Unavailable", "grounded": False, "sources": []}

        with patch("app.services.rag_service.resolve_follow_up", return_value=follow_up), patch(
            "app.services.rag_service.validate_grounded_result", side_effect=lambda result, **kwargs: result
        ), patch("app.services.rag_service.log_audit_event"):
            answer_question(
                "show those",
                7,
                conversation_id="missing-context",
                diagnostic=diagnostic,
            )

        payload = diagnostic.to_dict()
        self.assertEqual(payload["routing_decision"], "follow_up")
        self.assertEqual(payload["routing_reason"], "follow_up_context_unavailable")
        self.assertIsNone(payload["resolved_follow_up_query"])
        self.assertFalse(payload["grounded"])
        self.assertTrue(payload["unavailable"])

    def test_source_selection_records_primary_and_fallback_attempts(self) -> None:
        diagnostic = RagRequestDiagnostic()
        primary = [{
            "chunk_id": 301,
            "document_id": 51,
            "source_type": "text",
            "content": "unrelated evidence",
            "score": 0.1,
        }]
        fallback = [{
            "chunk_id": 302,
            "document_id": 52,
            "source_type": "text",
            "content": "alpha beta evidence",
            "score": 0.2,
        }]

        with patch(
            "app.services.source_selection._confident_document_id_from_question",
            return_value=None,
        ), patch("app.services.source_selection._structured_decisions", return_value=[]), patch(
            "app.services.source_selection.log_event"
        ):
            selection = select_sources(
                question="alpha beta",
                owner_id=7,
                searcher=lambda *args, **kwargs: primary if kwargs["min_score"] else fallback,
                diagnostic=diagnostic,
            )

        attempts = diagnostic.to_dict()["retrieval_attempts"]
        self.assertEqual(selection.path, "retrieval")
        self.assertEqual(len(attempts), 2)
        self.assertGreater(float(attempts[0]["minimum_similarity_score"]), 0.0)
        self.assertEqual(attempts[0]["retrieved_chunk_ids"], [301])
        self.assertEqual(attempts[1]["minimum_similarity_score"], 0.0)
        self.assertEqual(attempts[1]["retrieved_chunk_ids"], [302])

    def test_start_request_resets_reused_trace_state(self) -> None:
        diagnostic = RagRequestDiagnostic()
        diagnostic.start_request(
            query="first",
            conversation_id="one",
            collection_id=1,
            document_id=None,
            version_id=None,
        )
        diagnostic.record_selection(decision="retrieval", reason="first", document_id=10)
        diagnostic.finalize({"grounded": True})

        diagnostic.start_request(
            query="second",
            conversation_id="two",
            collection_id=2,
            document_id=None,
            version_id=None,
        )

        payload = diagnostic.to_dict()
        self.assertEqual(payload["metadata_filters"], {"collection_id": 2})
        self.assertEqual(payload["selected_document_ids"], [])
        self.assertIsNone(payload["routing_decision"])
        self.assertIsNone(payload["grounded"])

    def test_query_text_is_always_omitted_from_the_trace(self) -> None:
        diagnostic = RagRequestDiagnostic()

        diagnostic.start_request(
            query="Private Person private@example.com pasted document text",
            conversation_id="chat-minimal",
            collection_id=None,
            document_id=None,
            version_id=None,
        )

        payload = diagnostic.to_dict()
        self.assertIsNone(payload["original_query"])
        self.assertNotIn("Private Person", json.dumps(payload))
        self.assertNotIn("private@example.com", json.dumps(payload))

        diagnostic.start_request(
            query="safe query",
            conversation_id="chat-1234567890-skprojsecretvalue",
            collection_id=None,
            document_id=None,
            version_id=None,
        )
        self.assertTrue(str(diagnostic.to_dict()["conversation_id"]).startswith("sha256:"))
