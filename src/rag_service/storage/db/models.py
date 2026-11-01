"""SQLAlchemy ORM models — the system of record (ARCHITECTURE.md §6.1).

PostgreSQL holds everything needed to rebuild the vector index: documents, their
chunk text and metadata. Qdrant is a derived index (ARCHITECTURE.md §3).

Conventions:
* Primary keys are UUIDv7 with a server default of ``uuidv7()``, built into
  PostgreSQL 18. The app also generates them itself (``uuid.uuid7()``) when it needs
  the ID first.
* The service is single-organization. Access separation between consuming
  applications comes from API-client scopes and KB allow-lists, not from tenant
  columns.
* Status columns are plain text guarded by CHECK constraints instead of PG enums,
  because adding a value to a PG enum needs a migration that can't run inside a
  transaction.

The schema itself is created by Alembic (``migrations/``). These models must stay in sync
with it, and an integration test checks that they do.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)

# Aliased: the Chunk model has a column named ``text``, which would shadow it.
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base with PostgreSQL-specific type mapping."""

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
        uuid.UUID: UUID(as_uuid=True),
    }


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=sql_text("uuidv7()"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def _in(column: str, values: tuple[str, ...]) -> str:
    """Render a CHECK constraint body like ``status IN ('a','b')``."""
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


# Document lifecycle (ARCHITECTURE.md §7). "deleting" documents are already invisible
# to search; "deleted" rows are kept as tombstones for audit and idempotent replays.
DOCUMENT_STATUSES = ("pending", "parsing", "indexing", "ready", "failed", "deleting", "deleted")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class ApiClient(Base):
    """An API credential for one consuming application."""

    __tablename__ = "api_clients"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[list[str]] = mapped_column(server_default=sql_text("'{}'::text[]"))
    # NULL = the client may use every KB; otherwise only the listed ones.
    allowed_kb_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class KnowledgeBase(Base):
    """A named corpus with its own chunking/retrieval/generation config."""

    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # Qdrant alias/collection holding this KB's vectors. Usually the shared alias;
    # very large KBs can be moved to a dedicated collection (ARCHITECTURE.md §5.2).
    collection_name: Mapped[str] = mapped_column(String(200))
    embedding_config: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    chunking_config: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    generation_config: Mapped[dict[str, Any]] = mapped_column(
        server_default=sql_text("'{}'::jsonb")
    )
    metadata_schema: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # api client
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    # The consumer's own identifier. Re-sending the same external_id updates the
    # document instead of creating a duplicate.
    external_id: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(50))  # upload | text | url | connector:*
    source_uri: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(20))
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of original bytes
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    status_detail: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Document-level permissions for end users (enforced in Phase 3).
    acl: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=sql_text("'{}'::jsonb")
    )
    storage_prefix: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # api client
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint(_in("status", DOCUMENT_STATUSES), name="status_valid"),)


class Chunk(Base):
    """One retrievable unit of a document version. ``id`` equals the Qdrant point ID."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    text: Mapped[str] = mapped_column(Text)
    # The exact text that was embedded: chunk text plus its contextual header.
    embed_text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    heading_path: Mapped[list[str]] = mapped_column(server_default=sql_text("'{}'::text[]"))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    chunk_type: Mapped[str] = mapped_column(String(20), server_default="text")
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created_at()


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = _pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    document_version: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # ingest | delete | reindex
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    stage: Mapped[str | None] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint(_in("status", JOB_STATUSES), name="status_valid"),)


class QueryLog(Base):
    """One row per /retrieve or /query call. Range-partitioned by month."""

    __tablename__ = "query_logs"

    # The partition key must be part of the primary key in PostgreSQL.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    kb_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(String(10))  # retrieve | query
    query: Mapped[str] = mapped_column(Text)
    retrieved: Mapped[list[Any]] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(100))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    cache_hit: Mapped[bool] = mapped_column(Boolean, server_default="false")
    status: Mapped[str] = mapped_column(String(20), server_default="ok")
    request_id: Mapped[str | None] = mapped_column(String(64))


class UsageEvent(Base):
    """Metered usage (tokens, pages, queries) per API client and KB. Partitioned."""

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kb_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # e.g. embedding_tokens | llm_input_tokens | llm_output_tokens | pages_parsed
    kind: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[int] = mapped_column(BigInteger)
    model: Mapped[str | None] = mapped_column(String(100))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # document/query id
