"""Ingestion job status."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from rag_service.api.deps import ResourcesDep, require
from rag_service.api.schemas import JobOut
from rag_service.core.access import SCOPE_KB_READ, AccessContext
from rag_service.core.errors import NotFoundError
from rag_service.storage.repositories.documents import JobRepository
from rag_service.storage.repositories.knowledge_bases import KnowledgeBaseRepository

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    ctx: Annotated[AccessContext, Depends(require(SCOPE_KB_READ))],
    resources: ResourcesDep,
) -> JobOut:
    async with resources.sessions() as session:
        job = await JobRepository(session).get(job_id)
        # A job is visible only to callers who may access its KB.
        if job is None or await KnowledgeBaseRepository(session).get(ctx, job.kb_id) is None:
            raise NotFoundError("Job not found.")
    return JobOut.model_validate(job)
