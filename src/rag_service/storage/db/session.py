"""Async engine and session factory.

One engine per process. FastAPI request handlers and Temporal activities each open
a short-lived :class:`AsyncSession` per unit of work. ``expire_on_commit=False`` lets
returned ORM objects be read after commit without triggering lazy loads, which would
fail in async code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rag_service.core.config import Settings


def create_engine(
    settings: Settings, *, url: str | None = None, app_name: str = "rag"
) -> AsyncEngine:
    """Create the process-wide connection pool."""
    return create_async_engine(
        url or settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Detect connections the server closed (e.g. Postgres restarted on the host).
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            # Visible in pg_stat_activity, so you can see which service holds a connection.
            "server_settings": {
                "application_name": app_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Open a session inside a transaction that commits on success, rolls back on error."""
    async with factory() as session, session.begin():
        yield session
