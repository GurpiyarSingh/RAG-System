"""OpenAI generation through the Responses API with streaming.

Mapping to our provider-neutral events:

* ``response.output_text.delta``  → :class:`GenerationDelta`
* ``response.completed``          → :class:`GenerationDone` (with token usage)
* ``response.failed`` / ``error`` → :class:`UpstreamError`

Only *opening* the stream is retried. Once text has reached the client, a retry would
send duplicated text, so mid-stream failures surface as an error event instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from rag_service.core.config import Settings
from rag_service.core.errors import ModelNotConfiguredError, UpstreamError
from rag_service.core.logging import get_logger
from rag_service.core.resilience import retrying
from rag_service.core.tokens import Tokenizer
from rag_service.providers.base import (
    GenerationDelta,
    GenerationDone,
    GenerationEvent,
    GenerationRequest,
)
from rag_service.providers.ratelimit import Priority, RateLimiter

log = get_logger(__name__)


class OpenAILLMProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncOpenAI,
        limiter: RateLimiter | None,
        tokenizer: Tokenizer,
    ) -> None:
        self._settings = settings
        self._client = client
        self._limiter = limiter
        self._tokenizer = tokenizer

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        if not self._settings.openai_key_configured:
            raise ModelNotConfiguredError(
                "OPENAI_API_KEY is still the placeholder in .env. Add a real key and restart."
            )

        if self._limiter:
            # Reserve the estimated prompt plus the maximum answer length.
            estimate = self._tokenizer.count(request.system) + request.max_output_tokens
            estimate += sum(self._tokenizer.count(m.content) for m in request.messages)
            await self._limiter.acquire(estimate, Priority.INTERACTIVE)

        params: dict[str, Any] = {
            "model": request.model,
            "instructions": request.system,
            "input": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_output_tokens": request.max_output_tokens,
            "stream": True,
            # Don't keep conversation state on OpenAI's side. The consuming app owns
            # chat history, and documents shouldn't be stored by the provider.
            "store": False,
        }
        if request.reasoning_effort:
            # Reasoning models (gpt-5*, o-series) take an effort level but reject
            # temperature. Classic chat models like gpt-4o are the other way round.
            params["reasoning"] = {"effort": request.reasoning_effort}
        elif request.temperature is not None:
            params["temperature"] = request.temperature

        async for attempt in retrying(attempts=3):
            with attempt:
                stream = await self._client.responses.create(**params)

        input_tokens = output_tokens = 0
        finish_reason: str | None = None
        try:
            async for event in stream:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    yield GenerationDelta(text=event.delta)
                elif kind in ("response.completed", "response.incomplete"):
                    response = event.response
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        input_tokens = usage.input_tokens or 0
                        output_tokens = usage.output_tokens or 0
                    finish_reason = getattr(response, "status", None)
                    if kind == "response.incomplete":
                        # Usually max_output_tokens reached: the answer is cut off but usable.
                        details = getattr(response, "incomplete_details", None)
                        finish_reason = getattr(details, "reason", None) or "incomplete"
                elif kind in ("response.failed", "error"):
                    message = getattr(event, "message", None) or "generation failed"
                    log.error("llm_stream_failed", event_type=kind, message=message)
                    raise UpstreamError("The language model failed to produce an answer.")
        finally:
            # Closes the HTTP connection if the client disconnected mid-stream, so the
            # provider stops generating (and billing for) tokens nobody will read.
            await stream.close()

        yield GenerationDone(
            model=request.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
        )
