"""Error model: RFC 9457 "Problem Details" responses.

Domain code raises subclasses of :class:`ProblemError`; FastAPI handlers translate them
into ``application/problem+json`` with a stable ``type`` URI that clients can switch on.
Unexpected exceptions become a generic 500 that never leaks internals, but still
carries the ``request_id`` so support can find the full stack trace in the logs.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from rag_service.core.logging import get_logger

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"
# Base for problem type URIs. They don't need to resolve, only to be stable identifiers.
PROBLEM_BASE = "https://rag-service.dev/problems/"


class ProblemError(Exception):
    """Base class for every error that maps to a well-defined HTTP response."""

    status: int = 500
    type_: str = "internal-error"
    title: str = "Internal Server Error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        # Extension members (RFC 9457 §3.2), e.g. {"document_id": "..."}.
        self.extra = extra


class BadRequestError(ProblemError):
    status, type_, title = 400, "bad-request", "Bad Request"


class UnauthorizedError(ProblemError):
    status, type_, title = 401, "unauthorized", "Unauthorized"


class ForbiddenError(ProblemError):
    status, type_, title = 403, "forbidden", "Forbidden"


class NotFoundError(ProblemError):
    # Also used for KBs outside a client's allow-list, so their existence never leaks.
    status, type_, title = 404, "not-found", "Not Found"


class ConflictError(ProblemError):
    status, type_, title = 409, "conflict", "Conflict"


class PayloadTooLargeError(ProblemError):
    status, type_, title = 413, "payload-too-large", "Payload Too Large"


class UnsupportedMediaTypeError(ProblemError):
    status, type_, title = 415, "unsupported-media-type", "Unsupported Media Type"


class RateLimitedError(ProblemError):
    status, type_, title = 429, "rate-limited", "Too Many Requests"


class UpstreamError(ProblemError):
    """A dependency (OpenAI, Qdrant, ...) failed after retries."""

    status, type_, title = 502, "upstream-error", "Upstream Service Error"


class ServiceUnavailableError(ProblemError):
    status, type_, title = 503, "service-unavailable", "Service Unavailable"


class ModelNotConfiguredError(ServiceUnavailableError):
    """Raised when the OpenAI key is still the .env placeholder."""

    type_ = "model-not-configured"
    title = "Model Provider Not Configured"


def _problem(
    request: Request,
    status: int,
    type_: str,
    title: str,
    detail: str | None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_BASE + type_,
        "title": title,
        "status": status,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
    }
    if detail:
        body["detail"] = detail
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers that turn every error into a Problem Details response."""

    @app.exception_handler(ProblemError)
    async def _problem_error(request: Request, exc: ProblemError) -> JSONResponse:
        return _problem(request, exc.status, exc.type_, exc.title, exc.detail, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic error details are safe to return and very useful to API clients.
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return _problem(
            request,
            422,
            "validation-error",
            "Validation Error",
            "The request is invalid.",
            {"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers framework-generated errors such as 404 for unknown routes and 405.
        return _problem(
            request,
            exc.status_code,
            "http-error",
            str(exc.detail) if exc.detail else "HTTP Error",
            None,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        return _problem(request, 500, "internal-error", "Internal Server Error", None)
