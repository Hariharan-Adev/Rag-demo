"""Qdrant payload-filter and deterministic point behavior."""

import tempfile
import unittest
from unittest.mock import patch

from app.services.vector_store import QdrantVectorStore, VectorPoint


class QdrantVectorStoreTests(unittest.TestCase):
    def test_tenant_current_version_document_and_delete_filters(self) -> None:
        with tempfile.TemporaryDirectory() as qdrant_path, patch(
            "app.services.vector_store.settings.qdrant_url", ""
        ), patch(
            "app.services.vector_store.settings.qdrant_mode", "local"
        ), patch(
            "app.services.vector_store.settings.qdrant_local_path", qdrant_path
        ), patch(
            "app.services.vector_store.settings.qdrant_collection",
            "test_contract",
        ):
            store = QdrantVectorStore()
            vector = [1.0] + [0.0] * 383
            points = [
                VectorPoint(
                    organization_id=organization,
                    owner_id=owner,
                    document_id=document,
                    version_id=version,
                    content_id=version,
                    chunk_id=index,
                    chunk_index=0,
                    vector=vector,
                    text=text,
                    filename=f"{document}.txt",
                    visibility="private",
                    source_type="text",
                    source_location={"line_start": 1, "line_end": 1},
                )
                for index, (organization, owner, document, version, text) in enumerate(
                    [
                        ("org-a", 1, 10, 100, "current"),
                        ("org-a", 1, 10, 101, "old version"),
                        ("org-b", 2, 20, 200, "other tenant"),
                    ],
                    start=1,
                )
            ]
            store.upsert(points)
            with self.assertRaisesRegex(ValueError, "dimension"):
                store.upsert([
                    VectorPoint(**{**points[0].__dict__, "vector": [1.0, 0.0]})
                ])
            self.assertTrue(store.contains_points([point.point_id for point in points]))
            self.assertEqual(store.delete_points([points[1].point_id]), 1)
            self.assertFalse(store.contains_points([points[1].point_id]))
            self.assertTrue(store.contains_points([points[0].point_id, points[2].point_id]))
            self.assertEqual(
                store.get_vectors([points[0].point_id]),
                {points[0].point_id: vector},
            )
            results = store.search(
                vector,
                organization_id="org-a",
                user_id=1,
                current_version_ids=[100],
                document_id=10,
                limit=10,
            )
            self.assertEqual([result["chunk_id"] for result in results], [1])
            self.assertNotIn("text", results[0])
            self.assertEqual(results[0]["content_id"], 100)
            self.assertEqual(
                store.search(
                    [-1.0] + [0.0] * 383,
                    organization_id="org-a",
                    user_id=1,
                    current_version_ids=[100],
                    limit=10,
                    score_threshold=0.35,
                ),
                [],
            )
            store.set_document_deleted("org-a", 10, True)
            self.assertEqual(
                store.search(
                    vector,
                    organization_id="org-a",
                    user_id=1,
                    current_version_ids=[100],
                    limit=10,
                ),
                [],
            )
            store.set_document_deleted("org-a", 10, False)
            self.assertEqual(
                len(store.search(
                    vector,
                    organization_id="org-a",
                    user_id=1,
                    current_version_ids=[100],
                    limit=10,
                )),
                1,
            )
            store.upsert([
                VectorPoint(
                    organization_id="org-a",
                    owner_id=99,
                    document_id=11,
                    version_id=102,
                    content_id=102,
                    chunk_id=10,
                    chunk_index=0,
                    vector=vector,
                    text="inaccessible private",
                    filename="private.txt",
                    visibility="private",
                    source_type="text",
                    source_location={"line_start": 1, "line_end": 1},
                ),
                VectorPoint(
                    organization_id="org-a",
                    owner_id=99,
                    document_id=12,
                    version_id=103,
                    content_id=103,
                    chunk_id=11,
                    chunk_index=0,
                    vector=vector,
                    text="organization visible",
                    filename="organization.txt",
                    visibility="organization",
                    source_type="text",
                    source_location={"line_start": 1, "line_end": 1},
                ),
            ])
            acl_results = store.search(
                vector,
                organization_id="org-a",
                user_id=1,
                current_version_ids=[100, 102, 103],
                limit=10,
            )
            self.assertEqual({result["document_id"] for result in acl_results}, {10, 12})
            store.set_version_deleted("org-a", 10, 100, True)
            self.assertEqual(
                store.search(
                    vector,
                    organization_id="org-a",
                    user_id=1,
                    current_version_ids=[100],
                    limit=10,
                ),
                [],
            )
            store.set_version_deleted("org-a", 10, 100, False)
            self.assertEqual(
                len(store.search(
                    vector,
                    organization_id="org-a",
                    user_id=1,
                    current_version_ids=[100],
                    limit=10,
                )),
                1,
            )


if __name__ == "__main__":
    unittest.main()
