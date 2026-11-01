"""Document ingestion and deletion (ARCHITECTURE.md §7.1 "Accept", §7.3).

The synchronous part of ingestion lives here: validate, deduplicate, version, store
the original, record the job, and start the workflow. Everything slow happens in
Temporal afterwards, so uploads return ``202 Accepted`` quickly.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from rag_service.core.errors import ConflictError, ServiceUnavailableError
from rag_service.core.ids import new_id
from rag_service.core.logging import get_logger
from rag_service.ingestion import contracts as c
from rag_service.ingestion.mime import detect_mime, extension_for
from rag_service.resources import Resources
from rag_service.storage.db.models import Document, KnowledgeBase
from rag_service.storage.db.session import transaction
from rag_service.storage.object_store import document_prefix
from rag_service.storage.repositories.documents import DocumentRepository, JobRepository

log = get_logger(__name__)


@dataclass(slots=True)
class IngestCommand:
    data: bytes
    filename: str | None
    declared_mime: str | None
    source_type: str  # upload | text
    external_id: str | None = None
    title: str | None = None
    source_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    acl: dict[str, Any] = field(default_factory=dict)
    client_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    document: Document
    job_id: uuid.UUID | None
    # True when identical content was already ingested and nothing was done.
    unchanged: bool


class DocumentService:
    def __init__(self, resources: Resources) -> None:
        self.r = resources

    async def ingest(self, kb: KnowledgeBase, cmd: IngestCommand) -> IngestOutcome:
        mime = detect_mime(cmd.data, cmd.filename, cmd.declared_mime)
        content_hash = hashlib.sha256(cmd.data).hexdigest()
        ext = extension_for(mime)

        try:
            async with transaction(self.r.sessions) as session:
                docs = DocumentRepository(session)
                existing = (
                    await docs.get_by_external_id(kb.id, cmd.external_id, for_update=True)
                    if cmd.external_id
                    else None
                )
                if existing is not None and existing.status == "deleting":
                    raise ConflictError("The document is being deleted; retry once it's gone.")
                # Same content again (typical for consumers re-syncing a folder): don't
                # re-parse or re-embed. Failed documents are retried on re-upload.
                if (
                    existing is not None
                    and existing.content_hash == content_hash
                    and existing.status != "failed"
                ):
                    return IngestOutcome(document=existing, job_id=None, unchanged=True)

                if existing is not None:
                    doc, version = existing, existing.version + 1
                else:
                    doc_id = new_id()
                    doc, version = Document(id=doc_id, kb_id=kb.id, version=1), 1
                    # Without a consumer ID, every upload is a new document.
                    doc.external_id = cmd.external_id or str(doc_id)

                prefix = document_prefix(kb.id, doc.id, version)
                # Upload inside the transaction: if it fails, no row points at a
                # missing object. (An orphaned object after a failed commit is harmless
                # and gets reclaimed by maintenance.)
                await self.r.object_store.put_bytes(f"{prefix}/original{ext}", cmd.data, mime)

                doc.version = version
                doc.source_type = cmd.source_type
                doc.source_uri = cmd.source_uri
                # Uploads default to their file name; text documents have only a
                # synthetic one ("document.md"), which would be a meaningless title.
                default_title = cmd.filename if cmd.source_type == "upload" else None
                doc.title = cmd.title or default_title
                doc.mime_type = mime
                doc.content_hash = content_hash
                doc.size_bytes = len(cmd.data)
                doc.storage_prefix = prefix
                doc.metadata_ = cmd.metadata
                doc.acl = cmd.acl
                doc.status = "pending"
                doc.status_detail = None
                doc.error = None
                doc.uploaded_by = cmd.client_id
                if existing is None:
                    await docs.add(doc)
                else:
                    await session.flush()

                job = await JobRepository(session).create(
                    kb_id=kb.id,
                    document_id=doc.id,
                    version=version,
                    workflow_id=c.ingest_workflow_id(str(doc.id), version),
                    kind="ingest",
                )
        except IntegrityError as exc:
            # Two concurrent first uploads of the same external_id: one wins the
            # unique index, the other is told to retry (and then dedups).
            raise ConflictError("A concurrent upload of this document is in progress.") from exc

        try:
            await self.r.temporal.start_ingest(str(doc.id), version)
        except Exception as exc:
            log.exception("ingest_schedule_failed", document_id=str(doc.id))
            await self._mark_unscheduled(doc.id, version, job.workflow_id)
            raise ServiceUnavailableError(
                "The document was stored but ingestion couldn't be scheduled. Re-upload to retry."
            ) from exc

        log.info("document_accepted", document_id=str(doc.id), version=version, mime=mime)
        return IngestOutcome(document=doc, job_id=job.id, unchanged=False)

    async def _mark_unscheduled(self, doc_id: uuid.UUID, version: int, workflow_id: str) -> None:
        error = {"type": "SchedulingFailed", "message": "Workflow engine unavailable"}
        async with transaction(self.r.sessions) as session:
            await DocumentRepository(session).set_status(
                doc_id, status="failed", error=error, expected_version=version
            )
            await JobRepository(session).update_by_workflow(
                workflow_id, status="failed", error=error, finished_at=datetime.now(UTC)
            )

    async def delete(self, kb: KnowledgeBase, doc: Document) -> uuid.UUID:
        """Hide the document from search immediately, then clean up asynchronously."""
        async with transaction(self.r.sessions) as session:
            await DocumentRepository(session).set_status(doc.id, status="deleting")
            job = await JobRepository(session).create(
                kb_id=kb.id,
                document_id=doc.id,
                version=doc.version,
                workflow_id=c.delete_workflow_id(str(doc.id)),
                kind="delete",
            )
        # Removing the vectors synchronously is what makes deletion visible right away;
        # Qdrant's delete-by-filter takes milliseconds.
        await self.r.vector_store.delete_document(kb.collection_name, document_id=doc.id)
        try:
            # A still-running ingestion would only be stopped by its guards later;
            # cancelling saves the work. Then the delete workflow removes rows/files.
            await self.r.temporal.cancel(c.ingest_workflow_id(str(doc.id), doc.version))
            await self.r.temporal.start_delete(str(doc.id))
        except Exception as exc:
            # The document is already invisible; cleanup resumes when the client retries.
            log.exception("delete_schedule_failed", document_id=str(doc.id))
            raise ServiceUnavailableError(
                "The document was removed from search, but cleanup couldn't be scheduled. "
                "Retry the delete."
            ) from exc
        return job.id
