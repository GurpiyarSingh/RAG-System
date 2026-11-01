"""Documents, chunks and ingestion jobs.

These repositories take a ``kb_id`` that the caller has *already* resolved through
``KnowledgeBaseRepository.get`` (which enforces the KB allow-list). Filtering every query
on ``kb_id`` then guarantees a document can only be reached through its own KB.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.storage.db.models import Chunk, Document, IngestionJob
from rag_service.storage.repositories.pagination import decode_cursor, encode_cursor


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, kb_id: uuid.UUID, doc_id: uuid.UUID) -> Document | None:
        result = await self.session.execute(
            select(Document).where(
                Document.id == doc_id, Document.kb_id == kb_id, Document.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, doc_id: uuid.UUID) -> Document | None:
        """Unscoped lookup for Temporal activities (IDs come from authorized API calls)."""
        return await self.session.get(Document, doc_id)

    async def get_by_external_id(
        self, kb_id: uuid.UUID, external_id: str, *, for_update: bool = False
    ) -> Document | None:
        stmt = select(Document).where(
            Document.kb_id == kb_id,
            Document.external_id == external_id,
            Document.deleted_at.is_(None),
        )
        if for_update:
            # Row lock: two concurrent uploads of the same external_id are serialized,
            # so only one of them bumps the version.
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def add(self, doc: Document) -> Document:
        self.session.add(doc)
        await self.session.flush()
        await self.session.refresh(doc)
        return doc

    async def list(
        self, kb_id: uuid.UUID, *, limit: int, cursor: str | None, status: str | None
    ) -> tuple[list[Document], str | None]:
        """Newest-first page of documents plus the cursor for the next page (or None)."""
        stmt = select(Document).where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
        if status:
            stmt = stmt.where(Document.status == status)
        if cursor:
            created_at, row_id = decode_cursor(cursor)
            stmt = stmt.where(tuple_(Document.created_at, Document.id) < (created_at, row_id))
        # Fetch one extra row to know whether another page exists.
        stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)
        rows = list((await self.session.execute(stmt)).scalars())
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)
        return rows, next_cursor

    async def set_status(
        self,
        doc_id: uuid.UUID,
        *,
        status: str,
        detail: str | None = None,
        error: dict[str, Any] | None = None,
        expected_version: int | None = None,
        **extra: Any,
    ) -> bool:
        """Update status. Returns False when the guard conditions prevented the update.

        * ``expected_version``: no-op if a newer upload of the same document won.
        * A document being deleted is never resurrected by a late ingestion step.
        """
        stmt = update(Document).where(Document.id == doc_id)
        if expected_version is not None:
            stmt = stmt.where(Document.version == expected_version)
        if status not in ("deleting", "deleted"):
            stmt = stmt.where(Document.status.not_in(("deleting", "deleted")))
        result = await self.session.execute(
            stmt.values(status=status, status_detail=detail, error=error, **extra)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def mark_deleted(self, doc_id: uuid.UUID) -> None:
        """Turn the row into a tombstone; the unique (kb_id, external_id) slot is freed."""
        await self.session.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(status="deleted", deleted_at=datetime.now(UTC), chunk_count=0)
        )


class ChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_version(
        self, document_id: uuid.UUID, version: int, chunks: Sequence[Chunk]
    ) -> None:
        """Idempotently (re)write the chunks of one document version.

        Deleting first makes an activity retry safe: a crash halfway through an insert
        can't leave a mix of old and new rows.
        """
        await self.session.execute(
            delete(Chunk).where(Chunk.document_id == document_id, Chunk.document_version == version)
        )
        self.session.add_all(chunks)
        await self.session.flush()

    async def list_version(self, document_id: uuid.UUID, version: int) -> list[Chunk]:
        result = await self.session.execute(
            select(Chunk)
            .where(Chunk.document_id == document_id, Chunk.document_version == version)
            .order_by(Chunk.ordinal)
        )
        return list(result.scalars())

    async def set_embedding_model(self, document_id: uuid.UUID, version: int, model: str) -> None:
        await self.session.execute(
            update(Chunk)
            .where(Chunk.document_id == document_id, Chunk.document_version == version)
            .values(embedding_model=model)
        )

    async def delete_older_versions(self, document_id: uuid.UUID, keep_version: int) -> int:
        result = await self.session.execute(
            delete(Chunk).where(
                Chunk.document_id == document_id, Chunk.document_version < keep_version
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def delete_all(self, document_id: uuid.UUID) -> int:
        result = await self.session.execute(delete(Chunk).where(Chunk.document_id == document_id))
        return int(result.rowcount)  # type: ignore[attr-defined]


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        version: int,
        workflow_id: str,
        kind: str,
    ) -> IngestionJob:
        job = IngestionJob(
            kb_id=kb_id,
            document_id=document_id,
            document_version=version,
            workflow_id=workflow_id,
            kind=kind,
        )
        self.session.add(job)
        await self.session.flush()
        await self.session.refresh(job)
        return job

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None:
        """Callers must check the job's ``kb_id`` against the caller's access afterwards."""
        return await self.session.get(IngestionJob, job_id)

    async def update_by_workflow(self, workflow_id: str, **values: Any) -> None:
        await self.session.execute(
            update(IngestionJob).where(IngestionJob.workflow_id == workflow_id).values(**values)
        )
