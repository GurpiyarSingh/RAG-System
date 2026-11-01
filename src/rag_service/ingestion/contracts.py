"""Inputs/outputs shared by Temporal workflows and activities.

Kept in a dependency-free module on purpose: Temporal runs workflow code in a
deterministic sandbox that re-imports workflow modules, so workflows must not import
heavy or side-effecting packages (DB drivers, HTTP clients). They import only these
dataclasses and refer to activities **by name**.

IDs are plain strings so payloads serialize as simple JSON.
"""

from __future__ import annotations

from dataclasses import dataclass

# Task queues. Constants rather than settings: workflow code must be deterministic,
# so where an activity runs can't depend on configuration read at replay time.
# Workflows and the index activities run on INDEX_QUEUE (rag-service image); parsing
# runs on PARSE_QUEUE, served only by the rag-parser image with the Docling stack.
INDEX_QUEUE = "index"
PARSE_QUEUE = "parse"

# Activity names (the single source of truth for both sides).
ACT_MARK_STAGE = "mark_stage"
ACT_PARSE = "parse_document"
ACT_CHUNK = "chunk_document"
ACT_EMBED_INDEX = "embed_and_index"
ACT_CLEANUP_VERSIONS = "cleanup_old_versions"
ACT_FINALIZE = "finalize_ingestion"
ACT_MARK_FAILED = "mark_ingestion_failed"
ACT_DELETE_DATA = "delete_document_data"

# Error types raised as non-retryable ApplicationErrors by activities.
ERR_SUPERSEDED = "Superseded"  # a newer version of the document was uploaded
ERR_DELETED = "DocumentDeleted"  # the document was deleted while being ingested
ERR_EMPTY = "EmptyDocument"  # parsing produced no text
ERR_MODEL_NOT_CONFIGURED = "ModelNotConfigured"  # OpenAI key placeholder


def ingest_workflow_id(document_id: str, version: int) -> str:
    # Deterministic: starting the same ingestion twice is rejected by Temporal
    # instead of running it twice.
    return f"ingest-{document_id}-v{version}"


def delete_workflow_id(document_id: str) -> str:
    return f"delete-{document_id}"


@dataclass
class IngestInput:
    document_id: str
    version: int


@dataclass
class StageInput:
    document_id: str
    version: int
    workflow_id: str
    status: str  # document status to set (parsing | indexing)


@dataclass
class ParseOutput:
    parsed_key: str
    page_count: int | None


@dataclass
class ChunkInput:
    document_id: str
    version: int
    parsed_key: str


@dataclass
class ChunkOutput:
    chunk_count: int


@dataclass
class FinalizeInput:
    document_id: str
    version: int
    workflow_id: str
    chunk_count: int
    page_count: int | None


@dataclass
class FailInput:
    document_id: str
    version: int
    workflow_id: str
    error_type: str
    message: str


@dataclass
class DeleteInput:
    document_id: str
    workflow_id: str
