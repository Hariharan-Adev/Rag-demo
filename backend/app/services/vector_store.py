"""Provider abstraction and Qdrant implementation for tenant-filtered retrieval."""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.config import settings


def make_vector_point_id(
    organization_id: str,
    version_id: int,
    chunk_index: int,
    embedding_model: str,
) -> str:
    value = (
        f"{organization_id}:{version_id}:{chunk_index}:{embedding_model}"
    )
    return str(uuid5(NAMESPACE_URL, value))


@dataclass(frozen=True)
class VectorPoint:
    organization_id: str
    owner_id: int
    document_id: int
    version_id: int
    content_id: int
    chunk_id: int
    chunk_index: int
    vector: list[float]
    text: str
    filename: str
    visibility: str
    source_type: str
    source_location: dict[str, object]
    deleted: bool = False
    embedding_model: str = "all-MiniLM-L6-v2"

    @property
    def point_id(self) -> str:
        return make_vector_point_id(
            self.organization_id,
            self.version_id,
            self.chunk_index,
            self.embedding_model,
        )


class VectorStore(ABC):
    @abstractmethod
    def upsert(self, points: list[VectorPoint]) -> None: ...

    def upsert_chunks(self, points: list[VectorPoint]) -> None:
        self.upsert(points)

    def contains_points(self, point_ids: list[str]) -> bool:
        return False

    def get_vectors(self, point_ids: list[str]) -> dict[str, list[float]]:
        """Return stored vectors by point ID when the provider supports retrieval."""
        return {}

    def list_active_points(
        self, organization_id: str | None = None
    ) -> dict[str, dict[str, object]]:
        """Return active point payloads for operational reconciliation."""
        return {}

    @abstractmethod
    def search(
        self,
        vector: list[float],
        *,
        organization_id: str,
        user_id: int,
        current_version_ids: list[int],
        limit: int,
        document_id: int | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, object]]: ...

    @abstractmethod
    def set_document_deleted(
        self, organization_id: str, document_id: int, deleted: bool
    ) -> None: ...

    def set_version_deleted(
        self,
        organization_id: str,
        document_id: int,
        version_id: int,
        deleted: bool,
    ) -> None:
        """Toggle one version when supported; current-version filtering remains a fallback."""

    def delete_document_version(
        self, organization_id: str, document_version_id: int
    ) -> None:
        """Physically remove one version when supported by the provider."""

    @abstractmethod
    def set_document_visibility(
        self, organization_id: str, document_id: int, visibility: str
    ) -> None: ...

    @abstractmethod
    def delete_document(self, organization_id: str, document_id: int) -> None: ...

    @abstractmethod
    def clear(self, organization_id: str | None = None) -> None: ...

    @abstractmethod
    def health(self) -> dict[str, object]: ...


