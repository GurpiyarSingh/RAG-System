"""Run a Temporal worker for one task queue.

* ``index`` (rag-service image): hosts the workflows plus every activity except
  parsing, and creates the Qdrant collection on startup.
* ``parse`` (rag-parser image): only the parse activity, with the Docling stack.

They're separate processes so a backlog of scanned PDFs (minutes each) never blocks
small text documents waiting to be chunked and embedded, and each scales on its own
backlog (IMPLEMENTATION_PLAN.md §4 / ARCHITECTURE.md §4).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Literal

from temporalio.client import Client
from temporalio.worker import Worker

from rag_service.core.config import get_settings
from rag_service.core.logging import configure_logging, get_logger
from rag_service.ingestion import contracts as c
from rag_service.ingestion.activities import IngestionActivities
from rag_service.ingestion.workflows import DeleteDocumentWorkflow, IngestDocumentWorkflow
from rag_service.resources import build_resources

log = get_logger(__name__)

QueueName = Literal["index", "parse"]


async def _connect(address: str, namespace: str) -> Client:
    """Connect to Temporal, retrying while it starts up (common right after compose up)."""
    delay = 1.0
    while True:
        try:
            return await Client.connect(address, namespace=namespace)
        except Exception as exc:  # the SDK raises a variety of connection errors
            log.warning("temporal_connect_failed", error=str(exc), retry_in_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def run_worker(queue: QueueName) -> None:
    settings = get_settings()
    configure_logging(settings)
    resources = build_resources(settings, app_name=f"rag-worker-{queue}")
    activities = IngestionActivities(resources)
    client = await _connect(settings.temporal_address, settings.temporal_namespace)

    if queue == "index":
        # Make sure the vector collection and alias exist before the first upsert.
        await resources.vector_store.ensure_collection(
            alias=settings.qdrant_alias,
            dimension=resources.embedder.dimension,
            model_slug=resources.embedding_slug,
        )
        worker = Worker(
            client,
            task_queue=c.INDEX_QUEUE,
            workflows=[IngestDocumentWorkflow, DeleteDocumentWorkflow],
            activities=activities.all(),
            # I/O-bound activities (DB, OpenAI, Qdrant); a few in parallel per process.
            max_concurrent_activities=8,
        )
    else:
        worker = Worker(
            client,
            task_queue=c.PARSE_QUEUE,
            activities=[activities.parse_document],
            # Parsing is CPU/memory heavy and serialized inside the parser anyway.
            # Scale by running more parse-worker containers, not more slots.
            max_concurrent_activities=2,
        )

    # Graceful shutdown on SIGTERM (docker stop / Kubernetes): stop polling, let
    # running activities finish within the grace period, then exit.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows event loops lack signal handlers; Ctrl+C still raises KeyboardInterrupt.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("worker_started", queue=queue)
    async with worker:
        await stop.wait()
    log.info("worker_stopping", queue=queue)
    await resources.close()
