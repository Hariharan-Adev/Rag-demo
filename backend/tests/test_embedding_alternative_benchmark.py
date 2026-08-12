"""Contract tests for the local embedding-alternative benchmark scope."""

import unittest

from scripts.benchmark_local_embeddings import CANDIDATES, _corpus


class EmbeddingAlternativeBenchmarkTests(unittest.TestCase):
    def test_candidates_remain_compact_local_384_dimension_choices(self) -> None:
        """The benchmark must not silently expand into large or remote candidates."""
        self.assertEqual([candidate.name for candidate in CANDIDATES], [
            "sentence-transformers/all-MiniLM-L6-v2",
            "BAAI/bge-small-en-v1.5",
            "intfloat/e5-small-v2",
        ])
        self.assertTrue(all(candidate.license for candidate in CANDIDATES))

    def test_stress_corpus_has_one_target_per_query_and_no_private_data(self) -> None:
        """The local benchmark remains deterministic and synthetic."""
        passages, targets = _corpus()
        self.assertEqual((len(passages), len(targets)), (72, 8))
        self.assertEqual(len(set(targets)), 8)
        self.assertNotIn("password", " ".join(passages).casefold())


if __name__ == "__main__":
    unittest.main()
