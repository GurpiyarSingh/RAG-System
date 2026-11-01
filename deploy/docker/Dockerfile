# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# rag-service image: rag-api, rag-query, index-worker, bootstrap job.
# The document-parsing stack (Docling) lives in Dockerfile.parser instead, keeping
# this image small and fast to start (IMPLEMENTATION_PLAN.md §3.1).
# ---------------------------------------------------------------------------
ARG PYTHON_IMAGE=python:3.14.9-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6

FROM ${UV_IMAGE} AS uv

# ---- builder: resolve and install dependencies with uv ----------------------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/root/.cache/uv \
    UV_HTTP_TIMEOUT=300
WORKDIR /app

# 1) Dependencies only. This layer stays cached until pyproject.toml / uv.lock change,
#    so code edits rebuild in seconds.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# 2) The project itself.
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY migrations/ migrations/
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Pre-fetch the tokenizer's BPE file so containers need no network for it at runtime.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN /app/.venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# ---- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
# tini: a proper PID 1 that forwards SIGTERM, so uvicorn drains SSE streams and
# Temporal workers finish activities on `docker stop`, and zombies get reaped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
# Fixed non-root UID, compatible with Kubernetes runAsNonRoot later.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app
COPY --from=builder --chown=app:app /opt/tiktoken /opt/tiktoken
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

USER app
EXPOSE 8000 8001
ENTRYPOINT ["tini", "--", "rag-service"]
CMD ["api"]
