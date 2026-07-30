"""Embedding model loading must not contact the network when a cache exists."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.services import embeddings


class EmbeddingModelCacheTests(TestCase):
    def tearDown(self) -> None:
        embeddings.model = None

    def test_resolves_complete_cached_snapshot(self) -> None:
        home = Path("mock-home")
        snapshot = (
            home / ".cache" / "huggingface" / "hub"
            / "models--sentence-transformers--all-MiniLM-L6-v2"
            / "snapshots" / "revision-1"
        )
        with (
            patch.object(Path, "home", return_value=home),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value="revision-1\n"),
        ):
            self.assertEqual(embeddings._cached_model_path(), snapshot)

    def test_ignores_incomplete_cached_snapshot(self) -> None:
        home = Path("mock-home")
        revision_file = (
            home / ".cache" / "huggingface" / "hub"
            / "models--sentence-transformers--all-MiniLM-L6-v2"
            / "refs" / "main"
        )

        def is_file(path: Path) -> bool:
            return path == revision_file

        with (
            patch.object(Path, "home", return_value=home),
            patch.object(Path, "is_file", is_file),
            patch.object(Path, "read_text", return_value="revision-1"),
        ):
            self.assertIsNone(embeddings._cached_model_path())
