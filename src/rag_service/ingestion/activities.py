"""Temporal activities: every side effect of the ingestion pipeline.

Each activity is written to be **idempotent**, because Temporal may run it more than
once (retry after a timeout, worker crash after the work but before reporting):

* chunk rows of a version are replaced, not appended;
* Qdrant points have deterministic IDs, so re-upserting overwrites;
* status updates are guarded by ``expected_version`` and "not deleting".

Each activity also starts with a **guard** that re-reads the document. If a newer
version was uploaded or the document is being deleted, it stops with a
non-retryable error, so stale work never overwrites newer state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, update
from temporalio import activity
from temporalio.exceptions import ApplicationError

from rag_service.core.errors import ModelNotConfiguredError
from rag_service.core.ids import chunk_id, new_id
from rag_service.core.logging import get_logger
from rag_service.ingestion import contracts as c
from rag_service.ingestion.chunkers.recursive import ChunkingConfig, RecursiveChunker
from rag_service.ingestion.mime import extension_for
from rag_service.ingestion.parsers import parse_bytes
from rag_service.ingestion.types import ParsedDocument
from rag_service.providers.base import ChunkPoint
from rag_service.providers.pricing import cost_usd
from rag_service.resources import Resources
from rag_service.storage.db.models import Chunk, Document, IngestionJob, KnowledgeBase, UsageEvent
from rag_service.storage.db.session import transaction
from rag_service.storage.object_store import document_prefix, document_root
from rag_service.storage.repositories.documents import (
    ChunkRepository,
    DocumentRepository,
    JobRepository,
)
from rag_service.storage.repositories.logs import LogRepository

log = get_logger(__name__)

# Chunks embedded and upserted per round in embed_and_index. Bounded so memory stays
# flat for huge documents and progress is heartbeated regularly.
EMBED_GROUP = 256


def _fatal(error_type: str, message: str) -> ApplicationError:
    """A non-retryable error that ends the workflow with a typed reason."""
    return ApplicationError(message, type=error_type, non_retryable=True)


@contextlib.asynccontextmanager
async def _heartbeating(every_s: float = 20.0) -> AsyncIterator[None]:
    """Heartbeat in the background while a long, non-cooperative step runs.

    Without heartbeats Temporal can't tell a slow parse from a dead worker; with them
    a crashed worker is detected within ``heartbeat_timeout`` and the activity moves
    to another worker.
    """

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(every_s)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()


class IngestionActivities:
    """Activities bound to the process-wide resources (DB pool, clients)."""

    def __init__(self, resources: Resources) -> None:
        self.r = resources

    # ------------------------------------------------------------------ helpers
    async def _current(self, document_id: str, version: int) -> tuple[Document, KnowledgeBase]:
        """Load the document + KB and verify this work item is still the latest."""
        async with self.r.sessions() as session:
            doc = await DocumentRepository(session).get_by_id(uuid.UUID(document_id))
            if doc is None or doc.status in ("deleting", "deleted"):
                raise _fatal(c.ERR_DELETED, "The document was deleted.")
            if doc.version != version:
                raise _fatal(
                    c.ERR_SUPERSEDED, f"Version {version} was superseded by {doc.version}."
                )
            kb = await session.get(KnowledgeBase, doc.kb_id)
            if kb is None or kb.deleted_at is not None:
                raise _fatal(c.ERR_DELETED, "The knowledge base was deleted.")
            return doc, kb

    # ------------------------------------------------------------------ stages
    @activity.defn(name=c.ACT_MARK_STAGE)
    async def mark_stage(self, inp: c.StageInput) -> None:
        await self._current(inp.document_id, inp.version)
        async with transaction(self.r.sessions) as session:
            await DocumentRepository(session).set_status(
                uuid.UUID(inp.document_id),
                status=inp.status,
                expected_version=inp.version,
            )
            await session.execute(
                update(IngestionJob)
                .where(IngestionJob.workflow_id == inp.workflow_id)
                .values(
                    status="running",
                    stage=inp.status,
                    attempts=IngestionJob.attempts + 1,
                    # Keep the first start time across retries.
                    started_at=func.coalesce(IngestionJob.started_at, func.now()),
                )
            )

    @activity.defn(name=c.ACT_PARSE)
    async def parse_document(self, inp: c.IngestInput) -> c.ParseOutput:
        doc, _ = await self._current(inp.document_id, inp.version)
        ext = extension_for(doc.mime_type)
        data = await self.r.object_store.get_bytes(f"{doc.storage_prefix}/original{ext}")
        async with _heartbeating():
            # Docling picks its backend from the file extension, so pass a name with it.
            parsed = await parse_bytes(data, doc.mime_type, filename=f"document{ext}")
        if parsed.is_empty:
            raise _fatal(c.ERR_EMPTY, "No text could be extracted from the document.")
        if parsed.title is None and doc.title:
            parsed.title = doc.title
        key = f"{doc.storage_prefix}/parsed.json"
        await self.r.object_store.put_bytes(
            key, json.dumps(parsed.to_json_dict()).encode(), "application/json"
        )
        log.info("document_parsed", document_id=inp.document_id, blocks=len(parsed.blocks))
        return c.ParseOutput(parsed_key=key, page_count=parsed.page_count)

    @activity.defn(name=c.ACT_CHUNK)
    async def chunk_document(self, inp: c.ChunkInput) -> c.ChunkOutput:
        doc, kb = await self._current(inp.document_id, inp.version)
        parsed = ParsedDocument.from_json_dict(
            json.loads(await self.r.object_store.get_bytes(inp.parsed_key))
        )
        # The document's own title wins over whatever the parser guessed.
        parsed.title = doc.title or parsed.title
        settings = self.r.settings
        config = ChunkingConfig(
            max_tokens=int(kb.chunking_config.get("max_tokens", settings.chunk_max_tokens)),
            overlap_tokens=int(
                kb.chunking_config.get("overlap_tokens", settings.chunk_overlap_tokens)
            ),
        )
        drafts = RecursiveChunker(self.r.tokenizer, config).chunk(parsed)
        if not drafts:
            raise _fatal(c.ERR_EMPTY, "The document produced no chunks.")

        rows = [
            Chunk(
                id=chunk_id(doc.id, inp.version, d.ordinal),
                kb_id=doc.kb_id,
                document_id=doc.id,
                document_version=inp.version,
                ordinal=d.ordinal,
                text=d.text,
                embed_text=d.embed_text,
                token_count=d.token_count,
                content_hash=hashlib.sha256(d.embed_text.encode()).hexdigest(),
                heading_path=d.heading_path,
                page_start=d.page_start,
                page_end=d.page_end,
                chunk_type=d.chunk_type,
            )
            for d in drafts
        ]
        async with transaction(self.r.sessions) as session:
            await ChunkRepository(session).replace_version(doc.id, inp.version, rows)
        log.info("document_chunked", document_id=inp.document_id, chunks=len(rows))
        return c.ChunkOutput(chunk_count=len(rows))

    @activity.defn(name=c.ACT_EMBED_INDEX)
    async def embed_and_index(self, inp: c.IngestInput) -> None:
        doc, kb = await self._current(inp.document_id, inp.version)
        async with self.r.sessions() as session:
            chunks = await ChunkRepository(session).list_version(doc.id, inp.version)

        principals = list(doc.acl.get("principals", [])) if doc.acl else []
        total_tokens = 0
        for start in range(0, len(chunks), EMBED_GROUP):
            group = chunks[start : start + EMBED_GROUP]
            try:
                batch = await self.r.embedder.embed_documents([ch.embed_text for ch in group])
            except ModelNotConfiguredError as exc:
                raise _fatal(c.ERR_MODEL_NOT_CONFIGURED, str(exc)) from exc
            total_tokens += batch.total_tokens
            # Re-check before writing: a delete may have arrived while we embedded.
            await self._current(inp.document_id, inp.version)
            await self.r.vector_store.upsert(
                kb.collection_name,
                [
                    ChunkPoint(id=ch.id, dense=vec, payload=_payload(doc, ch, principals))
                    for ch, vec in zip(group, batch.vectors, strict=True)
                ],
            )
            activity.heartbeat(start + len(group))

        async with transaction(self.r.sessions) as session:
            await ChunkRepository(session).set_embedding_model(
                doc.id, inp.version, self.r.embedder.model_id
            )
            await LogRepository(session).add_usage(
                [
                    UsageEvent(
                        id=new_id(),
                        client_id=doc.uploaded_by,
                        kb_id=doc.kb_id,
                        kind="embedding_tokens",
                        quantity=total_tokens,
                        model=self.r.embedder.model_id,
                        cost_usd=cost_usd(self.r.embedder.model_id, total_tokens),
                        ref_id=doc.id,
                    )
                ]
            )
        log.info("document_indexed", document_id=inp.document_id, tokens=total_tokens)

    @activity.defn(name=c.ACT_CLEANUP_VERSIONS)
    async def cleanup_old_versions(self, inp: c.IngestInput) -> None:
        doc, kb = await self._current(inp.document_id, inp.version)
        if inp.version <= 1:
            return
        await self.r.vector_store.delete_document(
            kb.collection_name, document_id=doc.id, below_version=inp.version
        )
        async with transaction(self.r.sessions) as session:
            await ChunkRepository(session).delete_older_versions(doc.id, inp.version)
        for old in range(1, inp.version):
            await self.r.object_store.delete_prefix(document_prefix(doc.kb_id, doc.id, old) + "/")

    @activity.defn(name=c.ACT_FINALIZE)
    async def finalize_ingestion(self, inp: c.FinalizeInput) -> None:
        doc_id = uuid.UUID(inp.document_id)
        now = datetime.now(UTC)
        async with transaction(self.r.sessions) as session:
            updated = await DocumentRepository(session).set_status(
                doc_id,
                status="ready",
                expected_version=inp.version,
                chunk_count=inp.chunk_count,
                page_count=inp.page_count,
                indexed_at=now,
            )
            await JobRepository(session).update_by_workflow(
                inp.workflow_id,
                status="succeeded" if updated else "cancelled",
                stage="done",
                finished_at=now,
            )
            if updated and inp.page_count:
                doc = await DocumentRepository(session).get_by_id(doc_id)
                await LogRepository(session).add_usage(
                    [
                        UsageEvent(
                            id=new_id(),
                            client_id=doc.uploaded_by if doc else None,
                            kb_id=doc.kb_id if doc else None,
                            kind="pages_parsed",
                            quantity=inp.page_count,
                            ref_id=doc_id,
                        )
                    ]
                )

    @activity.defn(name=c.ACT_MARK_FAILED)
    async def mark_ingestion_failed(self, inp: c.FailInput) -> None:
        now = datetime.now(UTC)
        cancelled = inp.error_type in (c.ERR_SUPERSEDED, c.ERR_DELETED)
        error: dict[str, Any] = {"type": inp.error_type, "message": inp.message[:2000]}
        async with transaction(self.r.sessions) as session:
            if not cancelled:
                # Guarded: a failure of v1 never marks a newer v2 as failed.
                await DocumentRepository(session).set_status(
                    uuid.UUID(inp.document_id),
                    status="failed",
                    detail=inp.message[:500],
                    error=error,
                    expected_version=inp.version,
                )
            await JobRepository(session).update_by_workflow(
                inp.workflow_id,
                status="cancelled" if cancelled else "failed",
                error=error,
                finished_at=now,
            )
        log.warning("ingestion_failed", document_id=inp.document_id, **error)

    @activity.defn(name=c.ACT_DELETE_DATA)
    async def delete_document_data(self, inp: c.DeleteInput) -> None:
        """Remove a document everywhere. Safe to re-run: each step is idempotent."""
        doc_id = uuid.UUID(inp.document_id)
        async with self.r.sessions() as session:
            doc = await DocumentRepository(session).get_by_id(doc_id)
            kb = await session.get(KnowledgeBase, doc.kb_id) if doc else None
        if doc is None:
            return
        collection = kb.collection_name if kb else self.r.settings.qdrant_alias
        await self.r.vector_store.delete_document(collection, document_id=doc_id)
        await self.r.object_store.delete_prefix(document_root(doc.kb_id, doc_id))
        async with transaction(self.r.sessions) as session:
            await ChunkRepository(session).delete_all(doc_id)
            await DocumentRepository(session).mark_deleted(doc_id)
            await JobRepository(session).update_by_workflow(
                inp.workflow_id, status="succeeded", stage="done", finished_at=datetime.now(UTC)
            )
        log.info("document_deleted", document_id=inp.document_id)

    def all(self) -> list[Any]:
        """Activities served by the index worker (everything except parsing)."""
        return [
            self.mark_stage,
            self.chunk_document,
            self.embed_and_index,
            self.cleanup_old_versions,
            self.finalize_ingestion,
            self.mark_ingestion_failed,
            self.delete_document_data,
        ]


def _payload(doc: Document, chunk: Chunk, principals: list[str]) -> dict[str, Any]:
    """Qdrant payload (ARCHITECTURE.md §6.2). The chunk text is duplicated here so the
    query path needs no database round-trip to show or rerank results."""
    return {
        "kb_id": str(doc.kb_id),
        "document_id": str(doc.id),
        "document_version": chunk.document_version,
        "external_id": doc.external_id,
        "title": doc.title,
        "source_uri": doc.source_uri,
        "mime_type": doc.mime_type,
        "ordinal": chunk.ordinal,
        "text": chunk.text,
        "heading_path": chunk.heading_path,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "chunk_type": chunk.chunk_type,
        "acl": principals,
        "meta": doc.metadata_,
    }
