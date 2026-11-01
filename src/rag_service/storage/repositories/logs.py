"""Append-only query logs and usage events."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.storage.db.models import QueryLog, UsageEvent


class LogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_query_log(self, entry: QueryLog) -> None:
        self.session.add(entry)

    async def add_usage(self, events: Sequence[UsageEvent]) -> None:
        self.session.add_all(events)
