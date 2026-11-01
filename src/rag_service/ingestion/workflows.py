"""Temporal workflows for ingestion and deletion (ARCHITECTURE.md §7.2).

Workflows only *orchestrate*. Every side effect lives in an activity, which Temporal
retries according to its policy. If a worker dies mid-pipeline, the workflow resumes
from the last completed activity on another worker: no lost or doubled work.

Retry policy per stage reflects how each one fails:
* parse   — CPU heavy, failures are usually deterministic (corrupt file): few retries.
* embed   — network bound, failures are usually transient (429/5xx): many retries.
* DB/S3   — short, transient failures: default retries.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from rag_service.ingestion import contracts as c

# Errors that mean "stop now": retrying can't change the outcome.
_NON_RETRYABLE = [c.ERR_SUPERSEDED, c.ERR_DELETED, c.ERR_EMPTY, c.ERR_MODEL_NOT_CONFIGURED]

# Policy for short DB/S3 bookkeeping activities.
_SHORT_TIMEOUT = timedelta(seconds=60)
_SHORT_RETRY = RetryPolicy(maximum_attempts=5, non_retryable_error_types=_NON_RETRYABLE)


@workflow.defn(name="IngestDocument")
class IngestDocumentWorkflow:
    @workflow.run
    async def run(self, inp: c.IngestInput) -> c.ChunkOutput:
        wf_id = workflow.info().workflow_id
        try:
            await workflow.execute_activity(
                c.ACT_MARK_STAGE,
                c.StageInput(inp.document_id, inp.version, wf_id, "parsing"),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_SHORT_RETRY,
            )
            parsed: c.ParseOutput = await workflow.execute_activity(
                c.ACT_PARSE,
                inp,
                result_type=c.ParseOutput,
                task_queue=c.PARSE_QUEUE,
                start_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=10),
                    non_retryable_error_types=_NON_RETRYABLE,
                ),
            )
            await workflow.execute_activity(
                c.ACT_MARK_STAGE,
                c.StageInput(inp.document_id, inp.version, wf_id, "indexing"),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_SHORT_RETRY,
            )
            chunked: c.ChunkOutput = await workflow.execute_activity(
                c.ACT_CHUNK,
                c.ChunkInput(inp.document_id, inp.version, parsed.parsed_key),
                result_type=c.ChunkOutput,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, non_retryable_error_types=_NON_RETRYABLE
                ),
            )
            # Embedding and upserting happen in one activity: vectors are large
            # (1024 floats each), so passing them between activities through
            # Temporal's history would bloat it and hit payload size limits.
            await workflow.execute_activity(
                c.ACT_EMBED_INDEX,
                inp,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=10,
                    initial_interval=timedelta(seconds=5),
                    maximum_interval=timedelta(minutes=2),
                    non_retryable_error_types=_NON_RETRYABLE,
                ),
            )
            # New version is searchable now; only then remove the old one (zero gap).
            await workflow.execute_activity(
                c.ACT_CLEANUP_VERSIONS,
                inp,
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_SHORT_RETRY,
            )
            await workflow.execute_activity(
                c.ACT_FINALIZE,
                c.FinalizeInput(
                    inp.document_id, inp.version, wf_id, chunked.chunk_count, parsed.page_count
                ),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_SHORT_RETRY,
            )
            return chunked
        except ActivityError as err:
            cause = err.cause
            error_type = (
                cause.type if isinstance(cause, ApplicationError) and cause.type else "Error"
            )
            message = str(cause.message if isinstance(cause, ApplicationError) else cause or err)
            # Record the outcome on the document/job (a superseded or deleted document
            # is marked as cancelled, not failed), then fail the workflow so it's
            # visible as failed in the Temporal UI.
            await workflow.execute_activity(
                c.ACT_MARK_FAILED,
                c.FailInput(inp.document_id, inp.version, wf_id, error_type, message),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_SHORT_RETRY,
            )
            raise ApplicationError(message, type=error_type, non_retryable=True) from err


@workflow.defn(name="DeleteDocument")
class DeleteDocumentWorkflow:
    @workflow.run
    async def run(self, inp: c.DeleteInput) -> None:
        await workflow.execute_activity(
            c.ACT_DELETE_DATA,
            inp,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=20, maximum_interval=timedelta(minutes=5)),
        )
