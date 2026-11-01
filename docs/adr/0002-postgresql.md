# ADR 0002: PostgreSQL 18 as the system of record

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

Documents, chunk text, jobs, API clients and logs need transactions, relational integrity and flexible metadata. PostgreSQL 18.6 is already installed on the dev machine.

## Decision

PostgreSQL (≥ 16; V1 uses the local 18.6 install). Two roles: `rag_owner` (DDL/migrations) and `rag_app` (DML only). Native `uuidv7()` for keys; monthly partitions for logs.

## Alternatives considered

MongoDB (no cross-table transactions or relational integrity); MySQL (weaker JSONB/partitioning story).

## Consequences

Qdrant becomes a rebuildable derived index. The runtime role can't change the schema.
