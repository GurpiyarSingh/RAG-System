"""Versioned prompt templates.

Prompts are files in this package (``answer_v1.md``, ...) rather than inline strings,
so a prompt change is a reviewable diff, can be eval-gated in CI (Phase 2), and old
versions stay available for comparison. The prompt version used is recorded with
each query.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

ANSWER_PROMPT_VERSION = "answer_v1"


@lru_cache
def load_prompt(name: str) -> str:
    return files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


def build_user_message(context: str, question: str) -> str:
    """Sources go in the user turn, so the system prompt stays byte-identical across
    requests. OpenAI's automatic prompt caching reuses that identical prefix, which
    lowers cost and time-to-first-token."""
    return f"<sources>\n{context}\n</sources>\n\nQuestion: {question}"
