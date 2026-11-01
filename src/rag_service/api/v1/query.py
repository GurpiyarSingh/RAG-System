"""``/v1/retrieve`` (ranked chunks only) and ``/v1/query`` (grounded answer).

Streaming (``"stream": true``) uses Server-Sent Events with these event types:

* ``retrieval`` — ``{"query_id", "sources": [...]}``, sent before generation starts;
* ``delta``     — ``{"text": "..."}``, answer fragments;
* ``done``      — the full :class:`QueryResponse` (validated citations, usage, latency);
* ``error``     — a Problem Details object, if something fails mid-stream.

SSE rather than WebSockets: it's plain HTTP (works through proxies, load balancers
and API gateways), one-directional like the use case, and browsers reconnect
natively.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from rag_service.api.deps import ResourcesDep, require
from rag_service.api.schemas import (
    ChunkOut,
    Citation,
    QueryRequest,
    QueryResponse,
    RetrieveRequest,
    RetrieveResponse,
    Usage,
)
from rag_service.core.access import SCOPE_QUERY, AccessContext
from rag_service.core.errors import PROBLEM_BASE, ProblemError
from rag_service.core.logging import get_logger
from rag_service.domain.query import (
    DeltaEvent,
    DoneEvent,
    QueryOptions,
    QueryService,
    RetrievalEvent,
)
from rag_service.providers.base import ChatMessage
from rag_service.retrieval.context import Source
from rag_service.retrieval.pipeline import RetrievedChunk

log = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["query"])

QueryCtx = Annotated[AccessContext, Depends(require(SCOPE_QUERY))]
SNIPPET_CHARS = 300


def _chunk_out(ch: RetrievedChunk) -> ChunkOut:
    return ChunkOut(
        chunk_id=ch.chunk_id,
        score=ch.score,
        kb_id=ch.kb_id,
        document_id=ch.document_id,
        external_id=ch.external_id,
        title=ch.title,
        text=ch.text,
        heading_path=ch.heading_path,
        page_start=ch.page_start,
        page_end=ch.page_end,
        source_uri=ch.source_uri,
        metadata=ch.metadata,
    )


def _citation(source: Source) -> Citation:
    ch = source.chunk
    snippet = ch.text if len(ch.text) <= SNIPPET_CHARS else ch.text[:SNIPPET_CHARS].rstrip() + "…"
    return Citation(
        id=source.number,
        chunk_id=ch.chunk_id,
        document_id=ch.document_id,
        kb_id=ch.kb_id,
        title=ch.title,
        page_start=ch.page_start,
        page_end=ch.page_end,
        source_uri=ch.source_uri,
        score=ch.score,
        snippet=snippet,
    )


def _response(done: DoneEvent, sources: list[Source]) -> QueryResponse:
    return QueryResponse(
        query_id=done.query_id,
        answer=done.answer,
        citations=[_citation(s) for s in done.citations],
        sources=[_citation(s) for s in sources],
        invalid_citations=done.invalid_citations,
        model=done.model,
        usage=Usage(input_tokens=done.input_tokens, output_tokens=done.output_tokens),
        finish_reason=done.finish_reason,
        latency_ms={k: round(v, 1) for k, v in done.latency_ms.items()},
    )


def _options(body: RetrieveRequest, request: Request, default_top_k: int) -> QueryOptions:
    history: list[ChatMessage] = []
    if isinstance(body, QueryRequest):
        history = [ChatMessage(role=m.role, content=m.content) for m in body.history]
    return QueryOptions(
        query=body.query,
        kb_ids=body.kb_ids,
        top_k=body.top_k or default_top_k,
        history=history,
        model=getattr(body, "model", None),
        max_output_tokens=getattr(body, "max_output_tokens", None),
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    body: RetrieveRequest, request: Request, ctx: QueryCtx, resources: ResourcesDep
) -> RetrieveResponse:
    """Ranked chunks for apps that run their own LLM or agent."""
    opts = _options(body, request, resources.settings.retrieval_top_k)
    query_id, result = await QueryService(resources).retrieve(ctx, opts)
    return RetrieveResponse(
        query_id=query_id,
        chunks=[_chunk_out(c) for c in result.chunks],
        latency_ms={k: round(v, 1) for k, v in result.timings_ms.items()},
    )


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest, request: Request, ctx: QueryCtx, resources: ResourcesDep
) -> Any:
    """Grounded answer with citations; set ``stream: true`` for Server-Sent Events."""
    service = QueryService(resources)
    opts = _options(body, request, resources.settings.retrieval_top_k)

    if not body.stream:
        sources: list[Source] = []
        async for event in service.answer(ctx, opts):
            if isinstance(event, RetrievalEvent):
                sources = event.sources
            elif isinstance(event, DoneEvent):
                return _response(event, sources)
        raise RuntimeError("answer stream ended without a done event")  # pragma: no cover

    # Resolve KBs before streaming starts, so a 404 is a normal HTTP error instead of
    # an error event inside a 200 stream.
    await service.resolve_kbs(ctx, opts.kb_ids)

    async def events() -> AsyncIterator[dict[str, str]]:
        sources: list[Source] = []
        try:
            async for event in service.answer(ctx, opts):
                if isinstance(event, RetrievalEvent):
                    sources = event.sources
                    payload = {
                        "query_id": str(event.query_id),
                        "sources": [_citation(s).model_dump(mode="json") for s in sources],
                    }
                    yield {"event": "retrieval", "data": json.dumps(payload)}
                elif isinstance(event, DeltaEvent):
                    yield {"event": "delta", "data": json.dumps({"text": event.text})}
                elif isinstance(event, DoneEvent):
                    yield {"event": "done", "data": _response(event, sources).model_dump_json()}
        except ProblemError as exc:
            # Headers (200) are already sent, so errors travel as an SSE event.
            problem = {
                "type": PROBLEM_BASE + exc.type_,
                "title": exc.title,
                "status": exc.status,
                "detail": exc.detail,
            }
            yield {"event": "error", "data": json.dumps(problem)}
        except Exception:
            log.exception("query_stream_failed")
            problem = {
                "type": PROBLEM_BASE + "internal-error",
                "title": "Internal Server Error",
                "status": 500,
            }
            yield {"event": "error", "data": json.dumps(problem)}

    # ping keeps idle proxies from closing the connection during slow generation.
    return EventSourceResponse(events(), ping=15)
