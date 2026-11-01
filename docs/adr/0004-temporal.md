# ADR 0004: Temporal for ingestion orchestration

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

Ingestion is multi-step (parse → chunk → embed → index), long-running and retry-heavy, and must survive worker crashes.

## Decision

Temporal workflows with per-activity timeouts and retry policies; parse and index activities on separate task queues. V1 uses the Temporal dev server container.

## Alternatives considered

Celery + RabbitMQ (state, resumability and fan-out would have to be hand-built); plain background tasks (work lost on restart).

## Consequences

Crashed workers resume from the last completed step. Activities must be idempotent and guard against stale versions.
