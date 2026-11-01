"""FastAPI dependencies: resources, authentication, scope checks.

Authentication flow for ``Authorization: Bearer <key>`` (or ``X-API-Key: <key>``):

1. The bootstrap admin key from ``.env`` is accepted directly (fresh installs need
   a way to create the first API clients).
2. Otherwise the key is parsed and its public prefix is looked up, first in a short
   Redis cache (60 s), then in PostgreSQL.
3. The presented key is verified against the stored HMAC hash in constant time.
   Revoked and expired keys are rejected.

Cache entries live only 60 s, and revoking a key also evicts its entry, so revocation
takes effect immediately on the revoking node and within a minute everywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import Depends, Header, Request

from rag_service.core.access import ALL_SCOPES, AccessContext
from rag_service.core.errors import ForbiddenError, UnauthorizedError
from rag_service.core.security import constant_time_equals, parse_api_key, verify_api_key
from rag_service.resources import Resources
from rag_service.storage.cache import JsonCache
from rag_service.storage.db.session import transaction
from rag_service.storage.repositories.api_clients import ApiClientRepository

AUTH_CACHE_TTL_S = 60


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def auth_cache(resources: Resources) -> JsonCache:
    return JsonCache(resources.redis, "auth")


def _extract_key(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    raise UnauthorizedError("Missing API key. Send 'Authorization: Bearer <key>'.")


async def _load_client_record(resources: Resources, prefix: str) -> dict[str, Any] | None:
    """Fetch the auth-relevant fields of a client, cached briefly in Redis."""
    cache = auth_cache(resources)
    cached = await cache.get(prefix)
    if cached is not None:
        return cached if cached else None  # {} caches "no such key"
    async with transaction(resources.sessions) as session:
        repo = ApiClientRepository(session)
        client = await repo.get_by_prefix(prefix)
        if client is not None:
            # Updated only on cache misses: at most once per minute per key, instead of
            # one write per request.
            await repo.touch_last_used(client.id)
    record: dict[str, Any] = (
        {
            "id": str(client.id),
            "key_hash": client.key_hash,
            "scopes": client.scopes,
            "allowed_kb_ids": [str(k) for k in client.allowed_kb_ids]
            if client.allowed_kb_ids is not None
            else None,
            "expires_at": client.expires_at.isoformat() if client.expires_at else None,
            "revoked": client.revoked_at is not None,
        }
        if client
        else {}
    )
    await cache.set(prefix, record, AUTH_CACHE_TTL_S)
    return record or None


async def authenticate(
    request: Request,
    resources: ResourcesDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> AccessContext:
    key = _extract_key(authorization, x_api_key)
    settings = resources.settings

    bootstrap = settings.bootstrap_admin_key
    if (
        bootstrap
        and bootstrap.get_secret_value()
        and constant_time_equals(key, bootstrap.get_secret_value())
    ):
        ctx = AccessContext(client_id=None, scopes=ALL_SCOPES)
        structlog.contextvars.bind_contextvars(client_id="bootstrap-admin")
        request.state.access = ctx
        return ctx

    parsed = parse_api_key(key)
    record = await _load_client_record(resources, parsed.prefix) if parsed else None
    pepper = settings.api_key_pepper.get_secret_value()
    if record is None or not verify_api_key(key, record["key_hash"], pepper):
        raise UnauthorizedError("Invalid API key.")
    if record["revoked"]:
        raise UnauthorizedError("This API key has been revoked.")
    if record["expires_at"] and datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC):
        raise UnauthorizedError("This API key has expired.")

    allowed = record["allowed_kb_ids"]
    ctx = AccessContext(
        client_id=uuid.UUID(record["id"]),
        scopes=frozenset(record["scopes"]),
        allowed_kb_ids=frozenset(uuid.UUID(k) for k in allowed) if allowed is not None else None,
    )
    # Every log line of this request now names the calling application.
    structlog.contextvars.bind_contextvars(client_id=record["id"])
    request.state.access = ctx
    return ctx


def require(scope: str) -> Callable[..., Awaitable[AccessContext]]:
    """Dependency factory: authenticate, then demand ``scope``."""

    async def checker(ctx: Annotated[AccessContext, Depends(authenticate)]) -> AccessContext:
        if not ctx.has_scope(scope):
            raise ForbiddenError(f"This API key lacks the '{scope}' scope.")
        return ctx

    return checker
