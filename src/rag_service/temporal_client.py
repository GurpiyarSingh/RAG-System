"""Lazy Temporal client used by the API to start and cancel workflows.

The connection is opened on first use rather than at startup, so the API still boots
(and reports *not ready* on ``/readyz``) while Temporal is down, then recovers by
itself once Temporal is back.
"""

from __future__ import annotations

import asyncio
import contextlib

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from rag_service.core.config import Settings
from rag_service.core.logging import get_logger
from rag_service.ingestion import contracts as c

log = get_logger(__name__)


class TemporalGateway:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> Client:
        if self._client is None:
            async with self._lock:
                if self._client is None:  # another coroutine may have connected meanwhile
                    self._client = await Client.connect(
                        self._settings.temporal_address,
                        namespace=self._settings.temporal_namespace,
                    )
        return self._client

    async def ping(self) -> None:
        client = await self.client()
        await client.service_client.check_health()

    async def start_ingest(self, document_id: str, version: int) -> str:
        """Start ingestion; a duplicate start of the same version is a no-op."""
        workflow_id = c.ingest_workflow_id(document_id, version)
        client = await self.client()
        try:
            await client.start_workflow(
                "IngestDocument",
                c.IngestInput(document_id=document_id, version=version),
                id=workflow_id,
                task_queue=c.INDEX_QUEUE,
                # A failed ingestion of the same version may be retried by re-uploading.
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            log.info("ingest_already_running", workflow_id=workflow_id)
        return workflow_id

    async def start_delete(self, document_id: str) -> str:
        workflow_id = c.delete_workflow_id(document_id)
        client = await self.client()
        try:
            await client.start_workflow(
                "DeleteDocument",
                c.DeleteInput(document_id=document_id, workflow_id=workflow_id),
                id=workflow_id,
                task_queue=c.INDEX_QUEUE,
            )
        except WorkflowAlreadyStartedError:
            log.info("delete_already_running", workflow_id=workflow_id)
        return workflow_id

    async def cancel(self, workflow_id: str) -> None:
        """Best-effort cancellation (e.g. an ingestion of a document being deleted)."""
        client = await self.client()
        handle: WorkflowHandle[object, object] = client.get_workflow_handle(workflow_id)
        # Already finished or never existed: fine, the activities guard on status anyway.
        with contextlib.suppress(RPCError):
            await handle.cancel()
