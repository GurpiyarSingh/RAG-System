"""Process-wide resources (connection pools, clients, providers).

Each process (API, query API, worker) builds exactly one :class:`Resources` at
startup and closes it on shutdown. Pools are expensive to create and are meant to be
shared, so building them per request would exhaust PostgreSQL connections.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rag_service.core.config import OPENAI_KEY_PLACEHOLDER, Settings
from rag_service.core.tokens import Tokenizer, get_tokenizer
from rag_service.providers.embeddings.openai import OpenAIEmbeddingProvider
from rag_service.providers.llm.openai import OpenAILLMProvider
from rag_service.providers.ratelimit import RateLimiter
from rag_service.providers.vectorstores.qdrant import QdrantVectorStore, model_slug
from rag_service.storage.cache import create_redis
from rag_service.storage.db.session import create_engine, create_sessionmaker
from rag_service.storage.object_store import ObjectStore
from rag_service.temporal_client import TemporalGateway


@dataclass
class Resources:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    object_store: ObjectStore
    vector_store: QdrantVectorStore
    embedder: OpenAIEmbeddingProvider
    llm: OpenAILLMProvider
    tokenizer: Tokenizer
    temporal: TemporalGateway

    @property
    def embedding_slug(self) -> str:
        """Identifier of the active embedding space (part of the collection name)."""
        return model_slug("openai", self.embedder.model_id, self.embedder.dimension)

    async def close(self) -> None:
        await self.vector_store.close()
        await self.redis.aclose()
        await self.engine.dispose()


def build_resources(settings: Settings, *, app_name: str) -> Resources:
    """Create every shared client. Nothing connects yet; connections open lazily."""
    engine = create_engine(settings, app_name=app_name)
    redis = create_redis(settings.redis_url)
    tokenizer = get_tokenizer()
    openai_client = AsyncOpenAI(
        # The SDK refuses an empty key at construction; the placeholder keeps startup
        # working, and providers reject calls until a real key is configured.
        api_key=settings.openai_api_key.get_secret_value() or OPENAI_KEY_PLACEHOLDER,
        base_url=settings.openai_base_url,
        timeout=settings.openai_timeout_s,
        # Retries are handled by rag_service.core.resilience, so they're logged and
        # coordinated with the rate limiter instead of hidden inside the SDK.
        max_retries=0,
    )
    limiter = RateLimiter(
        redis,
        name="openai",
        rpm=settings.openai_rpm_limit,
        tpm=settings.openai_tpm_limit,
        reserve_ratio=settings.openai_query_reserve_ratio,
    )
    return Resources(
        settings=settings,
        engine=engine,
        sessions=create_sessionmaker(engine),
        redis=redis,
        object_store=ObjectStore(settings),
        vector_store=QdrantVectorStore(settings),
        embedder=OpenAIEmbeddingProvider(
            settings, client=openai_client, limiter=limiter, tokenizer=tokenizer
        ),
        llm=OpenAILLMProvider(settings, client=openai_client, limiter=limiter, tokenizer=tokenizer),
        tokenizer=tokenizer,
        temporal=TemporalGateway(settings),
    )
