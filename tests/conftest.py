"""Shared test fixtures.

Unit tests never touch the network or the developer's .env: they build ``Settings``
explicitly and use :class:`WordTokenizer` (whitespace tokens) instead of tiktoken,
whose encoding file would otherwise be downloaded on first use.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from rag_service.core.config import Settings


class WordTokenizer:
    """Deterministic stand-in for tiktoken: one token per whitespace-separated word."""

    def count(self, text: str) -> int:
        return len(text.split())

    def split(self, text: str, max_tokens: int) -> list[str]:
        words = text.split()
        return [" ".join(words[i : i + max_tokens]) for i in range(0, len(words), max_tokens)]

    def truncate(self, text: str, max_tokens: int) -> str:
        return " ".join(text.split()[:max_tokens])


@pytest.fixture
def tokenizer() -> WordTokenizer:
    return WordTokenizer()


@pytest.fixture
def settings() -> Settings:
    """Settings independent of any .env file on the machine."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        openai_api_key=SecretStr("sk-test-key"),
        openai_base_url="https://openai.test/v1",
        api_key_pepper=SecretStr("test-pepper"),
        bootstrap_admin_key=SecretStr("rag_admin_test"),
    )
