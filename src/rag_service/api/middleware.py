"""Request-ID + access-log middleware (pure ASGI).

Pure ASGI instead of Starlette's ``BaseHTTPMiddleware``, because the latter buffers
and wraps streaming responses, which interferes with Server-Sent Events, and it adds
overhead to every request.

Every request gets an ``X-Request-ID``: the client's own if it sent one (so traces
correlate across services), otherwise a fresh one. It's bound into the structured
log context and echoed in the response and in error bodies.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rag_service.core.logging import get_logger

log = get_logger("rag_service.access")

# Accept only reasonable client IDs; anything else is replaced (log-injection guard).
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Health probes hit these every few seconds; logging them adds noise, not insight.
_QUIET_PATHS = {"/healthz", "/readyz"}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["path"] not in _QUIET_PATHS:
                log.info(
                    "http_request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            structlog.contextvars.clear_contextvars()
