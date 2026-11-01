# ADR 0007: Python 3.14, FastAPI, uv

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

AI/ML ecosystem fit, async I/O, typed contracts; the dev machine has Python 3.14 and global packages must not be touched.

## Decision

Python 3.14 (host and images), FastAPI + Pydantic v2, SQLAlchemy 2 async, uv inside the repo-local `.venv` (repo-local cache, no Python downloads), ruff, strict mypy, pytest.

## Alternatives considered

Go/Java (weaker ML ecosystem); Poetry/pip (slower, no unified tool).

## Consequences

Nothing is installed outside the repo. Python 3.14's stdlib `uuid.uuid7()` removes a dependency.
