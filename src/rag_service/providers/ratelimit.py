"""Distributed client-side rate limiter for the OpenAI key (IMPLEMENTATION_PLAN.md P1.8).

All API pods and workers share one OpenAI key, which has requests-per-minute (RPM)
and tokens-per-minute (TPM) limits. Two token buckets live in Redis and are updated
atomically by a Lua script, so every process sees the same budget.

**Priorities.** Interactive queries may use the whole budget. Background ingestion
may only spend down to a *floor* (``openai_query_reserve_ratio`` of capacity). A bulk
import therefore never starves live user queries of headroom.

**Fail open.** If Redis is unavailable the limiter lets calls through. OpenAI's own
429 responses plus our retry/backoff are the safety net, and a cache outage shouldn't
take the API down.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum

from redis.asyncio import Redis
from redis.exceptions import RedisError

from rag_service.core.logging import get_logger

log = get_logger(__name__)

# Both buckets refill continuously. ``floor`` is a fraction of capacity that this caller
# must leave untouched. Returns 0 when granted, else the milliseconds to wait.
_LUA_ACQUIRE = """
local function level(key, cap, rate, now)
  local v = redis.call('HMGET', key, 'level', 'ts')
  local lvl = tonumber(v[1])
  local ts = tonumber(v[2])
  if lvl == nil then return cap end
  return math.min(cap, lvl + (now - ts) * rate)
end

local now = tonumber(ARGV[1])
local rcap, rrate = tonumber(ARGV[2]), tonumber(ARGV[3])
local tcap, trate = tonumber(ARGV[4]), tonumber(ARGV[5])
local need, floor = tonumber(ARGV[6]), tonumber(ARGV[7])

local r = level(KEYS[1], rcap, rrate, now)
local t = level(KEYS[2], tcap, trate, now)
local rfloor, tfloor = rcap * floor, tcap * floor

local wait = 0
if r - 1 < rfloor then wait = math.max(wait, (rfloor + 1 - r) / rrate) end
if t - need < tfloor then wait = math.max(wait, (tfloor + need - t) / trate) end
if wait == 0 then
  r = r - 1
  t = t - need
end

redis.call('HSET', KEYS[1], 'level', r, 'ts', now)
redis.call('HSET', KEYS[2], 'level', t, 'ts', now)
redis.call('PEXPIRE', KEYS[1], 120000)
redis.call('PEXPIRE', KEYS[2], 120000)
return math.ceil(wait)
"""


class Priority(Enum):
    INTERACTIVE = "interactive"  # user-facing /query and /retrieve
    BACKGROUND = "background"  # ingestion, re-indexing


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        name: str,
        rpm: int,
        tpm: int,
        reserve_ratio: float,
        max_wait_s: float = 120.0,
    ) -> None:
        self._redis = redis
        self._keys = [f"rl:{name}:req", f"rl:{name}:tok"]
        self._rpm = rpm
        self._tpm = tpm
        self._reserve = reserve_ratio
        self._max_wait_s = max_wait_s
        self._script = redis.register_script(_LUA_ACQUIRE)

    async def acquire(self, tokens: int, priority: Priority) -> None:
        """Wait until one request and ``tokens`` tokens fit into the budget."""
        floor = self._reserve if priority is Priority.BACKGROUND else 0.0
        # A single request bigger than the usable budget could never be granted, so
        # clamp it. OpenAI will still accept or reject it on its own terms.
        need = min(tokens, int(self._tpm * (1 - floor)))
        # Users are waiting on interactive calls, so they queue only briefly.
        max_wait = self._max_wait_s if priority is Priority.BACKGROUND else 5.0
        deadline = time.monotonic() + max_wait
        while True:
            try:
                wait_ms = int(
                    await self._script(
                        keys=self._keys,
                        args=[
                            int(time.time() * 1000),
                            self._rpm,
                            self._rpm / 60_000,  # refill per millisecond
                            self._tpm,
                            self._tpm / 60_000,
                            need,
                            floor,
                        ],
                    )
                )
            except RedisError as exc:
                log.warning("rate_limiter_unavailable", error=str(exc))
                return  # fail open
            if wait_ms <= 0:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Waited as long as allowed: let the call proceed. Backoff on a real
                # 429 from OpenAI handles any remaining overload.
                log.warning("rate_limiter_wait_exceeded", priority=priority.value)
                return
            # Sleep in short slices so freed capacity is picked up promptly.
            await asyncio.sleep(min(wait_ms / 1000, remaining, 5.0))
