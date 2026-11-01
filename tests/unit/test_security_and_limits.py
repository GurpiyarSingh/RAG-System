"""API-key handling, access rules and the Redis rate limiter."""

from __future__ import annotations

import time
import uuid

import fakeredis
import pytest

from rag_service.core.access import SCOPE_ADMIN, SCOPE_QUERY, AccessContext
from rag_service.core.security import generate_api_key, parse_api_key, verify_api_key
from rag_service.providers.ratelimit import Priority, RateLimiter


def test_api_key_roundtrip() -> None:
    key = generate_api_key("local", "pepper")
    parsed = parse_api_key(key.plaintext)
    assert parsed is not None
    assert parsed.prefix == key.prefix
    assert verify_api_key(key.plaintext, key.hash, "pepper")
    # Wrong pepper or tampered key fails.
    assert not verify_api_key(key.plaintext, key.hash, "other-pepper")
    assert not verify_api_key(key.plaintext + "x", key.hash, "pepper")


@pytest.mark.parametrize("bad", ["", "nope", "rag_local_short_x", "other_local_12345678_secret"])
def test_parse_rejects_foreign_keys(bad: str) -> None:
    assert parse_api_key(bad) is None


def test_kb_allow_list() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    restricted = AccessContext(
        client_id=uuid.uuid4(), scopes=frozenset({SCOPE_QUERY}), allowed_kb_ids=frozenset({kb_a})
    )
    assert restricted.can_access_kb(kb_a)
    assert not restricted.can_access_kb(kb_b)
    assert not restricted.has_scope(SCOPE_ADMIN)
    # Admins see everything and implicitly hold all scopes.
    admin = AccessContext(
        client_id=None, scopes=frozenset({SCOPE_ADMIN}), allowed_kb_ids=frozenset()
    )
    assert admin.can_access_kb(kb_b)
    assert admin.has_scope(SCOPE_QUERY)


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


async def test_rate_limiter_grants_within_budget(redis: fakeredis.FakeAsyncRedis) -> None:
    limiter = RateLimiter(redis, name="t1", rpm=600, tpm=60_000, reserve_ratio=0.3)
    started = time.monotonic()
    for _ in range(5):
        await limiter.acquire(1000, Priority.INTERACTIVE)
    assert time.monotonic() - started < 0.5  # no waiting while under budget


async def test_background_work_leaves_reserve_for_queries(redis: fakeredis.FakeAsyncRedis) -> None:
    # 1,000 tokens/min; background may only spend down to 30% (700 tokens).
    limiter = RateLimiter(redis, name="t2", rpm=1000, tpm=1000, reserve_ratio=0.3, max_wait_s=0.2)
    await limiter.acquire(700, Priority.BACKGROUND)

    started = time.monotonic()
    await limiter.acquire(100, Priority.BACKGROUND)  # would dip into the reserve → waits
    assert time.monotonic() - started >= 0.15

    started = time.monotonic()
    await limiter.acquire(250, Priority.INTERACTIVE)  # the reserve is available to queries
    assert time.monotonic() - started < 0.1


async def test_rate_limiter_fails_open_without_redis() -> None:
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    broken = fakeredis.FakeAsyncRedis(server=server)
    limiter = RateLimiter(broken, name="t3", rpm=1, tpm=1, reserve_ratio=0.0)
    await limiter.acquire(10_000, Priority.INTERACTIVE)  # must not raise
