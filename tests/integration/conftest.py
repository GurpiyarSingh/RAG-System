"""Fixtures backed by real PostgreSQL 18 and Qdrant.

* PostgreSQL: ``RAG_TEST_DATABASE_URL`` if set (e.g. the ``rag_test`` database on the
  local server), otherwise a throwaway ``postgres:18.6`` container. That's the same
  version as the local install, so tests and production agree on behavior such as
  ``uuidv7()``.
* Qdrant: a throwaway container with the same version as compose.yaml.

The whole module is skipped when Docker isn't available and no test DB is configured.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rag_service.core.config import Settings
from rag_service.providers.vectorstores.qdrant import QdrantVectorStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QDRANT_IMAGE = "qdrant/qdrant:v1.19.2"
POSTGRES_IMAGE = "postgres:18.6"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    url = os.environ.get("RAG_TEST_DATABASE_URL")
    if url:
        yield url
        return
    if not _docker_available():
        pytest.skip("Docker not available and RAG_TEST_DATABASE_URL not set")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_url(pg_url: str) -> str:
    """Apply all migrations once per session (downgrade first for a clean slate)."""
    config = Config(os.path.join(ROOT, "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url)
    config.attributes["configure_logger"] = False
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return pg_url


@pytest.fixture
async def sessions(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    # Each test starts from empty tables (schema stays).
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE api_clients, knowledge_bases, documents, chunks, ingestion_jobs,"
                " query_logs, usage_events CASCADE"
            )
        )
    await engine.dispose()


@pytest.fixture(scope="session")
def qdrant_url() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("Docker not available")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = (
        DockerContainer(QDRANT_IMAGE)
        .with_exposed_ports(6333)
        .waiting_for(LogMessageWaitStrategy("Qdrant HTTP listening"))
    )
    with container:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(6333)}"


@pytest.fixture
async def vector_store(qdrant_url: str, settings: Settings) -> AsyncIterator[QdrantVectorStore]:
    settings.qdrant_url = qdrant_url
    store = QdrantVectorStore(settings)
    yield store
    await store.close()
