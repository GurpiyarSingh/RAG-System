# ADR 0008: SeaweedFS for local S3-compatible storage

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

The plan named MinIO, but MinIO no longer publishes community container images (Docker Hub and quay.io tags are gone).

## Decision

SeaweedFS (Apache-2.0) with its S3 gateway for local and CI use. The app only uses the S3 API through `ObjectStore`, so production can use AWS S3/GCS/Azure unchanged.

## Alternatives considered

Building MinIO from source (maintenance burden); LocalStack (heavier, emulation-focused); storing files in PostgreSQL (bloats DB and backups).

## Consequences

One more container with a tiny start script that generates S3 credentials from `.env`.
