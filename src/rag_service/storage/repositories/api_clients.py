"""API clients: one credential per consuming application."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.storage.db.models import ApiClient


class ApiClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_prefix(self, prefix: str) -> ApiClient | None:
        """Auth lookup by the key's public prefix."""
        result = await self.session.execute(select(ApiClient).where(ApiClient.key_prefix == prefix))
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        name: str,
        key_prefix: str,
        key_hash: str,
        scopes: list[str],
        allowed_kb_ids: list[uuid.UUID] | None,
        expires_at: datetime | None,
    ) -> ApiClient:
        client = ApiClient(
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes,
            allowed_kb_ids=allowed_kb_ids,
            expires_at=expires_at,
        )
        self.session.add(client)
        await self.session.flush()  # populate server defaults (id, created_at)
        await self.session.refresh(client)
        return client

    async def list(self) -> list[ApiClient]:
        result = await self.session.execute(select(ApiClient).order_by(ApiClient.created_at))
        return list(result.scalars())

    async def get(self, client_id: uuid.UUID) -> ApiClient | None:
        return await self.session.get(ApiClient, client_id)

    async def revoke(self, client_id: uuid.UUID) -> bool:
        """Revoke a key; returns False if it doesn't exist or is already revoked."""
        result = await self.session.execute(
            update(ApiClient)
            .where(ApiClient.id == client_id, ApiClient.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def touch_last_used(self, client_id: uuid.UUID) -> None:
        await self.session.execute(
            update(ApiClient)
            .where(ApiClient.id == client_id)
            .values(last_used_at=datetime.now(UTC))
        )
