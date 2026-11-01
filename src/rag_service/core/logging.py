"""Structured logging setup.

Every log line is a JSON object (or a pretty console line in dev) carrying context
variables bound per request or per workflow activity: ``request_id``, ``client_id``,
``document_id`` ... This keeps logs searchable once they flow into Loki or similar.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from rag_service.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog and route stdlib logging (uvicorn, temporal, ...) through it."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,  # request_id etc. bound per request
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any
    if settings.log_format == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Third-party libraries log through stdlib; format them with the same renderer.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # Uvicorn installs its own handlers; let them propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # Our middleware writes access logs with more context, so silence the default one.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx logs every request at INFO, which is too noisy for OpenAI/Qdrant traffic.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger; module code calls ``log = get_logger(__name__)``."""
    return structlog.stdlib.get_logger(name)
