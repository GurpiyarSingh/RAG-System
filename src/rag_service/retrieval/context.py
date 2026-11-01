"""Turn retrieved chunks into the numbered source block the LLM answers from.

Each chunk becomes ``<source id="n" ...>text</source>``. The ``n`` is what the model
cites as ``[n]``, and it maps back to the chunk for the citation list. Two defenses
matter here:

* **Token budget.** Chunks are added in rank order until the budget is reached, so a
  long tail of weak matches never crowds out the prompt (or the bill).
* **Prompt-injection framing.** Source text is untrusted. It's wrapped in explicit
  delimiters, and any literal ``</source>`` inside a document is neutralized so a
  document can't "close" its block and smuggle instructions outside it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from rag_service.core.tokens import Tokenizer
from rag_service.retrieval.pipeline import RetrievedChunk

_CLOSING_TAG = re.compile(r"</\s*source", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Source:
    """A chunk that was shown to the LLM, with its citation number."""

    number: int
    chunk: RetrievedChunk


@dataclass(frozen=True, slots=True)
class Context:
    text: str
    sources: list[Source]
    tokens: int


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def _render(number: int, chunk: RetrievedChunk) -> str:
    attrs = [f'id="{number}"']
    if chunk.title:
        attrs.append(f'title="{_attr(chunk.title)}"')
    if chunk.heading_path:
        attrs.append(f'section="{_attr(" > ".join(chunk.heading_path))}"')
    if chunk.page_start is not None:
        pages = (
            str(chunk.page_start)
            if chunk.page_end in (None, chunk.page_start)
            else f"{chunk.page_start}-{chunk.page_end}"
        )
        attrs.append(f'page="{pages}"')
    # Neutralize closing tags in any letter case ("</Source>", "</SOURCE >", ...).
    body = _CLOSING_TAG.sub("&lt;/source", chunk.text)
    return f"<source {' '.join(attrs)}>\n{body}\n</source>"


def build_context(chunks: list[RetrievedChunk], tokenizer: Tokenizer, budget: int) -> Context:
    parts: list[str] = []
    sources: list[Source] = []
    used = 0
    for chunk in chunks:
        rendered = _render(len(sources) + 1, chunk)
        cost = tokenizer.count(rendered)
        if sources and used + cost > budget:
            break  # always keep at least the best chunk, even if it alone exceeds the budget
        parts.append(rendered)
        sources.append(Source(number=len(sources) + 1, chunk=chunk))
        used += cost
    return Context(text="\n\n".join(parts), sources=sources, tokens=used)
