"""Public request/response models — the API contract (published as OpenAPI).

These are deliberately separate from the ORM models: the database can be refactored
without breaking API consumers, and internal fields (hashes, storage keys) can't
leak into responses by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_service.core.access import ALL_SCOPES, DEFAULT_APP_SCOPES

# Metadata values are restricted to JSON scalars and string lists, so they can become
# Qdrant payload filters later (Phase 2) without type surprises.
MetadataValue = str | int | float | bool | list[str]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- API clients
class ApiClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Usually the consuming app's name")
    scopes: list[str] = Field(default_factory=lambda: sorted(DEFAULT_APP_SCOPES))
    allowed_kb_ids: list[uuid.UUID] | None = Field(
        default=None, description="Restrict the client to these KBs (null = all KBs)"
    )
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, scopes: list[str]) -> list[str]:
        unknown = set(scopes) - ALL_SCOPES
        if unknown:
            raise ValueError(f"unknown scopes: {sorted(unknown)}")
        return sorted(set(scopes))


class ApiClientOut(_Out):
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    allowed_kb_ids: list[uuid.UUID] | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiClientCreated(ApiClientOut):
    api_key: str = Field(description="Shown only once. Store it securely.")


# --------------------------------------------------------------------------- knowledge bases
class ChunkingConfigIn(BaseModel):
    max_tokens: int = Field(default=512, ge=64, le=2048)
    overlap_tokens: int = Field(default=64, ge=0, le=512)


class RetrievalConfigIn(BaseModel):
    top_k: int = Field(default=8, ge=1, le=50)


class GenerationConfigIn(BaseModel):
    model: str | None = Field(default=None, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=16, le=8192)


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    chunking: ChunkingConfigIn = Field(default_factory=ChunkingConfigIn)
    retrieval: RetrievalConfigIn = Field(default_factory=RetrievalConfigIn)
    generation: GenerationConfigIn = Field(default_factory=GenerationConfigIn)


class KnowledgeBaseOut(_Out):
    id: uuid.UUID
    name: str
    description: str | None
    embedding_config: dict[str, Any]
    chunking_config: dict[str, Any]
    retrieval_config: dict[str, Any]
    generation_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- documents
class TextDocumentCreate(BaseModel):
    text: str = Field(min_length=1, description="Plain text or Markdown content")
    format: Literal["text", "markdown"] = "text"
    title: str | None = Field(default=None, max_length=1000)
    external_id: str | None = Field(default=None, max_length=512)
    source_uri: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)
    acl: dict[str, list[str]] = Field(default_factory=dict)


class DocumentOut(_Out):
    id: uuid.UUID
    kb_id: uuid.UUID
    external_id: str
    title: str | None
    source_type: str
    source_uri: str | None
    mime_type: str
    version: int
    status: str
    status_detail: str | None
    error: dict[str, Any] | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    size_bytes: int
    page_count: int | None
    chunk_count: int | None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None


class DocumentAccepted(BaseModel):
    document: DocumentOut
    job_id: uuid.UUID | None
    unchanged: bool = Field(
        description="True if identical content was already ingested; nothing was re-processed"
    )


class DocumentList(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class JobOut(_Out):
    id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int
    kind: str
    status: str
    stage: str | None
    attempts: int
    error: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# --------------------------------------------------------------------------- retrieve / query
class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class RetrieveRequest(BaseModel):
    kb_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)


class QueryRequest(RetrieveRequest):
    history: list[HistoryMessage] = Field(default_factory=list, max_length=50)
    stream: bool = False
    model: str | None = Field(default=None, max_length=100)
    max_output_tokens: int | None = Field(default=None, ge=16, le=8192)


class ChunkOut(BaseModel):
    chunk_id: uuid.UUID
    score: float
    kb_id: uuid.UUID
    document_id: uuid.UUID
    external_id: str | None
    title: str | None
    text: str
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    source_uri: str | None
    metadata: dict[str, Any]


class RetrieveResponse(BaseModel):
    query_id: uuid.UUID
    chunks: list[ChunkOut]
    latency_ms: dict[str, float]


class Citation(BaseModel):
    id: int = Field(description="The number used as [n] in the answer")
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    title: str | None
    page_start: int | None
    page_end: int | None
    source_uri: str | None
    score: float
    snippet: str


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class QueryResponse(BaseModel):
    query_id: uuid.UUID
    answer: str
    citations: list[Citation] = Field(description="Sources actually cited in the answer")
    sources: list[Citation] = Field(description="All sources given to the model")
    invalid_citations: list[int]
    model: str | None
    usage: Usage
    finish_reason: str | None
    latency_ms: dict[str, float]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    checks: dict[str, str] = Field(default_factory=dict)
    version: str