class QdrantVectorStore(VectorStore):
    """Qdrant-backed vectors with tenant and ACL predicates applied server-side."""

    def __init__(self) -> None:
        from qdrant_client import QdrantClient, models

        self.models = models
        requested_mode = settings.qdrant_mode.strip().lower()
        if requested_mode not in {"auto", "local", "remote", "memory"}:
            raise RuntimeError(
                "QDRANT_MODE must be auto, local, remote, or memory."
            )
        local_path = settings.qdrant_path or settings.qdrant_local_path
        mode = requested_mode
        if mode == "auto":
            mode = "remote" if settings.qdrant_url else (
                "local" if local_path else "memory"
            )
        if settings.app_environment == "production":
            if mode != "remote" or not settings.qdrant_url.startswith("https://"):
                raise RuntimeError(
                    "Production Qdrant must use an HTTPS endpoint."
                )
            if not settings.qdrant_api_key:
                raise RuntimeError(
                    "Production Qdrant requires API-key authentication."
                )
        if mode == "remote":
            if not settings.qdrant_url:
                raise RuntimeError("QDRANT_URL is required in remote mode.")
            self.client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key or None,
                prefer_grpc=settings.qdrant_prefer_grpc,
                timeout=15,
            )
        elif mode == "local":
            if not local_path:
                raise RuntimeError("QDRANT_PATH is required in local mode.")
            self.client = QdrantClient(path=local_path)
        else:
            self.client = QdrantClient(location=":memory:")
        self.mode = mode
        self.local_path = local_path
        self.collection = settings.qdrant_collection
        collections = {
            item.name for item in self.client.get_collections().collections
        }
        if self.collection not in collections:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=settings.embedding_dimension,
                    distance=models.Distance.COSINE,
                ),
            )
        else:
            collection = self.client.get_collection(self.collection)
            vectors = collection.config.params.vectors
            actual_size = getattr(vectors, "size", None)
            if actual_size and actual_size != settings.embedding_dimension:
                raise RuntimeError(
                    "Qdrant embedding dimension does not match EMBEDDING_DIMENSION."
                )
        if self.mode == "remote":
            collection = self.client.get_collection(self.collection)
            payload_schema = collection.payload_schema or {}
            filter_indexes = {
                "organization_id": models.PayloadSchemaType.KEYWORD,
                "owner_id": models.PayloadSchemaType.INTEGER,
                "document_id": models.PayloadSchemaType.INTEGER,
                "document_version_id": models.PayloadSchemaType.INTEGER,
                "content_id": models.PayloadSchemaType.INTEGER,
                "visibility": models.PayloadSchemaType.KEYWORD,
                "is_deleted": models.PayloadSchemaType.BOOL,
            }
            for field_name, field_schema in filter_indexes.items():
                if field_name not in payload_schema:
                    self.client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field_name,
                        field_schema=field_schema,
                        wait=True,
                    )
        if self.mode == "local":
            self.client.close()
            self.client = None

    def _open_client(self):
        if self.mode != "local":
            return self.client, False
        from qdrant_client import QdrantClient

        return QdrantClient(path=self.local_path), True

    def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        if any(len(point.vector) != settings.embedding_dimension for point in points):
            raise ValueError("Embedding dimension does not match vector-store schema.")
        client, should_close = self._open_client()
        try:
            client.upsert(
                collection_name=self.collection,
                wait=True,
                points=[
                self.models.PointStruct(
                    id=point.point_id,
                    vector=point.vector,
                    payload={
                        "organization_id": point.organization_id,
                        "owner_id": point.owner_id,
                        "document_id": point.document_id,
                        "document_version_id": point.version_id,
                        "version_id": point.version_id,
                        "content_id": point.content_id,
                        "chunk_id": point.chunk_id,
                        "chunk_index": point.chunk_index,
                        "filename": point.filename,
                        "visibility": point.visibility,
                        "source_type": point.source_type,
                        "source_location": point.source_location,
                        "embedding_model": point.embedding_model,
                        "embedding_dimension": len(point.vector),
                        "is_deleted": point.deleted,
                        "deleted": point.deleted,
                    },
                )
                for point in points
                ],
            )
        finally:
            if should_close:
                client.close()

    def contains_points(self, point_ids: list[str]) -> bool:
        if not point_ids:
            return False
        client, should_close = self._open_client()
        try:
            points = client.retrieve(
                collection_name=self.collection,
                ids=point_ids,
                with_payload=False,
                with_vectors=False,
            )
            return len(points) == len(set(point_ids))
        finally:
            if should_close:
                client.close()

    def get_vectors(self, point_ids: list[str]) -> dict[str, list[float]]:
        if not point_ids:
            return {}
        client, should_close = self._open_client()
        try:
            points = client.retrieve(
                collection_name=self.collection,
                ids=point_ids,
                with_payload=False,
                with_vectors=True,
            )
            return {
                str(point.id): list(point.vector)
                for point in points
                if isinstance(point.vector, list)
            }
        finally:
            if should_close:
                client.close()

    def list_active_points(
        self, organization_id: str | None = None
    ) -> dict[str, dict[str, object]]:
        must = [
            self.models.FieldCondition(
                key="is_deleted",
                match=self.models.MatchValue(value=False),
            )
        ]
        if organization_id is not None:
            must.append(
                self.models.FieldCondition(
                    key="organization_id",
                    match=self.models.MatchValue(value=organization_id),
                )
            )
        client, should_close = self._open_client()
        try:
            points: dict[str, dict[str, object]] = {}
            offset = None
            while True:
                records, offset = client.scroll(
                    collection_name=self.collection,
                    scroll_filter=self.models.Filter(must=must),
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                points.update({
                    str(record.id): dict(record.payload or {})
                    for record in records
                })
                if offset is None:
                    break
            return points
        finally:
            if should_close:
                client.close()

    def search(
        self,
        vector: list[float],
        *,
        organization_id: str,
        user_id: int,
        current_version_ids: list[int],
        limit: int,
        document_id: int | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, object]]:
        if not current_version_ids:
            return []
        models = self.models
        must: list[Any] = [
            models.FieldCondition(
                key="organization_id", match=models.MatchValue(value=organization_id)
            ),
            models.FieldCondition(
                key="is_deleted", match=models.MatchValue(value=False)
            ),
            models.FieldCondition(
                key="document_version_id",
                match=models.MatchAny(any=current_version_ids),
            ),
        ]
        if document_id is not None:
            must.append(
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )
            )
        query_filter = models.Filter(
            must=must,
            should=[
                models.FieldCondition(
                    key="visibility",
                    match=models.MatchValue(value="organization"),
                ),
                models.FieldCondition(
                    key="owner_id",
                    match=models.MatchValue(value=user_id),
                ),
            ],
        )
        client, should_close = self._open_client()
        try:
            response = client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
                score_threshold=score_threshold,
            )
        finally:
            if should_close:
                client.close()
        return [
            {**(point.payload or {}), "score": float(point.score)}
            for point in response.points
        ]

    def set_document_deleted(
        self, organization_id: str, document_id: int, deleted: bool
    ) -> None:
        client, should_close = self._open_client()
        try:
            client.set_payload(
                collection_name=self.collection,
                payload={"deleted": deleted, "is_deleted": deleted},
                points=self.models.Filter(
                must=[
                    self.models.FieldCondition(
                        key="organization_id",
                        match=self.models.MatchValue(value=organization_id),
                    ),
                    self.models.FieldCondition(
                        key="document_id",
                        match=self.models.MatchValue(value=document_id),
                    ),
                ]
            ),
                wait=True,
            )
        finally:
            if should_close:
                client.close()

    def set_version_deleted(
        self,
        organization_id: str,
        document_id: int,
        version_id: int,
        deleted: bool,
    ) -> None:
        client, should_close = self._open_client()
        try:
            client.set_payload(
                collection_name=self.collection,
                payload={"deleted": deleted, "is_deleted": deleted},
                points=self.models.Filter(
                    must=[
                        self.models.FieldCondition(
                            key="organization_id",
                            match=self.models.MatchValue(value=organization_id),
                        ),
                        self.models.FieldCondition(
                            key="document_id",
                            match=self.models.MatchValue(value=document_id),
                        ),
                        self.models.FieldCondition(
                            key="document_version_id",
                            match=self.models.MatchValue(value=version_id),
                        ),
                    ]
                ),
                wait=True,
            )
        finally:
            if should_close:
                client.close()

    def delete_document_version(
        self, organization_id: str, document_version_id: int
    ) -> None:
        client, should_close = self._open_client()
        try:
            client.delete(
                collection_name=self.collection,
                points_selector=self.models.FilterSelector(
                    filter=self.models.Filter(
                        must=[
                            self.models.FieldCondition(
                                key="organization_id",
                                match=self.models.MatchValue(value=organization_id),
                            ),
                            self.models.FieldCondition(
                                key="document_version_id",
                                match=self.models.MatchValue(
                                    value=document_version_id
                                ),
                            ),
                        ]
                    )
                ),
                wait=True,
            )
        finally:
            if should_close:
                client.close()

    def set_document_visibility(
        self, organization_id: str, document_id: int, visibility: str
    ) -> None:
        client, should_close = self._open_client()
        try:
            client.set_payload(
            collection_name=self.collection,
            payload={"visibility": visibility},
            points=self.models.Filter(
                must=[
                    self.models.FieldCondition(
                        key="organization_id",
                        match=self.models.MatchValue(value=organization_id),
                    ),
                    self.models.FieldCondition(
                        key="document_id",
                        match=self.models.MatchValue(value=document_id),
                    ),
                ]
            ),
                wait=True,
            )
        finally:
            if should_close:
                client.close()

    def delete_document(self, organization_id: str, document_id: int) -> None:
        client, should_close = self._open_client()
        try:
            client.delete(
            collection_name=self.collection,
            points_selector=self.models.FilterSelector(
                filter=self.models.Filter(
                    must=[
                        self.models.FieldCondition(
                            key="organization_id",
                            match=self.models.MatchValue(value=organization_id),
                        ),
                        self.models.FieldCondition(
                            key="document_id",
                            match=self.models.MatchValue(value=document_id),
                        ),
                    ]
                )
            ),
                wait=True,
            )
        finally:
            if should_close:
                client.close()

    def clear(self, organization_id: str | None = None) -> None:
        conditions = []
        if organization_id is not None:
            conditions.append(
                self.models.FieldCondition(
                    key="organization_id",
                    match=self.models.MatchValue(value=organization_id),
                )
            )
        client, should_close = self._open_client()
        try:
            client.delete(
                collection_name=self.collection,
                points_selector=self.models.FilterSelector(
                    filter=self.models.Filter(must=conditions)
                ),
                wait=True,
            )
        finally:
            if should_close:
                client.close()

    def health(self) -> dict[str, object]:
        client, should_close = self._open_client()
        try:
            collection = client.get_collection(self.collection)
        finally:
            if should_close:
                client.close()
        return {
            "provider": "qdrant",
            "mode": self.mode,
            "collection": self.collection,
            "points_count": collection.points_count,
            "vector_size": getattr(collection.config.params.vectors, "size", None),
            "payload_indexes": sorted((collection.payload_schema or {}).keys()),
            "status": "ok",
        }


