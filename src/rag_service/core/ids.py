"""Identifier helpers.

* UUIDv7 for entity IDs: globally unique, safe to generate in application code
  (needed before insert, e.g. for idempotent Temporal workflow IDs), and time-ordered
  so B-tree inserts stay append-only. Python 3.14 provides ``uuid.uuid7()`` and
  PostgreSQL 18 provides ``uuidv7()`` for server-side defaults.
* Deterministic UUIDv5 for chunk IDs: the same (document, version, ordinal) always
  maps to the same ID, so a retried upsert overwrites instead of duplicating.
"""

from __future__ import annotations

import uuid

# Fixed namespace for chunk IDs. Never change it: existing point IDs depend on it.
_CHUNK_NAMESPACE = uuid.UUID("6f1b3c2e-9a4d-4f5e-8b7a-2c1d0e9f8a7b")


def new_id() -> uuid.UUID:
    """Return a new time-ordered UUIDv7."""
    return uuid.uuid7()


def chunk_id(document_id: uuid.UUID, version: int, ordinal: int) -> uuid.UUID:
    """Deterministic ID for a chunk; also used as its Qdrant point ID."""
    return uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}:{version}:{ordinal}")
