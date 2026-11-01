"""Command-line entrypoint: ``rag-service <command>``.

One image, several roles. The container's command picks what the process does:

    rag-service api          management + ingestion API  (port 8000)
    rag-service query        query API                   (port 8001)
    rag-service worker index Temporal worker: workflows + index activities
    rag-service worker parse Temporal worker: document parsing (rag-parser image)
    rag-service migrate      apply database migrations, then exit
    rag-service db-check     verify database connectivity and role safety
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from rag_service import __version__

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Enterprise RAG service")


def _serve(factory: str, port: int, reload: bool, workers: int) -> None:
    uvicorn.run(
        factory,
        factory=True,
        host="0.0.0.0",  # noqa: S104  - inside a container; Compose binds host ports to 127.0.0.1
        port=port,
        reload=reload,
        reload_dirs=["src"] if reload else None,
        workers=None if reload else workers,
        # Our middleware writes structured access logs with request IDs.
        access_log=False,
        log_config=None,
        proxy_headers=True,
        # Give in-flight SSE streams time to finish on shutdown (docker stop → SIGTERM).
        timeout_graceful_shutdown=20,
    )


@app.command()
def api(
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Hot reload (dev only)")] = False,
    workers: int = 1,
) -> None:
    """Run the management + ingestion API."""
    _serve("rag_service.api.app:create_api_app", port, reload, workers)


@app.command()
def query(
    port: int = 8001,
    reload: Annotated[bool, typer.Option(help="Hot reload (dev only)")] = False,
    workers: int = 1,
) -> None:
    """Run the query API (/v1/retrieve, /v1/query)."""
    _serve("rag_service.api.app:create_query_app", port, reload, workers)


@app.command()
def worker(queue: Annotated[str, typer.Argument(help="index | parse")] = "index") -> None:
    """Run a Temporal worker for one task queue."""
    if queue not in ("index", "parse"):
        raise typer.BadParameter("queue must be 'index' or 'parse'")
    from rag_service.workers.main import run_worker

    asyncio.run(run_worker(queue))  # type: ignore[arg-type]


@app.command()
def migrate(
    revision: str = "head",
    wait_s: Annotated[int, typer.Option(help="How long to wait for the database")] = 60,
) -> None:
    """Apply Alembic migrations (as the schema-owner role), then exit."""
    from alembic import command
    from alembic.config import Config

    from rag_service.core.config import get_settings

    settings = get_settings()
    _wait_for_db(settings.migration_database_url, wait_s)
    config = Config(str(_project_root() / "alembic.ini"))
    command.upgrade(config, revision)
    typer.echo(f"Database migrated to {revision}.")


@app.command()
def bootstrap(
    wait_s: Annotated[int, typer.Option(help="How long to wait for dependencies")] = 90,
) -> None:
    """One-shot setup run before the services start (the Compose ``bootstrap`` job).

    1. Apply database migrations (as the schema-owner role).
    2. Create the object-storage bucket.
    3. Create the Qdrant collection and its alias.

    Every step is idempotent, so running it on every ``docker compose up`` is safe.
    """
    migrate(revision="head", wait_s=wait_s)
    asyncio.run(_bootstrap_storage(wait_s))


async def _bootstrap_storage(wait_s: int) -> None:
    from rag_service.core.config import get_settings
    from rag_service.resources import build_resources

    settings = get_settings()
    resources = build_resources(settings, app_name="rag-bootstrap")
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                created = await resources.object_store.ensure_bucket()
                typer.echo(f"Bucket '{settings.s3_bucket}' {'created' if created else 'exists'}.")
                alias = await resources.vector_store.ensure_collection(
                    alias=settings.qdrant_alias,
                    dimension=resources.embedder.dimension,
                    model_slug=resources.embedding_slug,
                )
                typer.echo(f"Qdrant alias '{alias}' ready.")
                return
            except Exception as exc:
                # Storage containers may still be starting up.
                if time.monotonic() > deadline:
                    raise
                typer.echo(f"Waiting for storage ({type(exc).__name__}: {exc})...")
                await asyncio.sleep(2)
    finally:
        await resources.close()


@app.command("db-check")
def db_check() -> None:
    """Connect as both roles and verify the runtime role can't change the schema."""
    from rag_service.core.config import get_settings

    settings = get_settings()
    ok = asyncio.run(_db_check(settings.database_url, settings.migration_database_url))
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def version() -> None:
    typer.echo(__version__)


# --------------------------------------------------------------------------- helpers
def _project_root() -> Path:
    """Directory containing alembic.ini (repo root locally, /app in containers)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").exists():
            return parent
    return Path.cwd()


def _wait_for_db(url: str, wait_s: int) -> None:
    """Retry connecting until PostgreSQL accepts connections.

    The host PostgreSQL isn't a Compose service, so Compose can't health-check it. The
    migrate job waits for it itself.
    """
    import asyncpg

    dsn = url.replace("postgresql+asyncpg://", "postgresql://")

    async def probe() -> None:
        conn = await asyncpg.connect(dsn, timeout=5)
        await conn.close()

    deadline = time.monotonic() + wait_s
    while True:
        try:
            asyncio.run(probe())
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                raise typer.Exit(code=1) from exc
            typer.echo(f"Waiting for database ({type(exc).__name__}: {exc})...")
            time.sleep(2)


async def _db_check(app_url: str, owner_url: str) -> bool:
    import asyncpg

    ok = True
    for label, url in (("rag_app (runtime)", app_url), ("rag_owner (migrations)", owner_url)):
        try:
            conn = await asyncpg.connect(
                url.replace("postgresql+asyncpg://", "postgresql://"), timeout=5
            )
        except Exception as exc:
            typer.echo(f"[FAIL] {label}: cannot connect: {exc}")
            ok = False
            continue
        try:
            version = await conn.fetchval("SHOW server_version")
            role = await conn.fetchrow(
                "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles"
                " WHERE rolname = current_user"
            )
            typer.echo(f"[ OK ] {label}: connected as {role['rolname']} to PostgreSQL {version}")
            if label.startswith("rag_app") and (role["rolsuper"] or role["rolcreaterole"]):
                typer.echo("[FAIL] rag_app must not be a superuser or able to create roles")
                ok = False
            if label.startswith("rag_app"):
                # Least privilege: the runtime role must not own (and so can't alter) tables.
                owned = await conn.fetchval(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                    " AND tableowner = current_user"
                )
                if owned:
                    typer.echo(
                        f"[FAIL] rag_app owns {owned} table(s); tables must belong to rag_owner"
                    )
                    ok = False
            await conn.fetchval("SELECT uuidv7()")  # PostgreSQL 18+ is required
        except asyncpg.UndefinedFunctionError:
            typer.echo(f"[FAIL] {label}: uuidv7() missing; PostgreSQL 18 or newer is required")
            ok = False
        finally:
            await conn.close()
    return ok


if __name__ == "__main__":
    app()
