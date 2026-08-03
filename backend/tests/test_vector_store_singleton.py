"""Vector-store singleton initialization tests."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app.services import vector_store


class VectorStoreSingletonTests(unittest.TestCase):
    def tearDown(self) -> None:
        vector_store.reset_vector_store_for_tests()

    def test_concurrent_initialization_returns_one_instance(self) -> None:
        created = object()
        calls = 0
        calls_lock = threading.Lock()

        def factory():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return created

        results: list[object] = []
        with patch.object(vector_store.settings, "vector_store", "qdrant"), patch.object(
            vector_store, "QdrantVectorStore", side_effect=factory
        ):
            threads = [
                threading.Thread(target=lambda: results.append(vector_store.get_vector_store()))
                for _ in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(calls, 1)
        self.assertEqual(results, [created] * 12)


if __name__ == "__main__":
    unittest.main()
