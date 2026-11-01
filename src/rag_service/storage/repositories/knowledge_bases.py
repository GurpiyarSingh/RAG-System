"""Knowledge bases — the single place where KB allow-lists are enforced.

Every route that touches a KB, or anything inside one, resolves it through
:meth:`KnowledgeBaseRepository.get` or :meth:`get_many` first. A KB outside the
caller's allow-list behaves exactly like a missing one (``None`` → 404).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.core.access import AccessContext
from rag_service.storage.db.models import KnowledgeBase


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        ctx: AccessContext,
        *,
        name: str,
        description: str | None,
        collection_name: str,
        embedding_config: dict[str, Any],
        chunking_config: dict[str, Any],
        retrieval_config: dict[str, Any],
        generation_config: dict[str, Any],
        metadata_schema: dict[str, Any],
    ) -> KnowledgeBase:
        kb = KnowledgeBase(
            name=name,
            description=description,
            collection_name=collection_name,
            embedding_config=embedding_config,
            chunking_config=chunking_config,
            retrieval_config=retrieval_config,
            generation_config=generation_config,
            metadata_schema=metadata_schema,
            created_by=ctx.client_id,
        )
        self.session.add(kb)
        await self.session.flush()
        await self.session.refresh(kb)
        return kb

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        """Unscoped name lookup, used only to report duplicate names on create."""
        result = await self.session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.name == name, KnowledgeBase.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def get(self, ctx: AccessContext, kb_id: uuid.UUID) -> KnowledgeBase | None:
        """Return the KB if it exists, isn't deleted, and the caller may access it."""
        if not ctx.can_access_kb(kb_id):
            return None
        result = await self.session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def get_many(self, ctx: AccessContext, kb_ids: list[uuid.UUID]) -> list[KnowledgeBase]:
        """Resolve several KBs at once; inaccessible or unknown IDs are dropped."""
        allowed = [k for k in kb_ids if ctx.can_access_kb(k)]
        if not allowed:
            return []
        result = await self.session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(allowed), KnowledgeBase.deleted_at.is_(None)
            )
        )
        return list(result.scalars())

    async def list(self, ctx: AccessContext) -> list[KnowledgeBase]:
        result = await self.session.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.deleted_at.is_(None))
            .order_by(KnowledgeBase.created_at)
        )
        return [kb for kb in result.scalars() if ctx.can_access_kb(kb.id)]

    async def soft_delete(self, kb: KnowledgeBase) -> None:
        kb.deleted_at = datetime.now(UTC)
