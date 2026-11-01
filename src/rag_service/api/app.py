"""FastAPI application factories.

Two apps from one codebase, deployed as separate containers (ARCHITECTURE.md §4):

* ``create_api_app``   — control plane + ingestion (API clients, KBs, documents, jobs);
* ``create_query_app`` — latency-critical query plane (``/v1/retrieve``, ``/v1/query``).

Separate processes give separate scaling and failure isolation: a burst of large
uploads can't starve query traffic of CPU or connections.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from rag_service import __version__
from rag_service.api import health
from rag_service.api.middleware import RequestContextMiddleware
from rag_service.api.v1 import api_clients, documents, jobs, knowledge_bases, query
from rag_service.core.config import Settings, get_settings
from rag_service.core.errors import install_error_handlers
from rag_service.core.logging import configure_logging, get_logger
from rag_service.resources import build_resources

log = get_logger(__name__)


def _build(
    *, title: str, app_name: str, routers: Sequence[APIRouter], settings: Settings | None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resources = build_resources(settings, app_name=app_name)
        app.state.resources = resources
        if not settings.openai_key_configured:
            log.warning("openai_key_placeholder", hint="Set OPENAI_API_KEY in .env, then restart")
        try:
            # Idempotent; lets a fresh stack work before any worker has started.
            await resources.vector_store.ensure_collection(
                alias=settings.qdrant_alias,
                dimension=resources.embedder.dimension,
                model_slug=resources.embedding_slug,
            )
        except Exception as exc:
            # Qdrant may still be starting. /readyz reports it; the workers retry it.
            log.warning("qdrant_bootstrap_deferred", error=str(exc))
        log.info("app_started", app=app_name, version=__version__)
        yield
        await resources.close()

    app = FastAPI(
        title=title,
        version=__version__,
        lifespan=lifespan,
        description="Enterprise RAG service. See docs/ARCHITECTURE.md §10 for the API design.",
    )
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    for router in routers:
        app.include_router(router)
    return app


def create_api_app(settings: Settings | None = None) -> FastAPI:
    return _build(
        title="RAG Service — Management API",
        app_name="rag-api",
        routers=[api_clients.router, knowledge_bases.router, documents.router, jobs.router],
        settings=settings,
    )


def create_query_app(settings: Settings | None = None) -> FastAPI:
    return _build(
        title="RAG Service — Query API",
        app_name="rag-query",
        routers=[query.router],
        settings=settings,
    )
