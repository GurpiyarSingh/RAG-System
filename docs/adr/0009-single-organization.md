# ADR 0009: Single organization, no multi-tenancy

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

The service is run by and for one organization, with many consuming applications.

## Decision

No tenants: consuming applications are API clients with scopes and optional KB allow-lists, enforced in `KnowledgeBaseRepository`. Usage and quotas are tracked per API client. Document-level user ACLs remain (Phase 3).

## Alternatives considered

Full multi-tenancy (tenant IDs on every row, Row-Level Security, per-tenant quotas): complexity with no benefit here.

## Consequences

Simpler schema and queries. Separation between apps relies on KB allow-lists, covered by an access-control test suite.
