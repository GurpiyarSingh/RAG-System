"""Initial schema: API clients, knowledge bases, documents, chunks, jobs, logs.

Written as explicit SQL instead of autogenerate, because it uses PostgreSQL features
the ORM metadata can't express: partial unique indexes, declarative partitioning
and the PG 18 ``uuidv7()`` default.

Revision ID: 0001
Revises:
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------ api_clients
    # One row per consuming application. Its scopes and KB allow-list are the access
    # boundary between applications (ARCHITECTURE.md §11.2).
    op.execute("""
        CREATE TABLE api_clients (
            id              uuid PRIMARY KEY DEFAULT uuidv7(),
            name            varchar(200) NOT NULL,
            key_prefix      varchar(16)  NOT NULL UNIQUE,  -- lookup key, safe to display
            key_hash        varchar(128) NOT NULL,          -- HMAC-SHA256 of the full key
            scopes          text[]       NOT NULL DEFAULT '{}'::text[],
            allowed_kb_ids  uuid[],                         -- NULL = every KB
            last_used_at    timestamptz,
            expires_at      timestamptz,
            revoked_at      timestamptz,
            created_at      timestamptz  NOT NULL DEFAULT now()
        )
    """)

    # ------------------------------------------------------------------ knowledge_bases
    op.execute("""
        CREATE TABLE knowledge_bases (
            id                 uuid PRIMARY KEY DEFAULT uuidv7(),
            name               varchar(200) NOT NULL,
            description        text,
            collection_name    varchar(200) NOT NULL,
            embedding_config   jsonb NOT NULL DEFAULT '{}'::jsonb,
            chunking_config    jsonb NOT NULL DEFAULT '{}'::jsonb,
            retrieval_config   jsonb NOT NULL DEFAULT '{}'::jsonb,
            generation_config  jsonb NOT NULL DEFAULT '{}'::jsonb,
            metadata_schema    jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_by         uuid,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            deleted_at         timestamptz
        )
    """)
    # Partial unique index: a soft-deleted KB doesn't block reusing its name.
    op.execute("""
        CREATE UNIQUE INDEX uq_knowledge_bases_name
            ON knowledge_bases (name) WHERE deleted_at IS NULL
    """)

    # ------------------------------------------------------------------ documents
    op.execute("""
        CREATE TABLE documents (
            id              uuid PRIMARY KEY DEFAULT uuidv7(),
            kb_id           uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            external_id     varchar(512) NOT NULL,
            source_type     varchar(50)  NOT NULL,
            source_uri      text,
            title           text,
            mime_type       varchar(200) NOT NULL,
            language        varchar(20),
            content_hash    varchar(64)  NOT NULL,
            version         integer      NOT NULL DEFAULT 1,
            status          varchar(20)  NOT NULL DEFAULT 'pending'
                            CONSTRAINT documents_status_valid CHECK (status IN
                            ('pending','parsing','indexing','ready','failed','deleting','deleted')),
            status_detail   text,
            error           jsonb,
            acl             jsonb NOT NULL DEFAULT '{}'::jsonb,
            metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
            storage_prefix  text   NOT NULL,
            size_bytes      bigint NOT NULL,
            page_count      integer,
            chunk_count     integer,
            uploaded_by     uuid,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            indexed_at      timestamptz,
            deleted_at      timestamptz
        )
    """)
    # One live document per (KB, external_id): makes consumer re-syncs idempotent.
    op.execute("""
        CREATE UNIQUE INDEX uq_documents_kb_external_id
            ON documents (kb_id, external_id) WHERE deleted_at IS NULL
    """)
    # Listing is "newest first within a KB", served by cursor pagination on (created_at, id).
    op.execute(
        "CREATE INDEX ix_documents_kb_created ON documents (kb_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_documents_metadata ON documents USING gin (metadata jsonb_path_ops)"
    )

    # ------------------------------------------------------------------ chunks
    op.execute("""
        CREATE TABLE chunks (
            id                uuid PRIMARY KEY,    -- deterministic, equals the Qdrant point ID
            kb_id             uuid NOT NULL,
            document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            document_version  integer NOT NULL,
            ordinal           integer NOT NULL,
            parent_id         uuid,
            text              text NOT NULL,
            embed_text        text NOT NULL,
            token_count       integer NOT NULL,
            content_hash      varchar(64) NOT NULL,
            heading_path      text[] NOT NULL DEFAULT '{}'::text[],
            page_start        integer,
            page_end          integer,
            chunk_type        varchar(20) NOT NULL DEFAULT 'text',
            embedding_model   varchar(100),
            created_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_chunks_document_version ON chunks (document_id, document_version, ordinal)"
    )

    # ------------------------------------------------------------------ ingestion_jobs
    op.execute("""
        CREATE TABLE ingestion_jobs (
            id                uuid PRIMARY KEY DEFAULT uuidv7(),
            kb_id             uuid NOT NULL,
            document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            document_version  integer NOT NULL,
            workflow_id       varchar(200) NOT NULL,
            kind              varchar(20) NOT NULL,
            status            varchar(20) NOT NULL DEFAULT 'queued'
                              CONSTRAINT ingestion_jobs_status_valid CHECK (status IN
                              ('queued','running','succeeded','failed','cancelled')),
            stage             varchar(50),
            attempts          integer NOT NULL DEFAULT 0,
            error             jsonb,
            started_at        timestamptz,
            finished_at       timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_ingestion_jobs_document ON ingestion_jobs (document_id, created_at DESC)"
    )
    op.execute("CREATE INDEX ix_ingestion_jobs_workflow ON ingestion_jobs (workflow_id)")

    # ------------------------------------------------------------------ partitioned logs
    # High-volume, append-only tables are range-partitioned by month: retention becomes
    # "DROP the old partition" instead of a slow, bloating DELETE. A DEFAULT partition
    # catches rows if the monthly partition hasn't been created yet, so inserts never fail.
    op.execute("""
        CREATE TABLE query_logs (
            id          uuid NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            kb_ids      uuid[] NOT NULL,
            client_id   uuid,
            mode        varchar(10) NOT NULL,
            query       text NOT NULL,
            retrieved   jsonb NOT NULL DEFAULT '[]'::jsonb,
            answer      text,
            citations   jsonb,
            model       varchar(100),
            tokens_in   integer,
            tokens_out  integer,
            cost_usd    numeric(12,6),
            latency_ms  jsonb NOT NULL DEFAULT '{}'::jsonb,
            cache_hit   boolean NOT NULL DEFAULT false,
            status      varchar(20) NOT NULL DEFAULT 'ok',
            request_id  varchar(64),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """)
    op.execute("""
        CREATE TABLE usage_events (
            id          uuid NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            client_id   uuid,
            kb_id       uuid,
            kind        varchar(50) NOT NULL,
            quantity    bigint NOT NULL,
            model       varchar(100),
            cost_usd    numeric(12,6),
            ref_id      uuid,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """)
    for table in ("query_logs", "usage_events"):
        op.execute(f"CREATE TABLE {table}_default PARTITION OF {table} DEFAULT")
        # Usage and logs are reported per consuming application over time.
        op.execute(
            f"CREATE INDEX ix_{table}_client_created ON {table} (client_id, created_at DESC)"
        )

    # Helper to create a month's partition. Called here for the current and next two
    # months, and later by a scheduled maintenance workflow.
    op.execute("""
        CREATE OR REPLACE FUNCTION rag_ensure_month_partition(parent text, month_start date)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE
            part_name text := format('%s_%s', parent, to_char(month_start, 'YYYYMM'));
        BEGIN
            IF to_regclass(part_name) IS NULL THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                    part_name, parent, month_start, (month_start + interval '1 month')::date
                );
            END IF;
        END $$
    """)
    op.execute("""
        SELECT rag_ensure_month_partition(t, (date_trunc('month', now()) + make_interval(months => m))::date)
        FROM unnest(ARRAY['query_logs','usage_events']) AS t, generate_series(0, 2) AS m
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS rag_ensure_month_partition(text, date)")
    for table in (
        "usage_events",
        "query_logs",
        "ingestion_jobs",
        "chunks",
        "documents",
        "knowledge_bases",
        "api_clients",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
