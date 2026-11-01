# RAG-System

An enterprise-grade Retrieval-Augmented Generation (RAG) platform for a single organization, built as an independent service that any application can use through a versioned REST API.

- **[docs/LOCAL_SETUP_GUIDE.md](docs/LOCAL_SETUP_GUIDE.md): step-by-step setup for newcomers (start here)**
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): what is built and why (design, databases, decisions)
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md): phase-wise build plan, Docker setup, acceptance criteria
- [docs/adr/](docs/adr/README.md): architecture decision records

**Status:** Phase 0 (foundations, Docker stack) and Phase 1 (ingest → retrieve → answer) are implemented.

## V1 stack (local)

V1 is fully Dockerized and runs on your machine with Docker Compose. It uses OpenAI for models and your existing local PostgreSQL 18. See [§0 of the architecture](docs/ARCHITECTURE.md#0-v1-scope--local-deployment--openai).

| Component | Choice |
|---|---|
| API | Python 3.14, FastAPI, Pydantic v2 (`rag-api` on :8000, `rag-query` on :8001) |
| System of record | PostgreSQL 18.6, your local install (not a container) |
| Vector index | Qdrant 1.19 (dense vectors; hybrid search arrives in Phase 2) |
| Object storage | SeaweedFS S3 gateway (MinIO no longer ships community images, [ADR 0008](docs/adr/0008-seaweedfs-object-storage.md)) |
| Workflows | Temporal dev server, with `parse-worker` (Docling) and `index-worker` |
| Cache / rate limits | Redis 7.4 |
| Models | OpenAI `text-embedding-3-large` (1024 dims) and `gpt-4o` |

Later: Kubernetes + Helm, Terraform, Argo CD, and optionally self-hosted models or Claude through the same provider interfaces. Consumers see no API changes.

## Prerequisites

- **Docker Desktop** with the WSL2 backend. Give it at least 8 GB of memory: put `memory=8GB` (or more) under `[wsl2]` in `%UserProfile%\.wslconfig`, then run `wsl --shutdown`. The parse worker alone may use up to 2.5 GB.
- **Python 3.14** (already installed). Nothing is installed globally; everything goes into the repo-local `.venv`.
- **PostgreSQL 18** running locally (yours: service `postgresql-x64-18`, port 5432).

## One-time setup

```powershell
# 1. Virtual environment inside the repo, with uv installed into it (not globally).
python -m venv .venv
.\.venv\Scripts\python -m pip install uv
.\.venv\Scripts\uv sync          # dev dependencies; uv's cache stays in .uv-cache\

# 2. Config: .env with random secrets (skip if .env already exists).
#    Put your OpenAI key in OPENAI_API_KEY when you have it. Until then the stack runs
#    and model calls return a clear 503 "model-not-configured".
.\.venv\Scripts\python scripts\gen_env.py

# 3. Database roles and databases on your local PostgreSQL (asks for the postgres password).
.\.venv\Scripts\uv run poe db-init
```

4. **Let Docker containers reach your PostgreSQL.** Add this line to `D:\C Drive\PostgreSQL\data\pg_hba.conf`, then run `SELECT pg_reload_conf();` as `postgres` (no restart needed):

   ```
   host    rag,rag_test    rag_owner,rag_app    192.168.65.0/24    scram-sha-256
   ```

5. Verify with `.\.venv\Scripts\uv run poe db-check`.

## Run it

```powershell
.\.venv\Scripts\uv run poe up        # build + start everything (dev mode), waits until healthy
.\.venv\Scripts\uv run poe seed      # demo API key + KB + sample documents, prints curl examples
```

Ask a question (use the key and KB ID printed by `seed`):

```bash
curl -s http://localhost:8001/v1/query \
  -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  -d '{"kb_ids": ["<kb-id>"], "query": "How many days of annual leave do I get?"}'
```

Add `"stream": true` and `curl -N` for Server-Sent Events (`retrieval` → `delta`… → `done`).

| URL | What |
|---|---|
| http://localhost:8000/docs | Management API (API clients, KBs, documents, jobs), OpenAPI UI |
| http://localhost:8001/docs | Query API (`/v1/retrieve`, `/v1/query`) |
| http://localhost:8233 | Temporal UI: every ingestion workflow, step by step |
| http://localhost:6333/dashboard | Qdrant dashboard |

## Commands

| Command | Does |
|---|---|
| `uv run poe up` / `down` / `logs` | Start (dev mode) / stop / follow logs |
| `docker compose watch` | Hot reload: syncs `src/` into running containers on save |
| `uv run poe up-prod` | Prod-like start without the dev override |
| `uv run poe lint` | ruff + format check + strict mypy |
| `uv run poe unit` | Unit tests (no Docker needed) |
| `uv run poe test` | Unit + integration tests (Testcontainers: PostgreSQL 18.6, Qdrant) |
| `uv run poe e2e-up`, then `uv run poe e2e` | Full stack against a fake OpenAI server, then end-to-end tests |
| `uv run poe db-init` / `db-check` | Set up / verify the local database roles |

Prefix commands with `.\.venv\Scripts\` unless the venv is activated (`.\.venv\Scripts\Activate.ps1`).

## Using it from another application

1. With the admin key from `.env` (`RAG_BOOTSTRAP_ADMIN_KEY`), create one API client per application: `POST /v1/api-clients`. Pass `allowed_kb_ids` to restrict it to specific knowledge bases. Store the returned key; it's shown only once.
2. Create a knowledge base: `POST /v1/knowledge-bases`.
3. Upload documents: `POST /v1/knowledge-bases/{kb_id}/documents` (multipart file) or `.../documents/text` (JSON). Send your own `external_id`, so re-uploads update the document instead of duplicating it.
4. Query: `POST /v1/retrieve` for ranked chunks (bring your own LLM), or `POST /v1/query` for a cited answer.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `bootstrap` keeps printing "Waiting for database..." | Containers can't reach PostgreSQL: check the `pg_hba.conf` line and run `uv run poe db-check` |
| `/readyz` shows `openai: placeholder key` | Expected until you set `OPENAI_API_KEY` in `.env`; then run `uv run poe up` again |
| Containers OOM-killed, or Docker is very slow | Raise WSL2 memory (see prerequisites) |
| Port already in use | Another service uses 6333/6379/7233/8000/8001/8233/8333; stop it or change the host port in `compose.yaml` |
| `/bin/sh^M: bad interpreter` | A shell script was checked out with CRLF; `.gitattributes` enforces LF, so re-checkout the file |
