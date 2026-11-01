"""S3-compatible object storage (MinIO locally, S3/GCS/Azure-compatible later).

Key layout (ARCHITECTURE.md §5.4)::

    {kb_id}/{document_id}/v{version}/original.{ext}
    {kb_id}/{document_id}/v{version}/parsed.json

Raw files live here rather than in PostgreSQL, because object storage is cheap and
durable, and big BLOBs would bloat the database and its backups. The parsed output is
kept too, so re-chunking or re-embedding never repeats the expensive parse/OCR step.
"""

from __future__ import annotations

import uuid
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from rag_service.core.config import Settings


def document_prefix(kb_id: uuid.UUID, document_id: uuid.UUID, version: int) -> str:
    return f"{kb_id}/{document_id}/v{version}"


def document_root(kb_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """Prefix covering every version of a document (used for deletion)."""
    return f"{kb_id}/{document_id}/"


class ObjectStore:
    """Thin async wrapper around the S3 API. One instance per process."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._session = aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.s3_endpoint_url,
            "region_name": settings.s3_region,
            "aws_access_key_id": settings.s3_access_key.get_secret_value() or None,
            "aws_secret_access_key": settings.s3_secret_key.get_secret_value() or None,
            # Path-style addressing is required by MinIO; retries cover transient errors.
            "config": Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                signature_version="s3v4",
            ),
        }

    @property
    def bucket(self) -> str:
        return self._bucket

    def _client(self) -> Any:
        return self._session.client("s3", **self._client_kwargs)

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    async def get_bytes(self, key: str) -> bytes:
        async with self._client() as s3:
            obj = await s3.get_object(Bucket=self._bucket, Key=key)
            async with obj["Body"] as body:
                data: bytes = await body.read()
                return data

    async def exists(self, key: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``; returns how many were removed."""
        removed = 0
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    # DeleteObjects accepts up to 1000 keys, which is exactly one page.
                    await s3.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})
                    removed += len(keys)
        return removed

    async def ensure_bucket(self) -> bool:
        """Create the bucket if it doesn't exist; returns True if it was created.

        Run by the one-shot ``bootstrap`` job, so local stacks need no separate
        bucket-init container.
        """
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self._bucket)
                return False
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in (
                    "404",
                    "NoSuchBucket",
                    "NotFound",
                ):
                    raise
            await s3.create_bucket(Bucket=self._bucket)
            return True

    async def ping(self) -> None:
        """Readiness check: the bucket exists and the credentials work."""
        async with self._client() as s3:
            await s3.head_bucket(Bucket=self._bucket)
