# Local Setup Guide

This guide takes you from a fresh Windows machine to a running RAG service you can ask questions. No prior knowledge of the project is needed: follow the steps in order and run each check before moving on.

**Time needed:** about 45–60 minutes the first time. Most of that is waiting for downloads.

---

## Contents

1. [What you're going to run](#1-what-youre-going-to-run)
2. [Prerequisites](#2-prerequisites)
3. [Check your machine](#3-check-your-machine)
4. [Set up the project](#4-set-up-the-project)
5. [Set up the database](#5-set-up-the-database)
6. [Start the service](#6-start-the-service)
7. [Check that it's working](#7-check-that-its-working)
8. [Use it: your first question](#8-use-it-your-first-question)
9. [Add your OpenAI key](#9-add-your-openai-key)
10. [Run the tests](#10-run-the-tests)
11. [Everyday commands](#11-everyday-commands)
12. [Stop, reset and clean up](#12-stop-reset-and-clean-up)
13. [Troubleshooting](#13-troubleshooting)
14. [Glossary](#14-glossary)

> **Conventions.** Commands are for **PowerShell**, run from the project folder (`D:\D Drive\Codes\RAG-System`). Lines starting with `#` are comments; you don't type them. In Windows PowerShell, `curl` is a different command, so always type **`curl.exe`**.

---

## 1. What you're going to run

The RAG service answers questions using your own documents. You upload documents, and it splits them into small pieces ("chunks") and stores them in a searchable index. When you ask a question, it finds the most relevant pieces and has an AI model write an answer that cites them.

Everything runs on your computer, in **Docker containers**. Think of them as small, isolated mini-computers, each running one program:

```
                 your app / curl
                   │        │
          :8000 ───┘        └─── :8001
    ┌──────────────┐    ┌──────────────┐
    │   rag-api    │    │  rag-query   │   ← the service's two HTTP APIs
    │ upload, KBs  │    │ ask questions│
    └──────┬───────┘    └──────┬───────┘
           │ starts jobs       │ searches
    ┌──────▼───────┐    ┌──────▼───────┐    ┌───────────┐
    │   Temporal   │───▶│ index-worker │───▶│  Qdrant   │  ← search index
    │ (job engine) │    │ parse-worker │    └───────────┘
    └──────────────┘    └──────┬───────┘    ┌───────────┐
                               ├───────────▶│ SeaweedFS │  ← stores the files
                               │            └───────────┘
                               │            ┌───────────┐
                               └───────────▶│ PostgreSQL│  ← your local install,
                                            └───────────┘    NOT a container
                     OpenAI API (internet): embeddings + answers
```

| Program | What it does |
|---|---|
| `rag-api` (port 8000) | Manage API keys, knowledge bases (KBs) and documents |
| `rag-query` (port 8001) | Search (`/v1/retrieve`) and ask questions (`/v1/query`) |
| `parse-worker` | Reads PDFs, Word files etc. and extracts their text |
| `index-worker` | Splits text into chunks, gets embeddings from OpenAI, stores them in Qdrant |
| Temporal (UI on 8233) | Runs ingestion as reliable step-by-step jobs |
| Qdrant (6333) | The vector search index |
| SeaweedFS (8333) | Stores the original files (S3-compatible) |
| Redis (6379) | Cache and rate limiting |
| PostgreSQL (5432) | The main database. It's the PostgreSQL already installed on your machine |

---

## 2. Prerequisites

Install these first. For each one, the **"Check"** column shows a command that proves it works.

| # | What | Why | How to install | Check (expected result) |
|---|---|---|---|---|
| 1 | **Windows 10/11** with virtualization enabled | Docker needs it | Usually on already; otherwise enable "Virtualization" in the BIOS | Task Manager → Performance → CPU shows *Virtualization: Enabled* |
| 2 | **WSL 2** | Docker Desktop runs Linux containers through it | `wsl --install` in an admin PowerShell, then restart | `wsl --status` shows *Default Version: 2* |
| 3 | **Docker Desktop** (WSL 2 backend) | Runs all the containers | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) | `docker version` shows both *Client* and *Server*; `docker compose version` prints v2 or newer |
| 4 | **8 GB of memory for Docker** | The stack needs ~5 GB; document parsing alone may use 2.5 GB | See [§2.1](#21-give-docker-enough-memory) | `docker info --format "{{.MemTotal}}"` prints at least `6000000000` |
| 5 | **Python 3.14** | Runs the tools and tests | [python.org/downloads](https://www.python.org/downloads/); tick *Add python.exe to PATH* | `python --version` → `Python 3.14.x` |
| 6 | **PostgreSQL 18** (running) | The main database | [postgresql.org/download/windows](https://www.postgresql.org/download/windows/); remember the `postgres` password you choose | `Get-Service postgresql*` shows *Running* |
| 7 | **Git** | Gets the code | [git-scm.com](https://git-scm.com/download/win) | `git --version` |
| 8 | **~20 GB free disk space** | Container images | – | Script below checks it |
| 9 | **Internet access** | Downloads packages/images; calls OpenAI | – | Script below checks it |
| 10 | **OpenAI API key** *(optional at first)* | Real answers | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | You can run everything except real answers without it ([§9](#9-add-your-openai-key)) |

### 2.1 Give Docker enough memory

Docker Desktop on WSL 2 uses only part of your RAM by default (often too little for this project).

1. Open (or create) the file `C:\Users\<you>\.wslconfig` in Notepad:
   ```powershell
   notepad "$env:USERPROFILE\.wslconfig"
   ```
2. Put this in it (use about half of your RAM; `8GB` is the recommended minimum):
   ```ini
   [wsl2]
   memory=8GB
   ```
3. Save, then restart WSL and Docker Desktop:
   ```powershell
   wsl --shutdown
   ```
   Start Docker Desktop again and wait until it shows **Engine running**.
4. Check:
   ```powershell
   docker info --format "{{.MemTotal}}"     # 8 GB ≈ 8300000000
   ```

> Not sure how much RAM you have? Task Manager → Performance → Memory. With 8 GB total, set `memory=6GB`. With 16 GB, `8GB`–`10GB` is comfortable.

---

## 3. Check your machine

The project includes a script that checks everything from §2 in one go. It needs nothing installed except Python:

```powershell
cd "D:\D Drive\Codes\RAG-System"
python scripts\check_prereqs.py
```

Example of a healthy result:

```
[PASS] Python: 3.14.6
[PASS] Docker engine: version 29.8.2
[PASS] Docker Compose: version 5.6.0
[PASS] Docker memory: 8.0 GB
[PASS] Free ports: 6333, 6379, 7233, 8000, 8001, 8233, 8333
[PASS] PostgreSQL client: psql (PostgreSQL) 18.6
[PASS] PostgreSQL server: listening on 127.0.0.1:5432
[PASS] Disk space: 900 GB free
[PASS] Internet: PyPI, Docker Hub and OpenAI reachable
[WARN] .env: not created yet
        -> Run: python scripts/gen_env.py
```

- **FAIL** must be fixed before you continue; the line below it says how.
- **WARN** is fine for now. The `.env` and virtual-environment warnings disappear as you follow §4.

Run the script again at any time; it's safe.

---

## 4. Set up the project

### 4.1 Get the code

```powershell
cd "D:\D Drive\Codes"
git clone <repository-url> RAG-System     # skip if you already have the folder
cd RAG-System
```

### 4.2 Create the virtual environment

A **virtual environment** (`.venv`) is a private folder of Python packages for this project only. Nothing gets installed into your global Python, so other projects aren't affected.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip uv
.\.venv\Scripts\uv sync
```

- `uv` is a fast package manager. It's installed **inside** `.venv`, not globally.
- `uv sync` installs the exact versions recorded in `uv.lock`. Its download cache stays in the project folder (`.uv-cache\`).
- It takes a few minutes the first time.

**Check:**

```powershell
.\.venv\Scripts\python -c "import rag_service; print('ok', rag_service.__version__)"
# → ok 0.1.0
```

> **Tip:** run `.\.venv\Scripts\Activate.ps1` to *activate* the venv. Then you can type `uv`, `python`, `pytest` without the `.\.venv\Scripts\` prefix. If PowerShell blocks the script, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. The rest of this guide assumes the venv is **activated**.

### 4.3 Create your configuration (`.env`)

All settings live in a file named `.env`. It contains passwords, so it's **never committed to Git** (`.gitignore` excludes it).

```powershell
python scripts\gen_env.py
```

This copies `.env.example` to `.env` and fills in random passwords and keys. Open it with `notepad .env` and look at these lines:

| Setting | Meaning | Change it? |
|---|---|---|
| `OPENAI_API_KEY` | Your OpenAI key; a placeholder at first | Later ([§9](#9-add-your-openai-key)) |
| `RAG_LLM_MODEL=gpt-4o` | Model that writes the answers | Optional |
| `RAG_DB_OWNER_PASSWORD`, `RAG_DB_APP_PASSWORD` | Passwords for the two database users created in §5 | No: generated for you |
| `RAG_PG_BIN` | Folder containing `psql.exe` | Only if PostgreSQL is installed somewhere else |
| `RAG_BOOTSTRAP_ADMIN_KEY` | Admin key, used to create the first API keys | No, but you'll copy it in §8 |

---

## 5. Set up the database

The service uses your local PostgreSQL. It needs its own **databases** (`rag`, `rag_test`) and two **users**:
- `rag_owner` owns the tables and runs schema changes ("migrations");
- `rag_app` is what the running service uses. It can read and write data, but it can't change or delete tables, which limits the damage a bug could do.

Your other databases aren't touched.

### 5.1 Create the users and databases

```powershell
uv run poe db-init
```

It asks for the **`postgres` password** (the one you chose when installing PostgreSQL). It's safe to run again later; it updates the passwords to match `.env`.

### 5.2 Allow Docker containers to connect

PostgreSQL only accepts connections from places listed in its `pg_hba.conf` file. Containers connect from Docker's private network (`192.168.65.0/24`), so it has to be allowed:

1. Open the file **as administrator** (right-click Notepad → *Run as administrator*):
   `D:\C Drive\PostgreSQL\data\pg_hba.conf` (or `<your PostgreSQL folder>\data\pg_hba.conf`)
2. Add this line at the end and save:
   ```
   host    rag,rag_test    rag_owner,rag_app    192.168.65.0/24    scram-sha-256
   ```
3. Tell PostgreSQL to reload its config (no restart needed):
   ```powershell
   & "D:\C Drive\PostgreSQL\bin\psql.exe" -U postgres -h 127.0.0.1 -c "SELECT pg_reload_conf();"
   ```

> If Docker Desktop uses a different network (Settings → Resources → Network), use that subnet instead of `192.168.65.0/24`.

### 5.3 Check

```powershell
uv run poe db-check
```

Expected:

```
[ OK ] rag_app (runtime): connected as rag_app to PostgreSQL 18.6
[ OK ] rag_owner (migrations): connected as rag_owner to PostgreSQL 18.6
```

---

## 6. Start the service

```powershell
uv run poe up
```

What happens:
1. **Builds the images** (first time only, **15–30 minutes**). The document parser image is large (~3 GB) because it contains PyTorch and the layout/OCR models. Later builds take seconds.
2. **Downloads** Redis, Qdrant, SeaweedFS and Temporal (once).
3. Runs the one-time **`bootstrap`** job: creates the database tables, the file bucket and the search index.
4. Starts everything and **waits until all containers are healthy**.

When the command returns without errors, the service is up.

---

## 7. Check that it's working

### 7.1 Container status

```powershell
docker compose ps
```

You should see `rag-api`, `rag-query`, `index-worker`, `parse-worker`, `qdrant`, `redis`, `seaweedfs` and `temporal` as **running**/**healthy**. `bootstrap` doesn't appear, because it exits after finishing (`docker compose ps -a` shows it as *Exited (0)*).

### 7.2 Health endpoints

```powershell
Invoke-RestMethod http://localhost:8000/readyz | ConvertTo-Json
```

Expected: `"status": "ok"`, and every check `"ok"`. Until you add an OpenAI key you'll also see `"openai": "placeholder key (set OPENAI_API_KEY)"`. That's normal.

### 7.3 Web pages

| Open in a browser | You should see |
|---|---|
| http://localhost:8000/docs | Interactive API documentation (management API) |
| http://localhost:8001/docs | Interactive API documentation (query API) |
| http://localhost:8233 | Temporal UI: one entry per document ingestion |
| http://localhost:6333/dashboard | Qdrant dashboard: the `chunks__…` collection |

---

## 8. Use it: your first question

> Real answers need an OpenAI key ([§9](#9-add-your-openai-key)). Without one, uploads are accepted but indexing fails at the embedding step with *ModelNotConfigured*. You can still run the full test suite without a key ([§10](#10-run-the-tests)).

### 8.1 The quick way: demo script

```powershell
uv run poe seed
```

It creates an API key for a demo application and a knowledge base. It uploads the two sample documents in `examples\sample_docs\`, waits for them, and prints ready-to-paste commands.

### 8.2 Step by step (to understand the API)

**Key ideas:**
- **API key**: each application that uses the service gets its own key.
- **Knowledge base (KB)**: a named collection of documents, for example "HR policies".
- **Document**: a file or text you upload into a KB.

```powershell
# 0. Read the admin key from .env
$admin = (Select-String -Path .env -Pattern '^RAG_BOOTSTRAP_ADMIN_KEY=(.*)').Matches[0].Groups[1].Value
$H = @{ Authorization = "Bearer $admin" }

# 1. Create an API key for your application (shown ONCE: keep it)
$client = Invoke-RestMethod -Method Post http://localhost:8000/v1/api-clients -Headers $H `
  -ContentType "application/json" -Body '{"name": "my-first-app"}'
$K = @{ Authorization = "Bearer $($client.api_key)" }

# 2. Create a knowledge base
$kb = Invoke-RestMethod -Method Post http://localhost:8000/v1/knowledge-bases -Headers $K `
  -ContentType "application/json" -Body '{"name": "hr-policies"}'

# 3. Upload a document (curl.exe, because it handles file uploads easily)
curl.exe -s -X POST "http://localhost:8000/v1/knowledge-bases/$($kb.id)/documents" `
  -H "Authorization: Bearer $($client.api_key)" `
  -F "file=@examples/sample_docs/employee-handbook.md" -F "external_id=handbook"

# 4. Watch it get processed: pending → parsing → indexing → ready
Invoke-RestMethod "http://localhost:8000/v1/knowledge-bases/$($kb.id)/documents" -Headers $K |
  Select-Object -ExpandProperty items | Select-Object title, status, chunk_count

# 5a. Search only (returns the most relevant chunks)
Invoke-RestMethod -Method Post http://localhost:8001/v1/retrieve -Headers $K -ContentType "application/json" `
  -Body (@{ kb_ids = @($kb.id); query = "How many days of annual leave?" } | ConvertTo-Json)

# 5b. Ask a question (answer + citations)
$answer = Invoke-RestMethod -Method Post http://localhost:8001/v1/query -Headers $K -ContentType "application/json" `
  -Body (@{ kb_ids = @($kb.id); query = "How many days of annual leave do I get?" } | ConvertTo-Json)
$answer.answer
$answer.citations | Select-Object id, title, snippet
```

**Streaming answers** (text appears word by word, as in chat apps):

```powershell
curl.exe -N -X POST http://localhost:8001/v1/query `
  -H "Authorization: Bearer $($client.api_key)" -H "Content-Type: application/json" `
  -d "{\`"kb_ids\`": [\`"$($kb.id)\`"], \`"query\`": \`"What are the expense limits?\`", \`"stream\`": true}"
```

You'll see `event: retrieval` (the sources), many `event: delta` lines (pieces of the answer), then `event: done`.

**Tip:** everything above can also be clicked through at http://localhost:8000/docs and http://localhost:8001/docs. Use the **Authorize** button, paste `Bearer <your key>`, then use *Try it out*.

---

## 9. Add your OpenAI key

1. `notepad .env`, then replace the placeholder:
   ```dotenv
   OPENAI_API_KEY=sk-...your real key...
   ```
2. Restart so the containers pick it up:
   ```powershell
   uv run poe up
   ```
3. Check: `Invoke-RestMethod http://localhost:8000/readyz` no longer mentions a placeholder.
4. Documents that failed earlier because there was no key: upload them again with the same `external_id`. Failed documents are re-processed.

> **Cost:** embedding the sample documents costs a fraction of a cent. Every question with `gpt-4o` costs roughly $0.01–0.03, depending on how much context is sent.
>
> **Privacy:** document text and questions are sent to OpenAI. Use test or non-confidential documents in V1.

---

## 10. Run the tests

None of these need an OpenAI key.

| Command | What it tests | Needs |
|---|---|---|
| `uv run poe unit` | Chunking, parsing, citations, security, rate limiter, OpenAI client handling (against a fake server) | Nothing |
| `uv run poe test` | The above + integration tests against real PostgreSQL 18.6 and Qdrant, in throwaway containers | Docker |
| `uv run poe lint` | Code style and strict type checks | Nothing |
| `uv run poe e2e-up`, then `uv run poe e2e` | **Full system**: upload → index → search → answer → delete, against a fake OpenAI server | Docker, §5 done |

Expected: everything **passed**. Integration tests take a few minutes the first time (they download PostgreSQL and Qdrant images).

> After `e2e`, run `uv run poe up` to switch back to normal mode (real OpenAI).

---

## 11. Everyday commands

| I want to… | Command |
|---|---|
| Start everything | `uv run poe up` |
| Stop everything (keeps data) | `uv run poe down` |
| See logs of all services | `uv run poe logs` (Ctrl+C to stop watching) |
| See logs of one service | `docker compose logs -f rag-api` |
| Restart one service | `docker compose restart index-worker` |
| Hot reload while coding | `docker compose watch` (syncs `src\` into the containers on save) |
| See why a document failed | Temporal UI http://localhost:8233 → open the `ingest-…` workflow, or `GET /v1/jobs/{job_id}` |
| Check the machine again | `python scripts\check_prereqs.py` |

---

## 12. Stop, reset and clean up

| Goal | Command | What's lost |
|---|---|---|
| Stop | `uv run poe down` | Nothing |
| Wipe search index, files and job history | `docker compose down -v` | Everything in Qdrant, SeaweedFS, Temporal |
| Also remove the built images | `docker compose down -v --rmi local` | Images get rebuilt on the next `up` (15–30 min) |
| Wipe the database tables too | `& "D:\C Drive\PostgreSQL\bin\psql.exe" -U postgres -h 127.0.0.1 -c "DROP DATABASE rag;" -c "DROP DATABASE rag_test;"`, then `uv run poe db-init` | All documents/KBs/keys metadata |
| Free Docker disk space | `docker system prune` (add `-a` to also remove unused images) | Unused containers/images/build cache |
| Remove the Python environment | Delete the `.venv` and `.uv-cache` folders | Re-create with §4.2 |

> Wipe the database and the Docker volumes **together**. The database records which documents exist; the volumes hold their files and vectors.

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `check_prereqs.py`: **Docker memory FAIL** | WSL 2 memory too low | [§2.1](#21-give-docker-enough-memory) |
| `docker: error during connect` / `Cannot connect to the Docker daemon` | Docker Desktop isn't running | Start Docker Desktop, wait for *Engine running* |
| `db-init`: `password authentication failed for user "postgres"` | Wrong `postgres` password | Use the password from the PostgreSQL install. Forgot it? Search "reset postgres password windows" |
| `db-init`: `psql not found` | PostgreSQL is in another folder | Set `RAG_PG_BIN=<your PostgreSQL>\bin` in `.env` |
| `up` stops at **bootstrap**, logs show `Waiting for database...` | Containers can't reach PostgreSQL | Check the `pg_hba.conf` line and reload (§5.2); run `uv run poe db-check` |
| Build fails with `Failed to fetch` / `timed out` | Unstable internet | Run `uv run poe up` again; finished layers are cached |
| `parse-worker` keeps restarting, or `Exited (137)` | Out of memory | Give Docker more memory (§2.1) |
| Document status `failed`, error `ModelNotConfigured` | No OpenAI key yet | §9, then upload again |
| Document status `failed`, error `EmptyDocument` | File has no extractable text (e.g. blank or image-only and OCR found nothing) | Check the file |
| `415 Unsupported Media Type` on upload | File type not supported | Supported: PDF, DOCX, PPTX, XLSX, HTML, MD, TXT, CSV, PNG, JPG, TIFF |
| `401 Unauthorized` | Missing or wrong key | Header must be `Authorization: Bearer <key>` |
| `404` for a KB that exists | The API key is restricted to other KBs (`allowed_kb_ids`) | Use a key that is allowed to access that KB |
| `port is already allocated` | Another program uses a port | `netstat -ano \| findstr :8000` to find it; stop it or change the port in `compose.yaml` |
| `/bin/sh^M: bad interpreter` | A script got Windows line endings | Restore that file from Git with LF endings: delete it, then `git checkout -- <path-to-file>` (`.gitattributes` enforces LF) |
| `curl` behaves strangely in PowerShell | `curl` is an alias for `Invoke-WebRequest` | Type `curl.exe` |

Still stuck? Collect `docker compose ps -a` and `docker compose logs --tail=200 <service>` and ask the team.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **RAG** | Retrieval-Augmented Generation: find relevant text first, then let an AI answer from it |
| **Chunk** | A small piece of a document (~512 tokens) that can be searched on its own |
| **Embedding** | A list of numbers representing the meaning of a text; similar texts get similar numbers |
| **Vector index (Qdrant)** | A database that finds the embeddings closest to a question's embedding |
| **Knowledge base (KB)** | A named group of documents you search together |
| **API client / API key** | The credential one consuming application uses; can be limited to certain KBs |
| **Citation `[1]`** | Marks which source a sentence of the answer came from |
| **Container / image** | A packaged program (image) and a running instance of it (container) |
| **Docker Compose** | Starts and connects all the containers described in `compose.yaml` |
| **Temporal workflow** | A reliable multi-step job (parse → chunk → embed → index) that resumes after crashes |
| **Migration** | A versioned change to the database tables, applied by the `bootstrap` job |
| **`.venv`** | The project's private Python environment |
