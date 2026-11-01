"""Liveness and readiness endpoints.

* ``/healthz`` (liveness): the process is up and serving. It never checks
  dependencies; a restart wouldn't fix an unreachable database, it would only
  cause a restart storm.
* ``/readyz`` (readiness): every dependency answers within 2 s. Docker Compose
  health checks and, later, Kubernetes readiness probes use it to decide whether to
  route traffic here. The body says which dependency failed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from rag_service import __version__
from rag_service.api.deps import ResourcesDep
from rag_service.api.schemas import HealthOut
from rag_service.resources import Resources

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_S = 2.0


async def _db(r: Resources) -> None:
    async with r.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _redis(r: Resources) -> None:
    await r.redis.ping()


async def _qdrant(r: Resources) -> None:
    await r.vector_store.ping()


async def _s3(r: Resources) -> None:
    await r.object_store.ping()


async def _temporal(r: Resources) -> None:
    await r.temporal.ping()


CHECKS: dict[str, Callable[[Resources], Awaitable[None]]] = {
    "postgres": _db,
    "redis": _redis,
    "qdrant": _qdrant,
    "object_storage": _s3,
    "temporal": _temporal,
}


async def _run(name: str, check: Callable[[Resources], Awaitable[None]], r: Resources) -> str:
    try:
        await asyncio.wait_for(check(r), CHECK_TIMEOUT_S)
        return "ok"
    except TimeoutError:
        return "timeout"
    except Exception as exc:
        return f"error: {type(exc).__name__}"


@router.get("/healthz", response_model=HealthOut)
async def healthz() -> HealthOut:
    return HealthOut(status="ok", version=__version__)


@router.get("/readyz", response_model=HealthOut, responses={503: {"model": HealthOut}})
async def readyz(resources: ResourcesDep) -> JSONResponse:
    names = list(CHECKS)
    # Run all checks concurrently, so readiness takes ~max(check), not the sum.
    results = await asyncio.gather(*(_run(n, CHECKS[n], resources) for n in names))
    checks = dict(zip(names, results, strict=True))
    if not resources.settings.openai_key_configured:
        # Not a readiness failure: the stack works, but model calls will return 503.
        checks["openai"] = "placeholder key (set OPENAI_API_KEY)"
    ready = all(v == "ok" for k, v in checks.items() if k != "openai")
    body = HealthOut(status="ok" if ready else "unavailable", checks=checks, version=__version__)
    return JSONResponse(body.model_dump(), status_code=200 if ready else 503)
