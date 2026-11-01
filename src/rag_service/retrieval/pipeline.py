"""Phase 1 retrieval: dense vector search across one or more KBs.

Phase 2 extends this same pipeline with BM25 sparse vectors + RRF fusion, a
cross-encoder reranker and query rewriting (ARCHITECTURE.md §8). The interface stays
the same, so callers won't change.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rag_service.providers.base import EmbeddingProvider, ScoredChunk, VectorStore
from rag_service.storage.db.models import KnowledgeBase


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    score: float
    kb_id: uuid.UUID
    document_id: uuid.UUID
    title: str | None
    text: str
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    source_uri: str | None
    external_id: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_scored(cls, hit: ScoredChunk) -> RetrievedChunk:
        p = hit.payload
        return cls(
            chunk_id=hit.id,
            score=hit.score,
            kb_id=uuid.UUID(p["kb_id"]),
            document_id=uuid.UUID(p["document_id"]),
            title=p.get("title"),
            text=p.get("text", ""),
            heading_path=list(p.get("heading_path") or []),
            page_start=p.get("page_start"),
            page_end=p.get("page_end"),
            source_uri=p.get("source_uri"),
            external_id=p.get("external_id"),
            metadata=p.get("meta") or {},
        )


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    embedding_tokens: int
    timings_ms: dict[str, float] = field(default_factory=dict)


class Retriever:
    def __init__(self, embedder: EmbeddingProvider, vector_store: VectorStore) -> None:
        self._embedder = embedder
        self._store = vector_store

    async def retrieve(
        self, kbs: list[KnowledgeBase], query: str, *, top_k: int
    ) -> RetrievalResult:
        """Top-``top_k`` chunks for ``query`` across ``kbs`` (already access-checked)."""
        t0 = time.perf_counter()
        embedding = await self._embedder.embed_query(query)
        t1 = time.perf_counter()

        # KBs normally share one collection; a KB moved to a dedicated collection
        # gets its own search. Searches run concurrently and results merge by score,
        # which is comparable because every collection uses the same embedding model.
        by_collection: dict[str, list[uuid.UUID]] = defaultdict(list)
        for kb in kbs:
            by_collection[kb.collection_name].append(kb.id)
        results = await asyncio.gather(
            *(
                self._store.search(
                    collection, kb_ids=kb_ids, vector=embedding.vectors[0], top_k=top_k
                )
                for collection, kb_ids in by_collection.items()
            )
        )
        hits = sorted((h for r in results for h in r), key=lambda h: h.score, reverse=True)
        t2 = time.perf_counter()

        return RetrievalResult(
            chunks=[RetrievedChunk.from_scored(h) for h in hits[:top_k]],
            embedding_tokens=embedding.total_tokens,
            timings_ms={"embed": (t1 - t0) * 1000, "search": (t2 - t1) * 1000},
        )
