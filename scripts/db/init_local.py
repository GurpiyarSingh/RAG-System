"""Apply scripts/db/init_local.sql to the local PostgreSQL (``uv run poe db-init``).

Reads the role passwords from .env (``RAG_DB_OWNER_PASSWORD``, ``RAG_DB_APP_PASSWORD``),
finds ``psql`` via ``RAG_PG_BIN`` (or PATH), and runs the script as the ``postgres``
superuser. psql prompts for the superuser password itself, so that password is
never stored anywhere by this project.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "scripts" / "db" / "init_local.sql"


def read_env(path: Path) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE lines, # comments, optional quotes)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def find_psql(env: dict[str, str]) -> str:
    pg_bin = os.environ.get("RAG_PG_BIN") or env.get("RAG_PG_BIN")
    if pg_bin:
        candidate = Path(pg_bin) / ("psql.exe" if os.name == "nt" else "psql")
        if candidate.exists():
            return str(candidate)
    found = shutil.which("psql")
    if not found:
        sys.exit("psql not found. Set RAG_PG_BIN in .env to PostgreSQL's bin directory.")
    return found


def main() -> None:
    env = read_env(ROOT / ".env")
    owner_pw = env.get("RAG_DB_OWNER_PASSWORD")
    app_pw = env.get("RAG_DB_APP_PASSWORD")
    if not owner_pw or not app_pw:
        sys.exit("RAG_DB_OWNER_PASSWORD and RAG_DB_APP_PASSWORD must be set in .env.")
    host = env.get("RAG_DB_HOST", "127.0.0.1")
    port = env.get("RAG_DB_PORT", "5432")
    superuser = os.environ.get("PGUSER", "postgres")

    print(
        f"Applying {SQL.name} to {host}:{port} as '{superuser}' (psql will ask for its password)."
    )
    cmd = [
        find_psql(env),
        "-h", host,
        "-p", port,
        "-U", superuser,
        "-d", "postgres",
        "-v", f"owner_pw={owner_pw}",
        "-v", f"app_pw={app_pw}",
        "-f", str(SQL),
    ]  # fmt: skip
    # The passwords are passed as argv, never through a shell, so no quoting issues.
    result = subprocess.run(cmd, check=False)  # noqa: S603
    if result.returncode != 0:
        sys.exit(result.returncode)

    print(
        "\nDone. If containers can't connect, add this line to pg_hba.conf and reload\n"
        "(SELECT pg_reload_conf(); as postgres):\n\n"
        "  host    rag,rag_test    rag_owner,rag_app    192.168.65.0/24    scram-sha-256\n\n"
        "Then verify with: uv run poe db-check"
    )


if __name__ == "__main__":
    main()
