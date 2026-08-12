"""Executable baseline for known project RAG failure patterns."""

from __future__ import annotations

import re
import unittest

from tests.rag_eval.regression_helpers import (
    RagRegressionCase,
    RagRegressionHarness,
    assert_regression_answer,
    assert_regression_retrieval,
    load_regression_cases,
)


CASES = load_regression_cases()


class _RagRegressionTestCase(unittest.TestCase):
    """Provide a fresh isolated application fixture for each generated test."""

    def setUp(self) -> None:
        """Start an isolated database and local upload directory."""
        self.harness = RagRegressionHarness()
        self.harness.start()

    def tearDown(self) -> None:
        """Close all test-only patches and temporary resources."""
        self.harness.close()


class KnownFailureRetrievalBaselineTests(_RagRegressionTestCase):
    """Run only route, selected-source, and provenance expectations."""


class KnownFailureAnswerBaselineTests(_RagRegressionTestCase):
    """Run only grounded/unavailable and final fact expectations."""


def _retrieval_test(case: RagRegressionCase):
    """Create one independently reported retrieval regression test."""
    def test(self: KnownFailureRetrievalBaselineTests) -> None:
        self.harness.prepare(case.fixture)
        assert_regression_retrieval(self, case, self.harness.observe_retrieval(case))

    return test


def _answer_test(case: RagRegressionCase):
    """Create one independently reported final-answer regression test."""
    def test(self: KnownFailureAnswerBaselineTests) -> None:
        self.harness.prepare(case.fixture)
        assert_regression_answer(self, case, self.harness.observe_answer(case))

    return test


def _safe_test_name(case_id: str) -> str:
    """Convert stable case IDs into unittest-compatible method names."""
    return re.sub(r"[^a-z0-9_]+", "_", case_id.casefold())


for regression_case in CASES:
    safe_name = _safe_test_name(regression_case.id)
    setattr(
        KnownFailureRetrievalBaselineTests,
        f"test_retrieval__{safe_name}",
        _retrieval_test(regression_case),
    )
    setattr(
        KnownFailureAnswerBaselineTests,
        f"test_answer__{safe_name}",
        _answer_test(regression_case),
    )


if __name__ == "__main__":
    unittest.main()
