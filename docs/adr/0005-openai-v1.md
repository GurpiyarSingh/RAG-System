# ADR 0005: OpenAI for V1 behind provider interfaces

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

V1 runs locally without GPUs, and an OpenAI key is available.

## Decision

`text-embedding-3-large` at 1024 dims for embeddings and `gpt-4o` for answers (`gpt-4o-mini` for utility tasks), behind `EmbeddingProvider` / `LLMProvider`. A Redis rate limiter keeps a reserve for live queries.

## Alternatives considered

Self-hosted BGE-M3 + local LLM (needs a GPU); hard-coding one SDK (lock-in).

## Consequences

Document text and queries are sent to OpenAI, which is fine for V1 test data. Switching providers is config plus a re-index.
