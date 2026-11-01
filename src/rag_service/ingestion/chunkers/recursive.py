"""Structure-aware recursive chunker (ARCHITECTURE.md §7.1, IMPLEMENTATION_PLAN.md P1.16).

Splitting order, coarse to fine:

1. **Sections.** A heading starts a new section, and chunks never span two sections,
   so every chunk has one unambiguous heading path.
2. **Blocks** (paragraphs, list items, code) are packed greedily into chunks of at
   most ``max_tokens``.
3. A block that's too big on its own is split into **sentences**, and a sentence
   that's still too big is cut into token windows. Splitting never happens mid-word.

Tables become their own chunks. A large table is split by row groups with the header
row repeated, so every piece is still a readable table.

Consecutive chunks of a section overlap by up to ``overlap_tokens`` (trailing
sentences of the previous chunk), so a fact that straddles a boundary is still
retrievable from either side.

Each chunk also gets a **contextual header** ("Doc title > Section > Subsection")
that's prepended to the text used for embedding. A chunk like "It must be filed
within 3 days" is meaningless on its own; the header puts the context back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rag_service.core.tokens import Tokenizer
from rag_service.ingestion.types import Block, ChunkDraft, ParsedDocument

# Sentence boundary: end punctuation followed by whitespace. Imperfect for
# abbreviations ("e.g. this"), which only costs a slightly earlier split.
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")  # noqa: RUF001 - CJK sentence enders are intentional
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
# Decoding a token window and re-encoding it can yield a few more tokens than the
# window held (merges across the cut), so hard splits leave this much headroom.
_WINDOW_MARGIN = 8


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    max_tokens: int = 512
    overlap_tokens: int = 64

    def __post_init__(self) -> None:
        if self.max_tokens < 32:
            raise ValueError("max_tokens must be at least 32")
        if not 0 <= self.overlap_tokens < self.max_tokens // 2:
            raise ValueError("overlap_tokens must be in [0, max_tokens / 2)")


@dataclass(slots=True)
class _Piece:
    text: str
    page: int | None
    is_overlap: bool = False
    # Separator placed before this piece when it follows another one in a chunk:
    # a blank line between blocks, a space between sentences of the same paragraph
    # (so splitting a long paragraph doesn't change how it reads).
    sep: str = "\n\n"


def _join(pieces: list[_Piece]) -> str:
    return pieces[0].text + "".join(p.sep + p.text for p in pieces[1:]) if pieces else ""


@dataclass(slots=True)
class _Section:
    heading_path: list[str]
    blocks: list[Block] = field(default_factory=list)


class RecursiveChunker:
    def __init__(self, tokenizer: Tokenizer, config: ChunkingConfig | None = None) -> None:
        self._tok = tokenizer
        self._cfg = config or ChunkingConfig()

    # ------------------------------------------------------------------ public API
    def chunk(self, doc: ParsedDocument) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        for section in self._sections(doc.blocks):
            drafts.extend(self._chunk_section(section, doc.title))
        for ordinal, draft in enumerate(drafts):
            draft.ordinal = ordinal
        return drafts

    # ------------------------------------------------------------------ sections
    @staticmethod
    def _sections(blocks: list[Block]) -> list[_Section]:
        """Group blocks under their nearest heading, tracking the full heading path."""
        sections: list[_Section] = []
        path: list[str] = []
        current = _Section(heading_path=[])
        for block in blocks:
            if block.kind == "heading":
                if current.blocks:
                    sections.append(current)
                level = max(block.level, 1)
                # Keep ancestors above this level and replace the rest. Skipped
                # levels (e.g. H1 → H3) just keep the shorter path.
                path = [*path[: level - 1], block.text.strip()]
                current = _Section(heading_path=list(path))
            else:
                current.blocks.append(block)
        if current.blocks:
            sections.append(current)
        return sections

    def _chunk_section(self, section: _Section, title: str | None) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        pending: list[_Piece] = []  # text pieces awaiting packing

        def flush_text() -> None:
            if pending:
                drafts.extend(self._pack(pending, section.heading_path, title))
                pending.clear()

        for block in section.blocks:
            if block.kind == "table":
                flush_text()  # keep document order: text before the table stays before it
                for part in self._split_table(block.text):
                    drafts.append(
                        self._draft(part, section.heading_path, title, [block.page], "table")
                    )
            else:
                for i, text in enumerate(self._fit(block.text)):
                    pending.append(_Piece(text, block.page, sep="\n\n" if i == 0 else " "))
        flush_text()
        return drafts

    # ------------------------------------------------------------------ packing
    def _pack(self, pieces: list[_Piece], path: list[str], title: str | None) -> list[ChunkDraft]:
        """Greedily pack pieces into chunks of at most ``max_tokens``, with overlap."""
        max_tokens = self._cfg.max_tokens
        drafts: list[ChunkDraft] = []
        current: list[_Piece] = []

        def emit() -> None:
            # A chunk made only of overlap carries no new content, so it's dropped.
            if any(not p.is_overlap for p in current):
                drafts.append(
                    self._draft(
                        _join(current),
                        path,
                        title,
                        [p.page for p in current],
                        "text",
                    )
                )

        for piece in pieces:
            candidate = _join([*current, piece])
            if not current or self._tok.count(candidate) <= max_tokens:
                current.append(piece)
                continue
            emit()
            previous_text = _join(current)
            current = []
            overlap = self._tail(previous_text, self._cfg.overlap_tokens)
            carried = _Piece(overlap, piece.page, is_overlap=True)
            # Only carry the overlap if the new piece still fits next to it.
            if overlap and self._tok.count(_join([carried, piece])) <= max_tokens:
                current.append(carried)
            current.append(piece)
        if current:
            emit()
        return drafts

    def _fit(self, text: str) -> list[str]:
        """Return ``text`` as pieces that each fit into one chunk."""
        text = text.strip()
        if not text:
            return []
        if self._tok.count(text) <= self._cfg.max_tokens:
            return [text]
        pieces: list[str] = []
        for sentence in _SENTENCE_END.split(text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if self._tok.count(sentence) <= self._cfg.max_tokens:
                pieces.append(sentence)
            else:
                # Pathological input (no punctuation, e.g. minified data): hard windows.
                pieces.extend(
                    w.strip()
                    for w in self._tok.split(sentence, self._cfg.max_tokens - _WINDOW_MARGIN)
                    if w.strip()
                )
        return pieces

    def _tail(self, text: str, budget: int) -> str:
        """Trailing whole sentences of ``text`` totalling at most ``budget`` tokens."""
        if budget <= 0:
            return ""
        taken: list[str] = []
        for sentence in reversed(_SENTENCE_END.split(text.strip())):
            candidate = " ".join([sentence, *taken])
            if self._tok.count(candidate) > budget:
                break
            taken.insert(0, sentence)
        return " ".join(taken)

    # ------------------------------------------------------------------ tables
    def _split_table(self, table: str) -> list[str]:
        """Split a Markdown table by row groups, repeating the header in every part."""
        table = table.strip()
        if self._tok.count(table) <= self._cfg.max_tokens:
            return [table]
        lines = table.split("\n")
        header: list[str] = []
        if len(lines) >= 2 and _TABLE_SEPARATOR.match(lines[1]):
            header, lines = lines[:2], lines[2:]
        parts: list[str] = []
        rows: list[str] = []
        for row in lines:
            candidate = "\n".join([*header, *rows, row])
            if rows and self._tok.count(candidate) > self._cfg.max_tokens:
                parts.append("\n".join([*header, *rows]))
                rows = []
            rows.append(row)
        if rows:
            parts.append("\n".join([*header, *rows]))
        # A single row may still exceed the limit (huge cell): cut it into windows.
        fitted: list[str] = []
        for part in parts:
            if self._tok.count(part) <= self._cfg.max_tokens:
                fitted.append(part)
            else:
                fitted.extend(self._tok.split(part, self._cfg.max_tokens - _WINDOW_MARGIN))
        return fitted

    # ------------------------------------------------------------------ drafts
    def _draft(
        self,
        text: str,
        path: list[str],
        title: str | None,
        pages: list[int | None],
        chunk_type: str,
    ) -> ChunkDraft:
        known_pages = [p for p in pages if p is not None]
        # Contextual header for embedding (title once, even if it's also the H1).
        context = [title] if title else []
        context += [h for h in path if h != title]
        embed_text = f"{' > '.join(context)}\n\n{text}" if context else text
        return ChunkDraft(
            ordinal=0,  # assigned once all sections are chunked
            text=text,
            embed_text=embed_text,
            token_count=self._tok.count(text),
            heading_path=list(path),
            page_start=min(known_pages) if known_pages else None,
            page_end=max(known_pages) if known_pages else None,
            chunk_type=chunk_type,  # type: ignore[arg-type]
        )
