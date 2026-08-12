"""Deterministic guards for context-bound chat follow-ups."""

from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

from app.prompts.rag_prompt import UNAVAILABLE_ANSWER
from app.services import chat_context, rag_service


class FollowUpDetectionTests(TestCase):
    """Keep reference phrases distinct from independent questions with pronouns."""

    def test_short_reference_requests_are_follow_ups(self) -> None:
        """Common commands stay context-bound without an LLM classifier."""
        for question in ("name them", "what about those?", "and the others?", "show details", "who reviewd this?"):
            with self.subTest(question=question):
                self.assertTrue(chat_context.is_follow_up_question(question))

    def test_metric_only_questions_are_context_candidates_not_plain_references(self) -> None:
        """Elliptical metric wording needs compatible context before it is resolved."""
        self.assertFalse(chat_context.is_follow_up_question("what is the current rate?"))
        self.assertTrue(chat_context.is_elliptical_follow_up_candidate("what is the current rate?"))
        self.assertTrue(chat_context.is_elliptical_follow_up_candidate("current rate?"))
        self.assertFalse(chat_context.is_elliptical_follow_up_candidate("what is sab?"))
        self.assertFalse(chat_context.is_elliptical_follow_up_candidate("current rate for Project Alpha?"))

    def test_substantive_pronoun_questions_are_independent(self) -> None:
        """A pronoun plus a new topic must continue through normal routing."""
        for question in (
            "What is it like to work remotely?",
            "Show details for the Finance policy.",
            "What about employee benefits?",
        ):
            with self.subTest(question=question):
                self.assertFalse(chat_context.is_follow_up_question(question))

    def test_named_profile_continuation_requires_matching_prior_profile(self) -> None:
        """A named 'what about' continuation remains scoped to prior profile evidence."""
        context = {
            "structured": {
                "kind": "employee_profiles",
                "all_employee_names": ["Asha Patel", "Ravi Shah"],
            }
        }

        self.assertTrue(chat_context._is_named_profile_continuation("What about Ravi Shah?", context))
        self.assertFalse(chat_context._is_named_profile_continuation("What about employee benefits?", context))

    def test_recent_context_window_rejects_stale_result(self) -> None:
        """Long-lived storage does not make an old result eligible for follow-up."""
        current = chat_context._now()
        with patch.object(chat_context, "_now", return_value=current):
            self.assertTrue(chat_context._context_is_recent(current - timedelta(minutes=29)))
            self.assertFalse(chat_context._context_is_recent(current - timedelta(minutes=31)))


