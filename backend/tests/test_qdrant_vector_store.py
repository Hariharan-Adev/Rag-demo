"""Qdrant payload-filter and deterministic point behavior."""

import unittest
from unittest.mock import patch

from app.services.vector_store import QdrantVectorStore, VectorPoint


class QdrantVectorStoreTests(unittest.TestCase):
    def test_tenant_current_version_document_and_delete_filters(self) -> None:
        with patch("app.services.vector_store.settings.qdrant_url", ""), patch(
            "app.services.vector_store.settings.qdrant_local_path", ""
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
        self.assertTrue(store.contains_points([point.point_id for point in points]))
        results = store.search(
            vector,
            organization_id="org-a",
            user_id=1,
            current_version_ids=[100],
            document_id=10,
            limit=10,
        )
        self.assertEqual([result["text"] for result in results], ["current"])
        self.assertEqual(results[0]["content_id"], 100)
        self.assertEqual(store.search(
            [-1.0] + [0.0] * 383,
            organization_id="org-a",
            user_id=1,
            current_version_ids=[100],
            limit=10,
            score_threshold=0.35,
        ), [])
        store.set_document_deleted("org-a", 10, True)
        self.assertEqual(store.search(
            vector,
            organization_id="org-a",
            user_id=1,
            current_version_ids=[100],
            limit=10,
        ), [])
        store.set_document_deleted("org-a", 10, False)
        self.assertEqual(len(store.search(
            vector,
            organization_id="org-a",
            user_id=1,
            current_version_ids=[100],
            limit=10,
        )), 1)
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
        self.assertEqual(
            {result["text"] for result in acl_results},
            {"current", "organization visible"},
        )
        store.set_version_deleted("org-a", 10, 100, True)
        self.assertEqual(store.search(
            vector,
            organization_id="org-a",
            user_id=1,
            current_version_ids=[100],
            limit=10,
        ), [])
        store.set_version_deleted("org-a", 10, 100, False)
        self.assertEqual(len(store.search(
            vector,
            organization_id="org-a",
            user_id=1,
            current_version_ids=[100],
            limit=10,
        )), 1)


if __name__ == "__main__":
    unittest.main()
