"""Redis helpers: connection factory and a small JSON cache.

Redis is never a source of truth. Losing it only costs cache warmth and resets rate
limit buckets, so every caller tolerates Redis errors ("fail open") rather than
failing the request.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from rag_service.core.logging import get_logger

log = get_logger(__name__)


def create_redis(url: str) -> Redis:
    # Short timeouts: a slow Redis must not add seconds to an API call.
    return Redis.from_url(
        url,
        decode_responses=False,
        socket_timeout=1.0,
        socket_connect_timeout=1.0,
        health_check_interval=30,
    )


class JsonCache:
    """Namespaced JSON get/set with TTL that never raises on Redis failures."""

    def __init__(self, redis: Redis, namespace: str) -> None:
        self.redis = redis
        self.namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self.redis.get(self._key(key))
        except RedisError as exc:
            log.warning("cache_get_failed", namespace=self.namespace, error=str(exc))
            return None
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl_s: int) -> None:
        try:
            await self.redis.set(self._key(key), json.dumps(value), ex=ttl_s)
        except RedisError as exc:
            log.warning("cache_set_failed", namespace=self.namespace, error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            await self.redis.delete(self._key(key))
        except RedisError as exc:
            log.warning("cache_delete_failed", namespace=self.namespace, error=str(exc))
