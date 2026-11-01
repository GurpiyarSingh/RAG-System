"""Provider protocols and the value types they exchange."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# --------------------------------------------------------------------------- embeddings


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    total_tokens: int
    model: str


class EmbeddingProvider(Protocol):
    """Turns text into dense vectors. Implementations batch and rate-limit internally."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed chunk texts during ingestion (background priority)."""
        ...

    async def embed_query(self, text: str) -> EmbeddingBatch:
        """Embed a user query (interactive priority)."""
        ...


# --------------------------------------------------------------------------- generation


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    system: str
    messages: list[ChatMessage]
    model: str
    max_output_tokens: int
    temperature: float | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationDelta:
    """A piece of streamed answer text."""

    text: str


@dataclass(frozen=True, slots=True)
class GenerationDone:
    """Terminal event carrying token usage."""

    model: str
    input_tokens: int
    output_tokens: int
    finish_reason: str | None = None


GenerationEvent = GenerationDelta | GenerationDone


class LLMProvider(Protocol):
    def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        """Stream answer deltas, ending with exactly one :class:`GenerationDone`."""
        ...


# --------------------------------------------------------------------------- vector store


@dataclass(frozen=True, slots=True)
class ChunkPoint:
    """One chunk ready to be written to the index."""

    id: uuid.UUID
    dense: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    id: uuid.UUID
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    """Vector index. Searches always require the KB IDs they're scoped to."""

    async def ensure_collection(self, *, alias: str, dimension: int, model_slug: str) -> str:
        """Create the versioned collection + alias if missing; returns the alias."""
        ...

    async def upsert(self, collection: str, points: Sequence[ChunkPoint]) -> None: ...

    async def search(
        self,
        collection: str,
        *,
        kb_ids: Sequence[uuid.UUID],
        vector: list[float],
        top_k: int,
    ) -> list[ScoredChunk]: ...

    async def delete_document(
        self, collection: str, *, document_id: uuid.UUID, below_version: int | None = None
    ) -> None:
        """Delete a document's points (all of them, or only versions < ``below_version``)."""
        ...

    async def ping(self) -> None: ...
