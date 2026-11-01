"""Manage API clients: one credential per consuming application."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from rag_service.api.deps import ResourcesDep, auth_cache, require
from rag_service.api.schemas import ApiClientCreate, ApiClientCreated, ApiClientOut
from rag_service.core.access import SCOPE_ADMIN, SCOPE_CLIENTS, AccessContext
from rag_service.core.errors import ForbiddenError, NotFoundError
from rag_service.core.security import generate_api_key
from rag_service.storage.db.session import transaction
from rag_service.storage.repositories.api_clients import ApiClientRepository

router = APIRouter(prefix="/v1/api-clients", tags=["api-clients"])

ClientsCtx = Annotated[AccessContext, Depends(require(SCOPE_CLIENTS))]


@router.post("", response_model=ApiClientCreated, status_code=status.HTTP_201_CREATED)
async def create_api_client(
    body: ApiClientCreate, ctx: ClientsCtx, resources: ResourcesDep
) -> ApiClientCreated:
    """Create a key for a consuming application. The plaintext key is returned once."""
    # Privilege-escalation guard: only admins can mint admin keys.
    if SCOPE_ADMIN in body.scopes and not ctx.is_admin:
        raise ForbiddenError("Only admin keys can create keys with the 'admin' scope.")
    settings = resources.settings
    key = generate_api_key(settings.env, settings.api_key_pepper.get_secret_value())
    async with transaction(resources.sessions) as session:
        client = await ApiClientRepository(session).create(
            name=body.name,
            key_prefix=key.prefix,
            key_hash=key.hash,
            scopes=body.scopes,
            allowed_kb_ids=body.allowed_kb_ids,
            expires_at=body.expires_at,
        )
    return ApiClientCreated(
        **ApiClientOut.model_validate(client).model_dump(), api_key=key.plaintext
    )


@router.get("", response_model=list[ApiClientOut])
async def list_api_clients(ctx: ClientsCtx, resources: ResourcesDep) -> list[ApiClientOut]:
    async with resources.sessions() as session:
        clients = await ApiClientRepository(session).list()
    return [ApiClientOut.model_validate(c) for c in clients]


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_client(client_id: uuid.UUID, ctx: ClientsCtx, resources: ResourcesDep) -> None:
    async with transaction(resources.sessions) as session:
        repo = ApiClientRepository(session)
        client = await repo.get(client_id)
        if client is None:
            raise NotFoundError("API client not found.")
        if "admin" in client.scopes and not ctx.is_admin:
            raise ForbiddenError("Only admin keys can revoke admin keys.")
        await repo.revoke(client_id)
    # Evict the auth cache entry so the revocation applies immediately.
    await auth_cache(resources).delete(client.key_prefix)
