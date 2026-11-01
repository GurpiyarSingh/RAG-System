"""Retry policies for calls to external dependencies.

Only *transient* failures are retried (timeouts, connection errors, 429, 5xx), with
exponential backoff plus jitter so many workers don't retry in lockstep (the
"thundering herd"). Client errors such as 400/401 are never retried, because
repeating them can't succeed and only burns rate limit.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

from rag_service.core.logging import get_logger

log = get_logger(__name__)


def is_transient(exc: BaseException) -> bool:
    """True for errors worth retrying."""
    if isinstance(exc, openai.RateLimitError | openai.APITimeoutError | openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class _wait_retry_after(wait_base):
    """Honor a server-provided ``retry-after`` header, else fall back to backoff."""

    def __init__(self, fallback: wait_base) -> None:
        self.fallback = fallback

    def __call__(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        response = getattr(exc, "response", None)
        header = response.headers.get("retry-after") if response is not None else None
        if header:
            try:
                return min(float(header), 60.0)
            except ValueError:
                pass  # HTTP-date format; rare for these APIs, use backoff instead
        return self.fallback(retry_state)


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning(
        "retrying_external_call",
        attempt=state.attempt_number,
        error=type(exc).__name__ if exc else None,
    )


def retrying(attempts: int = 5, max_wait_s: float = 20.0) -> AsyncRetrying:
    """Build an ``AsyncRetrying`` controller for ``async for attempt in retrying(): ...``."""
    kwargs: dict[str, Any] = {
        "retry": retry_if_exception(is_transient),
        "stop": stop_after_attempt(attempts),
        "wait": _wait_retry_after(wait_exponential_jitter(multiplier=0.5, max=max_wait_s)),
        "before_sleep": _log_retry,
        "reraise": True,
    }
    return AsyncRetrying(**kwargs)
