"""Application settings loaded from environment variables (12-factor style).

All variables use the ``RAG_`` prefix, except ``OPENAI_API_KEY`` which keeps the name
the OpenAI tooling ecosystem expects. Values come from the process environment first,
then from a local ``.env`` file (git-ignored). Containers get infrastructure hostnames
from ``compose.yaml``; host-side tools (tests, scripts) use the ``.env`` defaults that
point at ``localhost``.

Secrets are typed as ``SecretStr`` so they are masked in ``repr()`` and never end up
in logs by accident.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder shipped in .env.example. The service starts with it (so the stack can run
# before a key is added) but every OpenAI call fails fast with a clear error.
OPENAI_KEY_PLACEHOLDER = "sk-your-openai-key-here"


class Settings(BaseSettings):
    """Typed view over every configuration knob of the service."""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ general
    env: Literal["local", "test", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"
    # "json" for machines (default, used in containers), "console" for humans in dev.
    log_format: Literal["json", "console"] = "json"

    # ------------------------------------------------------------------ PostgreSQL
    # Two roles (see scripts/db/init_local.sql):
    #   rag_owner - owns the schema, runs migrations;
    #   rag_app   - runtime role, non-owner so Row-Level Security applies to it.
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "rag"
    db_app_user: str = "rag_app"
    db_app_password: SecretStr = SecretStr("")
    db_owner_user: str = "rag_owner"
    db_owner_password: SecretStr = SecretStr("")
    # Kept small on purpose: the local Postgres (max_connections=100) is shared with
    # other databases on the machine. See ARCHITECTURE.md §0.8.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 30_000

    # ------------------------------------------------------------------ Redis
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ------------------------------------------------------------------ Qdrant
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr | None = None
    # Searches always go through the alias, so a re-index can swap collections
    # atomically (blue/green, ARCHITECTURE.md §5.2).
    qdrant_alias: str = "chunks_active"
    qdrant_shards: int = 1

    # ------------------------------------------------------------------ Object storage (S3 / MinIO)
    s3_endpoint_url: str | None = "http://127.0.0.1:9000"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_bucket: str = "rag-local-documents"

    # ------------------------------------------------------------------ Temporal
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"

    # ------------------------------------------------------------------ OpenAI
    openai_api_key: SecretStr = Field(
        default=SecretStr(OPENAI_KEY_PLACEHOLDER),
        validation_alias=AliasChoices("OPENAI_API_KEY", "RAG_OPENAI_API_KEY"),
    )
    # Overridable so e2e tests can point at the fake OpenAI server (tests/e2e/fake_openai).
    openai_base_url: str | None = None
    openai_timeout_s: float = 60.0
    # Account limits for the key; the client-side limiter keeps usage below them.
    openai_rpm_limit: int = 500
    openai_tpm_limit: int = 200_000
    # Share of the budget that background ingestion may NOT use, so live queries
    # always have headroom (IMPLEMENTATION_PLAN.md P1.8).
    openai_query_reserve_ratio: float = 0.3

    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024
    embedding_batch_size: int = 100

    llm_model: str = "gpt-4o"
    llm_utility_model: str = "gpt-4o-mini"
    # Only sent for reasoning models (gpt-5*, o-series). gpt-4o rejects the parameter.
    llm_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    llm_temperature: float = 0.1
    llm_max_output_tokens: int = 1024

    # ------------------------------------------------------------------ Security
    # Server-side secret mixed into API-key hashes. Rotating it invalidates all keys.
    api_key_pepper: SecretStr = SecretStr("")
    # Optional key with the "admin" scope, accepted without a database row. Lets you
    # create the first API clients and KBs on a fresh install.
    bootstrap_admin_key: SecretStr | None = None

    # ------------------------------------------------------------------ Ingestion
    max_upload_bytes: int = 100 * 1024 * 1024
    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64

    # ------------------------------------------------------------------ Retrieval / generation
    retrieval_top_k: int = 8
    context_token_budget: int = 6000

    # ------------------------------------------------------------------ derived values
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Runtime DSN (rag_app role) for SQLAlchemy + asyncpg."""
        return self._dsn(self.db_app_user, self.db_app_password)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migration_database_url(self) -> str:
        """DSN for Alembic (rag_owner role, which owns the tables)."""
        return self._dsn(self.db_owner_user, self.db_owner_password)

    @property
    def openai_key_configured(self) -> bool:
        """False while .env still contains the placeholder key."""
        key = self.openai_api_key.get_secret_value()
        return bool(key) and key != OPENAI_KEY_PLACEHOLDER

    def _dsn(self, user: str, password: SecretStr) -> str:
        # Passwords may contain URL-special characters, so they are percent-encoded.
        pw = quote(password.get_secret_value(), safe="")
        return f"postgresql+asyncpg://{user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (cached; tests call ``get_settings.cache_clear()``)."""
    return Settings()
