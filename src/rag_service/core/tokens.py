"""Token counting with the embedding model's tokenizer.

Chunk sizes, context budgets and rate-limit reservations are measured in *model
tokens*, not characters. A character count can be off by 2-4x depending on language
and formatting. OpenAI's ``text-embedding-3-*`` and ``gpt-4o`` families use
``cl100k_base`` / ``o200k_base``, which are close enough for budgeting, so a single
encoding (``cl100k_base``, the embedding model's) is used everywhere.

In containers the encoding file is downloaded at image build time (see the
Dockerfile, ``TIKTOKEN_CACHE_DIR``), so runtime needs no network access to fetch it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

import tiktoken


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...

    def split(self, text: str, max_tokens: int) -> list[str]:
        """Hard-split text into consecutive windows of at most ``max_tokens`` tokens."""
        ...

    def truncate(self, text: str, max_tokens: int) -> str: ...


class TiktokenTokenizer:
    def __init__(self, encoding: str = "cl100k_base") -> None:
        self._enc = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        # disallowed_special=() treats strings like "<|endoftext|>" in user documents
        # as plain text instead of raising.
        return len(self._enc.encode(text, disallowed_special=()))

    def split(self, text: str, max_tokens: int) -> list[str]:
        ids = self._enc.encode(text, disallowed_special=())
        return [self._enc.decode(ids[i : i + max_tokens]) for i in range(0, len(ids), max_tokens)]

    def truncate(self, text: str, max_tokens: int) -> str:
        ids = self._enc.encode(text, disallowed_special=())
        return text if len(ids) <= max_tokens else self._enc.decode(ids[:max_tokens])


@lru_cache
def get_tokenizer() -> Tokenizer:
    """Process-wide tokenizer (loading the BPE ranks takes ~100 ms)."""
    return TiktokenTokenizer()
