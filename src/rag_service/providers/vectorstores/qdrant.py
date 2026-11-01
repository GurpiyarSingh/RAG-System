"""Qdrant vector store (ARCHITECTURE.md §5.2).

Collection design:
* One **versioned collection per embedding model**, e.g.
  ``chunks__openai-text-embedding-3-large-1024__v1``, reached only through the alias
  ``chunks_active``. Vectors from different models aren't comparable, so a model change
  means building a new collection and swapping the alias atomically (blue/green).
* **All KBs share the collection.** The ``kb_id`` payload index is created with
  ``is_tenant=True`` and HNSW ``m=0`` / ``payload_m=16``: Qdrant then builds a
  separate small graph per KB instead of one global graph. Every search is scoped to a
  few KBs anyway, and this keeps KB-filtered searches fast without one collection per
  KB. (``is_tenant`` is just Qdrant's name for "partition by this field".)
* Dense vectors use **int8 scalar quantization** held in RAM with full-precision
  originals on disk for rescoring: ~4x less memory at almost no recall cost.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from rag_service.core.config import Settings
from rag_service.core.logging import get_logger
from rag_service.providers.base import ChunkPoint, ScoredChunk

log = get_logger(__name__)

DENSE = "dense"  # named vector; sparse vectors join it in Phase 2
UPSERT_BATCH = 256


def model_slug(provider: str, model: str, dimension: int) -> str:
    """Filesystem/URL-safe identifier of an embedding space."""
    return re.sub(r"[^a-z0-9]+", "-", f"{provider}-{model}-{dimension}".lower()).strip("-")


def collection_name_for(slug: str, version: int = 1) -> str:
    return f"chunks__{slug}__v{version}"


class QdrantVectorStore:
    def __init__(self, settings: Settings) -> None:
        api_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        self._client = AsyncQdrantClient(url=settings.qdrant_url, api_key=api_key, timeout=30)
        self._shards = settings.qdrant_shards

    async def close(self) -> None:
        await self._client.close()

    async def ping(self) -> None:
        await self._client.get_collections()

    # ------------------------------------------------------------------ bootstrap
    async def ensure_collection(self, *, alias: str, dimension: int, model_slug: str) -> str:
        """Create the collection, its payload indexes and the alias, if not present.

        Idempotent and safe to run concurrently from several processes: losing a
        creation race just means another process already did the work.
        """
        aliases = await self._client.get_aliases()
        if any(a.alias_name == alias for a in aliases.aliases):
            return alias

        name = collection_name_for(model_slug)
        if not await self._client.collection_exists(name):
            try:
                await self._client.create_collection(
                    collection_name=name,
                    vectors_config={
                        DENSE: models.VectorParams(
                            size=dimension, distance=models.Distance.COSINE, on_disk=True
                        )
                    },
                    quantization_config=models.ScalarQuantization(
                        scalar=models.ScalarQuantizationConfig(
                            type=models.ScalarType.INT8, always_ram=True
                        )
                    ),
                    hnsw_config=models.HnswConfigDiff(m=0, payload_m=16),
                    on_disk_payload=True,
                    shard_number=self._shards,
                )
                log.info("qdrant_collection_created", collection=name)
            except UnexpectedResponse as exc:
                if exc.status_code != 409:  # 409 = created concurrently elsewhere
                    raise
            await self._create_payload_indexes(name)

        try:
            await self._client.update_collection_aliases(
                change_aliases_operations=[
                    models.CreateAliasOperation(
                        create_alias=models.CreateAlias(collection_name=name, alias_name=alias)
                    )
                ]
            )
            log.info("qdrant_alias_created", alias=alias, collection=name)
        except UnexpectedResponse as exc:
            if exc.status_code != 409:
                raise
        return alias

    async def _create_payload_indexes(self, name: str) -> None:
        # kb_id partitions the HNSW graph (see module docstring).
        await self._client.create_payload_index(
            name,
            "kb_id",
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            ),
        )
        for field in ("document_id", "acl"):
            await self._client.create_payload_index(
                name, field, field_schema=models.PayloadSchemaType.KEYWORD
            )
        await self._client.create_payload_index(
            name, "document_version", field_schema=models.PayloadSchemaType.INTEGER
        )

    # ------------------------------------------------------------------ writes
    async def upsert(self, collection: str, points: Sequence[ChunkPoint]) -> None:
        for i in range(0, len(points), UPSERT_BATCH):
            batch = points[i : i + UPSERT_BATCH]
            # wait=True: return only once the points are durable and searchable, so
            # "document ready" really means ready.
            await self._client.upsert(
                collection_name=collection,
                points=[
                    models.PointStruct(id=str(p.id), vector={DENSE: p.dense}, payload=p.payload)
                    for p in batch
                ],
                wait=True,
            )

    async def delete_document(
        self, collection: str, *, document_id: uuid.UUID, below_version: int | None = None
    ) -> None:
        must: list[models.Condition] = [
            models.FieldCondition(
                key="document_id", match=models.MatchValue(value=str(document_id))
            )
        ]
        if below_version is not None:
            must.append(
                models.FieldCondition(key="document_version", range=models.Range(lt=below_version))
            )
        await self._client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=models.Filter(must=must)),
            wait=True,
        )

    # ------------------------------------------------------------------ reads
    async def search(
        self,
        collection: str,
        *,
        kb_ids: Sequence[uuid.UUID],
        vector: list[float],
        top_k: int,
    ) -> list[ScoredChunk]:
        # The KB filter isn't optional: an empty list would mean "search everything",
        # which is never what a caller should get by accident.
        if not kb_ids:
            raise ValueError("search() requires at least one kb_id")
        result = await self._client.query_points(
            collection_name=collection,
            query=vector,
            using=DENSE,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="kb_id", match=models.MatchAny(any=[str(k) for k in kb_ids])
                    )
                ]
            ),
            limit=top_k,
            with_payload=True,
            # Re-score the quantized candidates with the full-precision vectors.
            search_params=models.SearchParams(
                quantization=models.QuantizationSearchParams(rescore=True)
            ),
        )
        return [
            ScoredChunk(id=uuid.UUID(str(p.id)), score=p.score, payload=p.payload or {})
            for p in result.points
        ]
