"""OpenAI embeddings (V1 default: ``text-embedding-3-large`` truncated to 1024 dims).

``dimensions=1024`` relies on the model's Matryoshka training: the leading
dimensions carry most of the signal, so truncating keeps nearly full retrieval quality
while vector storage stays at the size the Qdrant collection was designed for
(ARCHITECTURE.md §0.4).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from openai import AsyncOpenAI

from rag_service.core.config import Settings
from rag_service.core.errors import ModelNotConfiguredError
from rag_service.core.logging import get_logger
from rag_service.core.resilience import retrying
from rag_service.core.tokens import Tokenizer
from rag_service.providers.base import EmbeddingBatch
from rag_service.providers.ratelimit import Priority, RateLimiter

log = get_logger(__name__)

# API limits for text-embedding-3-*: 8,191 tokens per input and 2,048 inputs and
# ~300k tokens per request. We stay a little under them.
MAX_INPUT_TOKENS = 8000
MAX_REQUEST_TOKENS = 250_000


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncOpenAI,
        limiter: RateLimiter | None,
        tokenizer: Tokenizer,
    ) -> None:
        self._settings = settings
        self._client = client
        self._limiter = limiter
        self._tokenizer = tokenizer
        self._model = settings.embedding_model
        self._dimension = settings.embedding_dimensions
        self._batch_size = settings.embedding_batch_size

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        return await self._embed(texts, Priority.BACKGROUND)

    async def embed_query(self, text: str) -> EmbeddingBatch:
        return await self._embed([text], Priority.INTERACTIVE)

    async def _embed(self, texts: Sequence[str], priority: Priority) -> EmbeddingBatch:
        if not self._settings.openai_key_configured:
            raise ModelNotConfiguredError(
                "OPENAI_API_KEY is still the placeholder in .env. Add a real key and restart."
            )
        vectors: list[list[float]] = []
        total_tokens = 0
        for batch, batch_tokens in self._batches(texts):
            if self._limiter:
                await self._limiter.acquire(batch_tokens, priority)
            async for attempt in retrying():
                with attempt:
                    response = await self._client.embeddings.create(
                        model=self._model, input=batch, dimensions=self._dimension
                    )
            # The API may return items out of order; ``index`` says where each belongs.
            ordered = sorted(response.data, key=lambda d: d.index)
            vectors.extend(list(d.embedding) for d in ordered)
            total_tokens += response.usage.total_tokens
        return EmbeddingBatch(vectors=vectors, total_tokens=total_tokens, model=self._model)

    def _batches(self, texts: Sequence[str]) -> Iterator[tuple[list[str], int]]:
        """Group inputs into requests bounded by item count and total tokens."""
        batch: list[str] = []
        batch_tokens = 0
        for raw in texts:
            # Empty strings are rejected by the API; a single space embeds harmlessly.
            text = raw if raw.strip() else " "
            n = self._tokenizer.count(text)
            if n > MAX_INPUT_TOKENS:
                # Chunks are ~512 tokens, so this only guards against misconfiguration.
                log.warning("embedding_input_truncated", tokens=n)
                text = self._tokenizer.truncate(text, MAX_INPUT_TOKENS)
                n = MAX_INPUT_TOKENS
            if batch and (len(batch) >= self._batch_size or batch_tokens + n > MAX_REQUEST_TOKENS):
                yield batch, batch_tokens
                batch, batch_tokens = [], 0
            batch.append(text)
            batch_tokens += n
        if batch:
            yield batch, batch_tokens
