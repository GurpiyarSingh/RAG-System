"""Model and index providers behind stable interfaces (ARCHITECTURE.md §21).

Pipelines depend only on the protocols in :mod:`rag_service.providers.base`. Swapping
OpenAI for self-hosted BGE-M3, or for Claude, is a new implementation plus a config
change. Pipeline code doesn't change.
"""
