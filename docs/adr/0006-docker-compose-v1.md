# ADR 0006: Docker Compose as the V1 runtime

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

No cloud deployment yet; the system must still run exactly as it will later.

## Decision

Everything runs in containers built from this repo (app + parser images), orchestrated by Compose with health checks, a one-shot `bootstrap` job and a dev override with Compose Watch. PostgreSQL stays on the host.

## Alternatives considered

Running processes directly on the host (environment drift, no path to Kubernetes).

## Consequences

The same images move to Kubernetes in Phase 4. Compose services map 1:1 to Deployments/Jobs.
