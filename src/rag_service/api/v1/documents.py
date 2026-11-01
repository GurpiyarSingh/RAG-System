"""Document ingestion and management within a knowledge base.

* ``POST .../documents``       multipart file upload
* ``POST .../documents/text``  raw text/Markdown as JSON

Both return **202 Accepted** with a job ID while ingestion runs in the background, or
**200** with ``unchanged: true`` when identical content was already ingested.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from pydantic import TypeAdapter, ValidationError

from rag_service.api.deps import ResourcesDep, require
from rag_service.api.schemas import (
    DocumentAccepted,
    DocumentList,
    DocumentOut,
    MetadataValue,
    TextDocumentCreate,
)
from rag_service.core.access import SCOPE_KB_READ, SCOPE_KB_WRITE, AccessContext
from rag_service.core.errors import BadRequestError, NotFoundError, PayloadTooLargeError
from rag_service.domain.documents import DocumentService, IngestCommand, IngestOutcome
from rag_service.resources import Resources
from rag_service.storage.db.models import KnowledgeBase
from rag_service.storage.repositories.documents import DocumentRepository
from rag_service.storage.repositories.knowledge_bases import KnowledgeBaseRepository

router = APIRouter(prefix="/v1/knowledge-bases/{kb_id}/documents", tags=["documents"])

ReadCtx = Annotated[AccessContext, Depends(require(SCOPE_KB_READ))]
WriteCtx = Annotated[AccessContext, Depends(require(SCOPE_KB_WRITE))]

_metadata_adapter = TypeAdapter(dict[str, MetadataValue])
_acl_adapter = TypeAdapter(dict[str, list[str]])
_READ_CHUNK = 1024 * 1024


async def _kb_or_404(resources: Resources, ctx: AccessContext, kb_id: uuid.UUID) -> KnowledgeBase:
    async with resources.sessions() as session:
        kb = await KnowledgeBaseRepository(session).get(ctx, kb_id)
    if kb is None:
        raise NotFoundError("Knowledge base not found.")
    return kb


def _parse_json_field(raw: str | None, adapter: TypeAdapter[Any], name: str) -> Any:
    """Multipart forms can't carry nested JSON natively, so these fields are JSON strings."""
    if not raw:
        return {}
    try:
        return adapter.validate_python(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise BadRequestError(f"'{name}' must be a JSON object of the documented shape.") from exc


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Read the upload in chunks and stop as soon as it exceeds the limit."""
    buf = bytearray()
    while chunk := await upload.read(_READ_CHUNK):
        buf.extend(chunk)
        if len(buf) > limit:
            raise PayloadTooLargeError(f"The file exceeds the {limit // (1024 * 1024)} MB limit.")
    return bytes(buf)


def _accepted(outcome: IngestOutcome, response: Response) -> DocumentAccepted:
    response.status_code = status.HTTP_200_OK if outcome.unchanged else status.HTTP_202_ACCEPTED
    return DocumentAccepted(
        document=DocumentOut.model_validate(outcome.document),
        job_id=outcome.job_id,
        unchanged=outcome.unchanged,
    )


@router.post(
    "",
    response_model=DocumentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": DocumentAccepted, "description": "Identical content already ingested"}
    },
)
async def upload_document(
    kb_id: uuid.UUID,
    ctx: WriteCtx,
    resources: ResourcesDep,
    response: Response,
    file: Annotated[
        UploadFile, File(description="PDF, DOCX, PPTX, XLSX, HTML, MD, TXT, CSV, PNG/JPG/TIFF")
    ],
    external_id: Annotated[str | None, Form(max_length=512)] = None,
    title: Annotated[str | None, Form(max_length=1000)] = None,
    source_uri: Annotated[str | None, Form(max_length=2000)] = None,
    metadata: Annotated[str | None, Form(description="JSON object")] = None,
    acl: Annotated[str | None, Form(description='JSON, e.g. {"principals": ["group:hr"]}')] = None,
) -> DocumentAccepted:
    kb = await _kb_or_404(resources, ctx, kb_id)
    data = await _read_limited(file, resources.settings.max_upload_bytes)
    outcome = await DocumentService(resources).ingest(
        kb,
        IngestCommand(
            data=data,
            filename=file.filename,
            declared_mime=file.content_type,
            source_type="upload",
            external_id=external_id,
            title=title,
            source_uri=source_uri,
            metadata=_parse_json_field(metadata, _metadata_adapter, "metadata"),
            acl=_parse_json_field(acl, _acl_adapter, "acl"),
            client_id=ctx.client_id,
        ),
    )
    return _accepted(outcome, response)


@router.post(
    "/text",
    response_model=DocumentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": DocumentAccepted, "description": "Identical content already ingested"}
    },
)
async def create_text_document(
    kb_id: uuid.UUID,
    body: TextDocumentCreate,
    ctx: WriteCtx,
    resources: ResourcesDep,
    response: Response,
) -> DocumentAccepted:
    kb = await _kb_or_404(resources, ctx, kb_id)
    data = body.text.encode("utf-8")
    if len(data) > resources.settings.max_upload_bytes:
        raise PayloadTooLargeError("The text exceeds the upload size limit.")
    outcome = await DocumentService(resources).ingest(
        kb,
        IngestCommand(
            data=data,
            # The extension tells MIME detection whether this is Markdown or plain text.
            filename="document.md" if body.format == "markdown" else "document.txt",
            declared_mime="text/markdown" if body.format == "markdown" else "text/plain",
            source_type="text",
            external_id=body.external_id,
            title=body.title,
            source_uri=body.source_uri,
            metadata=dict(body.metadata),
            acl=dict(body.acl),
            client_id=ctx.client_id,
        ),
    )
    return _accepted(outcome, response)


@router.get("", response_model=DocumentList)
async def list_documents(
    kb_id: uuid.UUID,
    ctx: ReadCtx,
    resources: ResourcesDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> DocumentList:
    await _kb_or_404(resources, ctx, kb_id)
    async with resources.sessions() as session:
        docs, next_cursor = await DocumentRepository(session).list(
            kb_id, limit=limit, cursor=cursor, status=status_filter
        )
    return DocumentList(
        items=[DocumentOut.model_validate(d) for d in docs], next_cursor=next_cursor
    )


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(
    kb_id: uuid.UUID, doc_id: uuid.UUID, ctx: ReadCtx, resources: ResourcesDep
) -> DocumentOut:
    await _kb_or_404(resources, ctx, kb_id)
    async with resources.sessions() as session:
        doc = await DocumentRepository(session).get(kb_id, doc_id)
    if doc is None:
        raise NotFoundError("Document not found.")
    return DocumentOut.model_validate(doc)


@router.delete("/{doc_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_document(
    kb_id: uuid.UUID, doc_id: uuid.UUID, ctx: WriteCtx, resources: ResourcesDep
) -> dict[str, str]:
    """Removes the document from search immediately; storage cleanup runs in the background."""
    kb = await _kb_or_404(resources, ctx, kb_id)
    async with resources.sessions() as session:
        doc = await DocumentRepository(session).get(kb_id, doc_id)
    if doc is None:
        raise NotFoundError("Document not found.")
    job_id = await DocumentService(resources).delete(kb, doc)
    return {"job_id": str(job_id), "status": "deleting"}
