"""Alembic environment: runs migrations over an async (asyncpg) connection.

The URL comes from application settings (``migration_database_url``, rag_owner role),
unless a caller passes ``sqlalchemy.url`` explicitly (used by the integration tests).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from rag_service.core.config import get_settings
from rag_service.storage.db.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().migration_database_url


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # One transaction per migration file, so a failure leaves the DB at a known revision.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    # NullPool: migrations are a one-shot process and shouldn't keep connections.
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without a DB connection."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async())
