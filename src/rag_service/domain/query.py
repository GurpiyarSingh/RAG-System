"""Retrieval and grounded answering (ARCHITECTURE.md §8).

``answer()`` is an async generator of provider-neutral events. The HTTP layer turns
them into Server-Sent Events, or collects them into one JSON response. Event order:

1. :class:`RetrievalEvent` — the sources, sent *before* generation starts so UIs can
   show them immediately;
2. :class:`DeltaEvent` x n — answer text as it's generated;
3. :class:`DoneEvent` — validated citations, token usage, latency.

Logging and usage metering run in the background after the response, so a slow
database write never delays the user.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from typing import Any

from rag_service.core.access import AccessContext
from rag_service.core.errors import NotFoundError
from rag_service.core.ids import new_id
from rag_service.core.logging import get_logger
from rag_service.generation.citations import check_citations, strip_invalid
from rag_service.generation.prompts import (
    ANSWER_PROMPT_VERSION,
    build_user_message,
    load_prompt,
)
from rag_service.providers.base import (
    ChatMessage,
    GenerationDelta,
    GenerationDone,
    GenerationRequest,
)
from rag_service.providers.pricing import cost_usd
from rag_service.resources import Resources
from rag_service.retrieval.context import Source, build_context
from rag_service.retrieval.pipeline import RetrievalResult, Retriever
from rag_service.storage.db.models import KnowledgeBase, QueryLog, UsageEvent
from rag_service.storage.db.session import transaction
from rag_service.storage.repositories.knowledge_bases import KnowledgeBaseRepository
from rag_service.storage.repositories.logs import LogRepository

log = get_logger(__name__)

NO_ANSWER = "I don't know based on the provided documents."
MAX_HISTORY_MESSAGES = 10

# Strong references to fire-and-forget tasks. The event loop keeps only weak ones,
# so an unreferenced task can be garbage-collected before it finishes.
_background: set[asyncio.Task[None]] = set()


def spawn_background(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@dataclass(frozen=True, slots=True)
class QueryOptions:
    query: str
    kb_ids: list[uuid.UUID]
    top_k: int
    history: list[ChatMessage] = field(default_factory=list)
    model: str | None = None
    max_output_tokens: int | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalEvent:
    query_id: uuid.UUID
    sources: list[Source]


@dataclass(frozen=True, slots=True)
class DeltaEvent:
    text: str


@dataclass(frozen=True, slots=True)
class DoneEvent:
    query_id: uuid.UUID
    answer: str
    citations: list[Source]  # sources actually cited, in order of first citation
    invalid_citations: list[int]
    model: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: dict[str, float]
    finish_reason: str | None


QueryEvent = RetrievalEvent | DeltaEvent | DoneEvent


class QueryService:
    def __init__(self, resources: Resources) -> None:
        self.r = resources
        self.retriever = Retriever(resources.embedder, resources.vector_store)

    async def resolve_kbs(self, ctx: AccessContext, kb_ids: list[uuid.UUID]) -> list[KnowledgeBase]:
        """All requested KBs must exist and be accessible; otherwise 404 (no partial results)."""
        unique = list(dict.fromkeys(kb_ids))
        async with self.r.sessions() as session:
            kbs = await KnowledgeBaseRepository(session).get_many(ctx, unique)
        missing = set(unique) - {kb.id for kb in kbs}
        if missing:
            raise NotFoundError(
                "One or more knowledge bases were not found.",
                kb_ids=sorted(str(k) for k in missing),
            )
        return kbs

    # ------------------------------------------------------------------ /retrieve
    async def retrieve(
        self, ctx: AccessContext, opts: QueryOptions
    ) -> tuple[uuid.UUID, RetrievalResult]:
        started = time.perf_counter()
        kbs = await self.resolve_kbs(ctx, opts.kb_ids)
        result = await self.retriever.retrieve(kbs, opts.query, top_k=opts.top_k)
        query_id = new_id()
        latency = {**result.timings_ms, "total": (time.perf_counter() - started) * 1000}
        spawn_background(
            self._record(
                ctx, opts, query_id, "retrieve", result, latency=latency, answer=None, done=None
            )
        )
        return query_id, result

    # ------------------------------------------------------------------ /query
    async def answer(self, ctx: AccessContext, opts: QueryOptions) -> AsyncIterator[QueryEvent]:
        started = time.perf_counter()
        settings = self.r.settings
        kbs = await self.resolve_kbs(ctx, opts.kb_ids)
        result = await self.retriever.retrieve(kbs, opts.query, top_k=opts.top_k)
        context = build_context(result.chunks, self.r.tokenizer, settings.context_token_budget)
        query_id = new_id()
        yield RetrievalEvent(query_id=query_id, sources=context.sources)

        # Generation settings: request override > KB config (first KB) > service default.
        kb_gen = kbs[0].generation_config if kbs else {}
        model = opts.model or kb_gen.get("model") or settings.llm_model
        max_tokens = int(
            opts.max_output_tokens
            or kb_gen.get("max_output_tokens")
            or settings.llm_max_output_tokens
        )

        parts: list[str] = []
        done: GenerationDone | None = None
        t_gen = time.perf_counter()
        first_token_ms: float | None = None

        if not context.sources:
            # Nothing relevant was found: answer without paying for an LLM call.
            parts.append(NO_ANSWER)
            yield DeltaEvent(text=NO_ANSWER)
        else:
            request = GenerationRequest(
                system=load_prompt(ANSWER_PROMPT_VERSION),
                messages=[
                    *opts.history[-MAX_HISTORY_MESSAGES:],
                    ChatMessage(role="user", content=build_user_message(context.text, opts.query)),
                ],
                model=model,
                max_output_tokens=max_tokens,
                temperature=kb_gen.get("temperature", settings.llm_temperature),
                reasoning_effort=settings.llm_reasoning_effort,
            )
            async for event in self.r.llm.stream(request):
                if isinstance(event, GenerationDelta):
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    parts.append(event.text)
                    yield DeltaEvent(text=event.text)
                elif isinstance(event, GenerationDone):
                    done = event

        raw_answer = "".join(parts)
        check = check_citations(raw_answer, len(context.sources))
        by_number = {s.number: s for s in context.sources}
        latency = {
            **result.timings_ms,
            "generate": (time.perf_counter() - t_gen) * 1000,
            "total": (time.perf_counter() - started) * 1000,
        }
        if first_token_ms is not None:
            latency["first_token"] = first_token_ms

        final = DoneEvent(
            query_id=query_id,
            # Streamed clients already have the raw text; the final answer drops markers
            # that point at non-existent sources.
            answer=strip_invalid(raw_answer, len(context.sources)),
            citations=[by_number[n] for n in check.used],
            invalid_citations=check.invalid,
            model=done.model if done else None,
            input_tokens=done.input_tokens if done else 0,
            output_tokens=done.output_tokens if done else 0,
            latency_ms=latency,
            finish_reason=done.finish_reason if done else None,
        )
        spawn_background(
            self._record(
                ctx, opts, query_id, "query", result, latency=latency, answer=final, done=done
            )
        )
        yield final

    # ------------------------------------------------------------------ logging
    async def _record(
        self,
        ctx: AccessContext,
        opts: QueryOptions,
        query_id: uuid.UUID,
        mode: str,
        result: RetrievalResult,
        *,
        latency: dict[str, float],
        answer: DoneEvent | None,
        done: GenerationDone | None,
    ) -> None:
        """Write the query log and usage events. Errors are logged, never raised."""
        try:
            embed_model = self.r.embedder.model_id
            usage = [
                UsageEvent(
                    id=new_id(),
                    client_id=ctx.client_id,
                    kind="embedding_tokens",
                    quantity=result.embedding_tokens,
                    model=embed_model,
                    cost_usd=cost_usd(embed_model, result.embedding_tokens),
                    ref_id=query_id,
                )
            ]
            llm_cost = None
            if done is not None:
                llm_cost = cost_usd(done.model, done.input_tokens, done.output_tokens)
                usage += [
                    UsageEvent(
                        id=new_id(),
                        client_id=ctx.client_id,
                        kind="llm_input_tokens",
                        quantity=done.input_tokens,
                        model=done.model,
                        cost_usd=cost_usd(done.model, done.input_tokens),
                        ref_id=query_id,
                    ),
                    UsageEvent(
                        id=new_id(),
                        client_id=ctx.client_id,
                        kind="llm_output_tokens",
                        quantity=done.output_tokens,
                        model=done.model,
                        cost_usd=cost_usd(done.model, 0, done.output_tokens),
                        ref_id=query_id,
                    ),
                ]
            entry = QueryLog(
                id=query_id,
                kb_ids=opts.kb_ids,
                client_id=ctx.client_id,
                mode=mode,
                query=opts.query,
                retrieved=[
                    {"chunk_id": str(ch.chunk_id), "score": ch.score, "rank": i + 1}
                    for i, ch in enumerate(result.chunks)
                ],
                answer=answer.answer if answer else None,
                citations=[s.number for s in answer.citations] if answer else None,
                model=done.model if done else None,
                tokens_in=done.input_tokens if done else None,
                tokens_out=done.output_tokens if done else None,
                cost_usd=llm_cost,
                latency_ms={k: round(v, 1) for k, v in latency.items()},
                status="ok",
                request_id=opts.request_id,
            )
            async with transaction(self.r.sessions) as session:
                repo = LogRepository(session)
                await repo.add_query_log(entry)
                await repo.add_usage(usage)
        except Exception:
            log.exception("query_log_failed", query_id=str(query_id))
