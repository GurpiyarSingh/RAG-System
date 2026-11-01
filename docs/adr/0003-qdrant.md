# ADR 0003: Qdrant as the vector index

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

Filtered vector search per knowledge base, later hybrid (dense + sparse) search, tens of millions of vectors.

## Decision

Qdrant, one versioned collection per embedding model behind the alias `chunks_active`; all KBs share it with a partitioned (`is_tenant`) `kb_id` index; int8 quantization.

## Alternatives considered

pgvector (kept as a future alternative for small installs), Milvus (heavy ops), Weaviate, Pinecone (SaaS only), OpenSearch (JVM cluster for keyword search Qdrant already covers).

## Consequences

Model changes are blue/green re-indexes plus an alias swap. Searches always carry a KB filter.
