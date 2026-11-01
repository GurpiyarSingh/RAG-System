"""Vector store behavior against a real Qdrant."""

from __future__ import annotations

import uuid

import pytest

from rag_service.providers.base import ChunkPoint
from rag_service.providers.vectorstores.qdrant import QdrantVectorStore

pytestmark = pytest.mark.integration

DIM = 8


def _point(kb_id: uuid.UUID, doc_id: uuid.UUID, version: int, vector: list[float]) -> ChunkPoint:
    return ChunkPoint(
        id=uuid.uuid4(),
        dense=vector,
        payload={
            "kb_id": str(kb_id),
            "document_id": str(doc_id),
            "document_version": version,
            "text": "t",
        },
    )


async def test_search_is_scoped_to_kbs_and_versions_are_cleaned(
    vector_store: QdrantVectorStore,
) -> None:
    alias = f"test_{uuid.uuid4().hex[:8]}"
    await vector_store.ensure_collection(alias=alias, dimension=DIM, model_slug=alias)
    # Second call is a no-op (idempotent bootstrap).
    await vector_store.ensure_collection(alias=alias, dimension=DIM, model_slug=alias)

    kb_a, kb_b, doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    vec = [1.0] + [0.0] * (DIM - 1)
    await vector_store.upsert(
        alias,
        [
            _point(kb_a, doc, 1, vec),
            _point(kb_a, doc, 2, vec),
            _point(kb_b, uuid.uuid4(), 1, vec),
        ],
    )

    hits = await vector_store.search(alias, kb_ids=[kb_a], vector=vec, top_k=10)
    assert len(hits) == 2
    assert {h.payload["kb_id"] for h in hits} == {str(kb_a)}

    # Removing versions below 2 keeps only the new version.
    await vector_store.delete_document(alias, document_id=doc, below_version=2)
    hits = await vector_store.search(alias, kb_ids=[kb_a], vector=vec, top_k=10)
    assert [h.payload["document_version"] for h in hits] == [2]

    with pytest.raises(ValueError, match="kb_id"):
        await vector_store.search(alias, kb_ids=[], vector=vec, top_k=1)