class SQLiteVectorStore(VectorStore):
    """Temporary rollback provider using retained SQLite embedding JSON."""

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return -1.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return -1.0
        return sum(a * b for a, b in zip(left, right)) / (
            left_norm * right_norm
        )

    @staticmethod
    def _decode_vector(value: object) -> list[float] | None:
        try:
            vector = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(vector, list)
            or len(vector) != settings.embedding_dimension
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in vector
            )
        ):
            return None
        return [float(item) for item in vector]

    def upsert(self, points: list[VectorPoint]) -> None:
        if any(len(point.vector) != settings.embedding_dimension for point in points):
            raise ValueError("Embedding dimension does not match SQLite vector schema.")
        from app.database import get_connection

        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """UPDATE chunks
                   SET embedding = ?, vector_point_id = ?,
                       embedding_model = ?, embedding_dimension = ?
                   WHERE id = ? AND organization_id = ?""",
                [
                    (
                        json.dumps(point.vector),
                        point.point_id,
                        point.embedding_model,
                        len(point.vector),
                        point.chunk_id,
                        point.organization_id,
                    )
                    for point in points
                ],
            )

    def contains_points(self, point_ids: list[str]) -> bool:
        if not point_ids:
            return False
        from app.database import get_connection

        placeholders = ",".join("?" for _ in point_ids)
        with get_connection() as connection:
            count = connection.execute(
                f"""SELECT COUNT(DISTINCT vector_point_id) FROM chunks
                    WHERE vector_point_id IN ({placeholders})
                      AND embedding IS NOT NULL""",
                tuple(point_ids),
            ).fetchone()[0]
        return int(count) == len(set(point_ids))

    def get_vectors(self, point_ids: list[str]) -> dict[str, list[float]]:
        if not point_ids:
            return {}
        from app.database import get_connection

        placeholders = ",".join("?" for _ in point_ids)
        with get_connection() as connection:
            rows = connection.execute(
                f"""SELECT vector_point_id, embedding FROM chunks
                    WHERE vector_point_id IN ({placeholders})
                      AND embedding IS NOT NULL""",
                tuple(point_ids),
            ).fetchall()
        vectors = {}
        for row in rows:
            vector = self._decode_vector(row["embedding"])
            if vector is not None:
                vectors[str(row["vector_point_id"])] = vector
        return vectors

    def list_active_points(
        self, organization_id: str | None = None
    ) -> dict[str, dict[str, object]]:
        from app.database import get_connection

        with get_connection() as connection:
            rows = connection.execute(
                """SELECT c.vector_point_id, c.id AS chunk_id,
                          c.content_id, c.document_id, c.version_id,
                          c.chunk_index, c.organization_id
                   FROM chunks c
                   JOIN documents d
                     ON d.id = c.document_id
                    AND d.organization_id = c.organization_id
                   WHERE c.embedding IS NOT NULL
                     AND c.vector_point_id IS NOT NULL
                     AND c.deleted_at IS NULL AND d.deleted_at IS NULL
                     AND d.current_version_id = c.version_id
                     AND (? IS NULL OR c.organization_id = ?)""",
                (organization_id, organization_id),
            ).fetchall()
        return {
            str(row["vector_point_id"]): {
                "chunk_id": int(row["chunk_id"]),
                "content_id": int(row["content_id"]),
                "document_id": int(row["document_id"]),
                "document_version_id": int(row["version_id"]),
                "chunk_index": int(row["chunk_index"]),
                "organization_id": str(row["organization_id"]),
            }
            for row in rows
        }

    def search(
        self,
        vector: list[float],
        *,
        organization_id: str,
        user_id: int,
        current_version_ids: list[int],
        limit: int,
        document_id: int | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, object]]:
        if not current_version_ids:
            return []
        from app.database import get_connection

        version_placeholders = ",".join("?" for _ in current_version_ids)
        with get_connection() as connection:
            rows = connection.execute(
                f"""SELECT c.id, c.content_id, c.document_id, c.version_id,
                           c.chunk_index, c.embedding
                    FROM chunks c
                    JOIN documents d
                      ON d.id = c.document_id
                     AND d.organization_id = c.organization_id
                    WHERE c.organization_id = ?
                      AND c.version_id IN ({version_placeholders})
                      AND c.deleted_at IS NULL AND d.deleted_at IS NULL
                      AND (d.owner_id = ? OR d.visibility = 'organization')
                      AND (? IS NULL OR c.document_id = ?)""",
                (
                    organization_id,
                    *current_version_ids,
                    user_id,
                    document_id,
                    document_id,
                ),
            ).fetchall()
        matches = []
        for row in rows:
            stored = self._decode_vector(row["embedding"])
            if stored is None:
                continue
            score = self._cosine(vector, stored)
            if score_threshold is not None and score < score_threshold:
                continue
            matches.append({
                "chunk_id": int(row["id"]),
                "content_id": int(row["content_id"]),
                "document_id": int(row["document_id"]),
                "document_version_id": int(row["version_id"]),
                "chunk_index": int(row["chunk_index"]),
                "score": score,
            })
        matches.sort(key=lambda item: float(item["score"]), reverse=True)
        return matches[:limit]

    def set_document_deleted(
        self, organization_id: str, document_id: int, deleted: bool
    ) -> None:
        return None

    def set_document_visibility(
        self, organization_id: str, document_id: int, visibility: str
    ) -> None:
        return None

    def delete_document(self, organization_id: str, document_id: int) -> None:
        return None

    def clear(self, organization_id: str | None = None) -> None:
        from app.database import get_connection

        with get_connection() as connection:
            if organization_id is None:
                connection.execute(
                    """UPDATE chunks SET embedding = NULL,
                       indexing_status = 'pending', qdrant_indexed_at = NULL"""
                )
            else:
                connection.execute(
                    """UPDATE chunks SET embedding = NULL,
                       indexing_status = 'pending', qdrant_indexed_at = NULL
                       WHERE organization_id = ?""",
                    (organization_id,),
                )

    def health(self) -> dict[str, object]:
        from app.database import get_connection

        with get_connection() as connection:
            count = int(connection.execute(
                """SELECT COUNT(*) FROM chunks
                   WHERE embedding IS NOT NULL AND deleted_at IS NULL"""
            ).fetchone()[0])
        return {
            "provider": "sqlite",
            "mode": "rollback",
            "collection": "chunks.embedding",
            "points_count": count,
            "status": "ok",
        }


_store: VectorStore | None = None
_store_lock = Lock()


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                provider = (
                    settings.vector_store or settings.vector_store_provider
                ).strip().lower()
                if provider == "qdrant":
                    _store = QdrantVectorStore()
                elif provider == "sqlite":
                    _store = SQLiteVectorStore()
                else:
                    raise ValueError(
                        "VECTOR_STORE must be 'qdrant' or 'sqlite'."
                    )
    return _store


def reset_vector_store_for_tests() -> None:
    global _store
    _store = None
