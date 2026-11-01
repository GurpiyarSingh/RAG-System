"""Intermediate representations between pipeline stages.

A parser turns any file format into a flat, ordered list of :class:`Block` objects
that keep the document's *structure* (headings, paragraphs, tables, pages). The
chunker relies on that structure to split along section boundaries instead of at
arbitrary character offsets (ARCHITECTURE.md §7.1).

``ParsedDocument`` is serialized to JSON and stored next to the original file, so
re-chunking later never needs to re-parse.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BlockKind = Literal["heading", "paragraph", "list_item", "table", "code"]


@dataclass(slots=True)
class Block:
    kind: BlockKind
    text: str
    level: int = 0  # heading depth (1 = top level); 0 for non-headings
    page: int | None = None  # 1-based page number when the format has pages


@dataclass(slots=True)
class ParsedDocument:
    blocks: list[Block]
    title: str | None = None
    page_count: int | None = None
    parser: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> ParsedDocument:
        return cls(
            blocks=[Block(**b) for b in data["blocks"]],
            title=data.get("title"),
            page_count=data.get("page_count"),
            parser=data.get("parser", "unknown"),
            metadata=data.get("metadata", {}),
        )

    @property
    def is_empty(self) -> bool:
        return not any(b.text.strip() for b in self.blocks)


@dataclass(slots=True)
class ChunkDraft:
    """A chunk before it gets IDs and embeddings."""

    ordinal: int
    text: str
    embed_text: str  # text prefixed with its contextual header
    token_count: int
    heading_path: list[str]
    page_start: int | None
    page_end: int | None
    chunk_type: Literal["text", "table", "code"] = "text"
