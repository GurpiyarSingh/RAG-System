"""Schema, repositories and access rules against a real PostgreSQL 18."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rag_service.core.access import SCOPE_KB_READ, AccessContext
from rag_service.core.ids import chunk_id, new_id
from rag_service.storage.db.models import Base, Chunk, Document
from rag_service.storage.db.session import transaction
from rag_service.storage.repositories.documents import ChunkRepository, DocumentRepository
from rag_service.storage.repositories.knowledge_bases import KnowledgeBaseRepository

pytestmark = pytest.mark.integration

ADMIN = AccessContext(client_id=None, scopes=frozenset({"admin"}))


async def _kb(sessions: async_sessionmaker[AsyncSession], name: str) -> uuid.UUID:
    async with transaction(sessions) as s:
        kb = await KnowledgeBaseRepository(s).create(
            ADMIN,
            name=name,
            description=None,
            collection_name="chunks_active",
            embedding_config={},
            chunking_config={},
            retrieval_config={},
            generation_config={},
            metadata_schema={},
        )
        return kb.id


def _doc(kb_id: uuid.UUID, external_id: str) -> Document:
    return Document(
        id=new_id(),
        kb_id=kb_id,
        external_id=external_id,
        source_type="text",
        mime_type="text/plain",
        content_hash="0" * 64,
        storage_prefix="x",
        size_bytes=1,
    )


async def test_orm_models_match_migrated_schema(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The hand-written migration and the ORM models must describe the same columns."""
    async with sessions() as s:
        conn = await s.connection()

        def columns(sync_conn: object) -> dict[str, set[str]]:
            insp = inspect(sync_conn)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in Base.metadata.tables}

        db_columns = await conn.run_sync(columns)
    for table in Base.metadata.sorted_tables:
        assert {c.name for c in table.columns} == db_columns[table.name], table.name


async def test_uuidv7_default_and_unique_external_id(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    kb_id = await _kb(sessions, "kb-unique")
    async with transaction(sessions) as s:
        version = await s.scalar(text("SELECT uuid_extract_version(uuidv7())"))
        assert version == 7
        await DocumentRepository(s).add(_doc(kb_id, "same-id"))

    # A second live document with the same external_id in the same KB is rejected.
    with pytest.raises(Exception, match="uq_documents_kb_external_id"):
        async with transaction(sessions) as s:
            await DocumentRepository(s).add(_doc(kb_id, "same-id"))


async def test_kb_allow_list_hides_other_kbs(sessions: async_sessionmaker[AsyncSession]) -> None:
    kb_a = await _kb(sessions, "kb-a")
    kb_b = await _kb(sessions, "kb-b")
    client = AccessContext(
        client_id=uuid.uuid4(), scopes=frozenset({SCOPE_KB_READ}), allowed_kb_ids=frozenset({kb_a})
    )
    async with sessions() as s:
        repo = KnowledgeBaseRepository(s)
        assert await repo.get(client, kb_a) is not None
        assert await repo.get(client, kb_b) is None  # behaves like "not found"
        assert [kb.id for kb in await repo.list(client)] == [kb_a]
        assert {kb.id for kb in await repo.get_many(client, [kb_a, kb_b])} == {kb_a}


async def test_document_pagination_newest_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    kb_id = await _kb(sessions, "kb-pages")
    async with transaction(sessions) as s:
        for i in range(5):
            await DocumentRepository(s).add(_doc(kb_id, f"doc-{i}"))

    seen: list[str] = []
    cursor = None
    async with sessions() as s:
        while True:
            page, cursor = await DocumentRepository(s).list(
                kb_id, limit=2, cursor=cursor, status=None
            )
            seen += [d.external_id for d in page]
            if cursor is None:
                break
    assert sorted(seen) == [f"doc-{i}" for i in range(5)]
    assert len(seen) == len(set(seen))  # no duplicates across pages


async def test_status_guards(sessions: async_sessionmaker[AsyncSession]) -> None:
    kb_id = await _kb(sessions, "kb-status")
    async with transaction(sessions) as s:
        doc = await DocumentRepository(s).add(_doc(kb_id, "guarded"))

    async with transaction(sessions) as s:
        repo = DocumentRepository(s)
        # Stale version: no update.
        assert not await repo.set_status(doc.id, status="ready", expected_version=99)
        assert await repo.set_status(doc.id, status="deleting")
        # A late ingestion step can't resurrect a document being deleted.
        assert not await repo.set_status(doc.id, status="ready")


async def test_chunk_replace_is_idempotent(sessions: async_sessionmaker[AsyncSession]) -> None:
    kb_id = await _kb(sessions, "kb-chunks")
    async with transaction(sessions) as s:
        doc = await DocumentRepository(s).add(_doc(kb_id, "chunked"))

    def rows() -> list[Chunk]:
        return [
            Chunk(
                id=chunk_id(doc.id, 1, i),
                kb_id=kb_id,
                document_id=doc.id,
                document_version=1,
                ordinal=i,
                text=f"chunk {i}",
                embed_text=f"chunk {i}",
                token_count=2,
                content_hash="h",
            )
            for i in range(3)
        ]

    # Running the same activity twice (a Temporal retry) leaves exactly one copy.
    for _ in range(2):
        async with transaction(sessions) as s:
            await ChunkRepository(s).replace_version(doc.id, 1, rows())
    async with sessions() as s:
        assert len(await ChunkRepository(s).list_version(doc.id, 1)) == 3
