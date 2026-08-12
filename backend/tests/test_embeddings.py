"""Embedding model loading must not contact the network when a cache exists."""

from pathlib import Path
from threading import Lock, Thread
from time import sleep
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

from app.services import embeddings


class EmbeddingModelCacheTests(TestCase):
    def setUp(self) -> None:
        """Start each cache assertion without model state from another test module."""
        embeddings.reset_model_for_tests()

    def tearDown(self) -> None:
        embeddings.reset_model_for_tests()

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

    def test_concurrent_loaders_reuse_one_model_instance(self) -> None:
        """Concurrent searches must not create duplicate heavyweight models."""
        calls = 0
        calls_lock = Lock()
        fake_module = ModuleType("sentence_transformers")

        def factory(*_args: object, **_kwargs: object) -> object:
            nonlocal calls
            with calls_lock:
                calls += 1
            sleep(0.03)
            return object()

        fake_module.SentenceTransformer = factory
        loaded: list[object] = []

        def load() -> None:
            loaded.append(embeddings.get_model())

        with (
            patch.dict("sys.modules", {"sentence_transformers": fake_module}),
            patch.object(embeddings, "_cached_model_path", return_value=None),
        ):
            threads = [Thread(target=load) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(calls, 1)
        self.assertEqual(len(loaded), 6)
        self.assertEqual(len({id(instance) for instance in loaded}), 1)
        self.assertEqual(embeddings.embedding_health()["status"], "ready")

    def test_waiting_loader_has_a_bounded_timeout(self) -> None:
        """A second request fails clearly instead of waiting forever for a loader."""
        embeddings._model_lock.acquire()
        try:
            with patch.object(embeddings.settings, "embedding_model_load_timeout_seconds", 1):
                with self.assertRaisesRegex(RuntimeError, "still in progress"):
                    embeddings.get_model()
        finally:
            embeddings._model_lock.release()

    def test_failed_load_reports_only_safe_error_metadata(self) -> None:
        """Health records failure type without retaining model or exception text."""
        fake_module = ModuleType("sentence_transformers")

        def failing_factory(*_args: object, **_kwargs: object) -> object:
            raise OSError("private local path must not be returned")

        fake_module.SentenceTransformer = failing_factory
        with (
            patch.dict("sys.modules", {"sentence_transformers": fake_module}),
            patch.object(embeddings, "_cached_model_path", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be initialized"):
                embeddings.get_model()

        self.assertEqual(
            embeddings.embedding_health(),
            {"status": "failed", "loaded": False, "error_type": "OSError"},
        )
