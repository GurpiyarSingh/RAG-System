"""Knowledge-base CRUD."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select

from rag_service.api.deps import ResourcesDep, require
from rag_service.api.schemas import KnowledgeBaseCreate, KnowledgeBaseOut
from rag_service.core.access import SCOPE_KB_READ, SCOPE_KB_WRITE, AccessContext
from rag_service.core.errors import ConflictError, ForbiddenError, NotFoundError
from rag_service.storage.db.models import Document
from rag_service.storage.db.session import transaction
from rag_service.storage.repositories.knowledge_bases import KnowledgeBaseRepository

router = APIRouter(prefix="/v1/knowledge-bases", tags=["knowledge-bases"])

ReadCtx = Annotated[AccessContext, Depends(require(SCOPE_KB_READ))]
WriteCtx = Annotated[AccessContext, Depends(require(SCOPE_KB_WRITE))]


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    body: KnowledgeBaseCreate, ctx: WriteCtx, resources: ResourcesDep
) -> KnowledgeBaseOut:
    # A client restricted to specific KBs couldn't access a KB it creates, so only
    # unrestricted clients may create KBs.
    if ctx.allowed_kb_ids is not None:
        raise ForbiddenError("Clients restricted to specific KBs can't create new KBs.")
    settings = resources.settings
    async with transaction(resources.sessions) as session:
        repo = KnowledgeBaseRepository(session)
        if await repo.get_by_name(body.name):
            raise ConflictError(f"A knowledge base named '{body.name}' already exists.")
        kb = await repo.create(
            ctx,
            name=body.name,
            description=body.description,
            # V1: all KBs share the active collection (ARCHITECTURE.md §5.2).
            collection_name=settings.qdrant_alias,
            # Fixed by the collection, so it's recorded but not client-selectable.
            embedding_config={
                "provider": "openai",
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
            },
            chunking_config=body.chunking.model_dump(),
            retrieval_config=body.retrieval.model_dump(),
            generation_config=body.generation.model_dump(exclude_none=True),
            metadata_schema={},
        )
    return KnowledgeBaseOut.model_validate(kb)


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(ctx: ReadCtx, resources: ResourcesDep) -> list[KnowledgeBaseOut]:
    async with resources.sessions() as session:
        kbs = await KnowledgeBaseRepository(session).list(ctx)
    return [KnowledgeBaseOut.model_validate(kb) for kb in kbs]


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: uuid.UUID, ctx: ReadCtx, resources: ResourcesDep
) -> KnowledgeBaseOut:
    async with resources.sessions() as session:
        kb = await KnowledgeBaseRepository(session).get(ctx, kb_id)
    if kb is None:
        raise NotFoundError("Knowledge base not found.")
    return KnowledgeBaseOut.model_validate(kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(kb_id: uuid.UUID, ctx: WriteCtx, resources: ResourcesDep) -> None:
    """Soft-delete the KB. Phase 1 refuses non-empty KBs; bulk deletion of contained
    documents (a fan-out workflow) arrives with the maintenance workflows."""
    async with transaction(resources.sessions) as session:
        repo = KnowledgeBaseRepository(session)
        kb = await repo.get(ctx, kb_id)
        if kb is None:
            raise NotFoundError("Knowledge base not found.")
        live = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
        )
        if live:
            raise ConflictError(
                f"The knowledge base still has {live} document(s); delete them first."
            )
        await repo.soft_delete(kb)
