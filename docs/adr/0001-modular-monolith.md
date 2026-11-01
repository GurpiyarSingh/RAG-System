# ADR 0001: Modular monolith, deployed as separate services

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

The service needs independent scaling for management, queries and ingestion, but is built by a small team.

## Decision

One codebase and one image, run in different roles (`rag-service api | query | worker index | worker parse`). Modules have clear boundaries (`api` → `domain` → `providers`/`storage`).

## Alternatives considered

Microservices per concern (more network hops, versioning and ops overhead); a single process (no isolation between upload bursts and queries).

## Consequences

Independent scaling and failure isolation with one build. A module can be extracted later if a team boundary requires it.
