"""Opaque cursor encoding for keyset pagination.

Keyset ("seek") pagination on ``(created_at, id)`` stays fast on large tables and
doesn't skip or repeat rows while new rows are inserted, which OFFSET pagination
can't guarantee.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from rag_service.core.errors import BadRequestError


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, row_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(created), uuid.UUID(row_id)
    except (ValueError, TypeError) as exc:
        raise BadRequestError("Invalid pagination cursor.") from exc