class FollowUpResolutionTests(TestCase):
    """Verify missing or unusable context never falls through to global search."""

    @staticmethod
    def _context() -> dict[str, object]:
        """Build the minimum grounded structured context used by a direct reference."""
        return {
            "organization_id": "org-a",
            "document_ids": [7],
            "version_ids": [3],
            "structured": {
                "kind": "structured_rows",
                "result_type": "list",
                "display_column": "Name",
                "contributing_values": ["Asha"],
                "row_refs": [{"document_id": 7, "sheet": "People", "row_number": 2}],
                "result_plan": {
                    "document_id": 7,
                    "version_id": 3,
                    "sheets": [{"sheet_name": "People", "row_ranges": [{"row_start": 2, "row_end": 2}]}],
                },
            },
            "sources": [{
                "document_id": 7,
                "version_id": 3,
                "filename": "people.xlsx",
                "source_location": {"sheet_name": "People"},
            }],
        }

    def test_valid_reference_uses_recent_grounded_rows(self) -> None:
        """A direct request resolves only from its most recent grounded rows."""
        rows = [{
            "document_id": 7,
            "version_id": 3,
            "filename": "people.xlsx",
            "sheet": "People",
            "row_number": 2,
            "values": {"Name": "Asha"},
        }]
        sources = [{"document_id": 7, "version_id": 3, "filename": "people.xlsx"}]
        with (
            patch.object(chat_context, "_latest_context", return_value=self._context()),
            patch.object(chat_context, "_load_context_rows", return_value=(rows, sources)),
        ):
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id="conversation-1",
                question="name them",
            )

        self.assertTrue(result and result["grounded"])
        self.assertEqual(result["question_type"], "follow_up")
        self.assertEqual(result["sources"], sources)
        self.assertIn("Asha", str(result["answer"]))

    def test_reference_can_request_another_column_from_same_row(self) -> None:
        """Column-specific follow-ups answer from the prior cited row, including minor typos."""
        context = self._context()
        context["structured"]["display_column"] = "Test Case ID"
        rows = [{
            "document_id": 7,
            "version_id": 3,
            "filename": "test-cases.xlsx",
            "sheet": "TestCases",
            "row_number": 12,
            "values": {"Test Case ID": 192, "Reviewed By": "Aparna"},
        }]
        sources = [{"document_id": 7, "version_id": 3, "filename": "test-cases.xlsx"}]
        with (
            patch.object(chat_context, "_latest_context", return_value=context),
            patch.object(chat_context, "_load_context_rows", return_value=(rows, sources)),
        ):
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id="conversation-1",
                question="who reviewd this?",
            )

        self.assertTrue(result and result["grounded"])
        self.assertIn("Reviewed By from the prior grounded result", str(result["answer"]))
        self.assertIn("Aparna", str(result["answer"]))
        self.assertNotIn("Test Case ID from the prior grounded result", str(result["answer"]))

    def test_metric_only_follow_up_inherits_matching_column_context(self) -> None:
        """A compatible short metric question resolves within the prior row scope."""
        context = self._context()
        context["structured"]["display_column"] = "Project"
        rows = [{
            "document_id": 7,
            "version_id": 3,
            "filename": "projects.xlsx",
            "sheet": "Projects",
            "row_number": 2,
            "values": {"Project": "Alpha", "Current rate": "17.5 percent"},
        }]
        sources = [{"document_id": 7, "version_id": 3, "filename": "projects.xlsx"}]
        with (
            patch.object(chat_context, "_latest_context", return_value=context),
            patch.object(chat_context, "_load_context_rows", return_value=(rows, sources)),
        ):
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id="conversation-1",
                question="what is the current rate?",
            )

        self.assertTrue(result and result["grounded"])
        self.assertIn("Current rate from the prior grounded result", str(result["answer"]))
        self.assertIn("17.5 percent", str(result["answer"]))

    def test_metric_only_without_matching_context_is_unavailable(self) -> None:
        """Unresolved elliptical questions cannot fall through to global retrieval."""
        context = self._context()
        rows = [{
            "document_id": 7,
            "version_id": 3,
            "filename": "projects.xlsx",
            "sheet": "Projects",
            "row_number": 2,
            "values": {"Project": "Alpha", "Owner": "Finance"},
        }]
        with (
            patch.object(chat_context, "_latest_context", return_value=context),
            patch.object(chat_context, "_load_context_rows", return_value=(rows, [])),
        ):
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id="conversation-1",
                question="current rate?",
            )

        self.assertEqual(result and result["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(result and result["sources"], [])

    def test_missing_conversation_returns_unavailable(self) -> None:
        """Reference language without a conversation cannot start global retrieval."""
        with patch.object(chat_context, "_latest_context") as latest:
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id=None,
                question="show details",
            )

        latest.assert_not_called()
        self.assertEqual(result and result["answer"], UNAVAILABLE_ANSWER)
        self.assertFalse(bool(result and result["grounded"]))

    def test_missing_conversation_for_metric_only_returns_unavailable(self) -> None:
        """Metric-only continuations need conversation context and never search globally."""
        with patch.object(chat_context, "_latest_context") as latest:
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id=None,
                question="current rate?",
            )

        latest.assert_not_called()
        self.assertEqual(result and result["answer"], UNAVAILABLE_ANSWER)
        self.assertEqual(result and result["sources"], [])

    def test_context_without_reusable_grounded_material_is_unavailable(self) -> None:
        """A cited document alone cannot support a deterministic reference answer."""
        context = {"organization_id": "org-a", "document_ids": [7], "structured": {}}
        with (
            patch.object(chat_context, "_latest_context", return_value=context),
            patch.object(chat_context, "_load_context_rows") as load_rows,
        ):
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id="conversation-1",
                question="show details",
            )

        load_rows.assert_not_called()
        self.assertEqual(result and result["answer"], UNAVAILABLE_ANSWER)

    def test_unstructured_context_exposes_single_document_scope(self) -> None:
        """DOCX follow-ups use the prior cited document for fresh scoped retrieval."""
        context = {
            "organization_id": "org-a",
            "document_ids": [12],
            "version_ids": [34],
            "structured": {},
            "sources": [{
                "document_id": 12,
                "version_id": 34,
                "filename": "hunt-bmt.docx",
                "source_type": "word",
                "source_location": {"paragraph_start": 8, "paragraph_end": 8},
            }],
        }
        with patch.object(chat_context, "_latest_context", return_value=context):
            scope = chat_context.scoped_unstructured_follow_up_document(
                owner_id=1,
                conversation_id="conversation-1",
            )

        self.assertEqual(scope, (12, 34))

    def test_mismatched_result_plan_is_unavailable(self) -> None:
        """A retained plan outside its cited source cannot authorize a follow-up."""
        context = self._context()
        context["structured"]["result_plan"]["document_id"] = 8
        with (
            patch.object(chat_context, "_latest_context", return_value=context),
            patch.object(chat_context, "_load_context_rows") as load_rows,
        ):
            result = chat_context.resolve_follow_up(
                owner_id=1,
                conversation_id="conversation-1",
                question="name them",
            )

        load_rows.assert_not_called()
        self.assertEqual(result and result["answer"], UNAVAILABLE_ANSWER)

    def test_missing_context_does_not_run_global_routing_or_llm(self) -> None:
        """The RAG orchestration exits before retrieval for an unresolved reference."""
        with (
            patch.object(rag_service, "select_sources", side_effect=AssertionError("must not search")),
            patch.object(rag_service, "generate_answer", side_effect=AssertionError("must not call LLM")),
            patch.object(rag_service, "log_audit_event"),
        ):
            result = rag_service.answer_question("name them", user_id=1)

        self.assertFalse(result["grounded"])
        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)

    def test_missing_metric_context_does_not_run_global_routing_or_llm(self) -> None:
        """Unresolved metric-only follow-ups return unavailable before retrieval."""
        with (
            patch.object(rag_service, "select_sources", side_effect=AssertionError("must not search")),
            patch.object(rag_service, "generate_answer", side_effect=AssertionError("must not call LLM")),
            patch.object(rag_service, "log_audit_event"),
        ):
            result = rag_service.answer_question("current rate?", user_id=1)

        self.assertFalse(result["grounded"])
        self.assertEqual(result["answer"], UNAVAILABLE_ANSWER)
