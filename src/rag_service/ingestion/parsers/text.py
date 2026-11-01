"""Parser for plain text, Markdown and CSV.

Markdown structure is recovered with a small line-based scanner: ATX headings
(``#``..``######``), fenced code blocks, pipe tables and list items. That covers how
documentation is actually written, without pulling in a full Markdown AST library.
Plain text is split into paragraphs on blank lines.
"""

from __future__ import annotations

import re

from rag_service.ingestion.types import Block, ParsedDocument

TEXT_MIME_TYPES = frozenset({"text/plain", "text/markdown", "text/csv"})

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def decode_text(data: bytes) -> str:
    """Decode bytes as UTF-8 (with or without BOM), falling back to Latin-1.

    Latin-1 maps every byte, so decoding never fails. A wrong guess only garbles
    characters instead of rejecting the document.
    """
    # UTF-16 files (common from Windows tools) start with a byte-order mark.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_text(data: bytes, mime_type: str) -> ParsedDocument:
    text = decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
    if mime_type == "text/markdown":
        blocks = _parse_markdown(text)
    elif mime_type == "text/csv":
        # A CSV is one table. The chunker splits it by row groups if it's large.
        blocks = [Block(kind="table", text=text.strip())] if text.strip() else []
    else:
        blocks = [Block(kind="paragraph", text=p) for p in _paragraphs(text)]
    title = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), None)
    return ParsedDocument(blocks=blocks, title=title, parser=f"text:{mime_type}")


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _parse_markdown(text: str) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []
    lines = text.split("\n")
    i = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(Block(kind="paragraph", text="\n".join(paragraph).strip()))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if fence := _FENCE.match(line):
            # Code block: keep verbatim until the matching closing fence.
            flush_paragraph()
            marker, body = fence.group(1), [line]
            i += 1
            while i < len(lines) and not lines[i].startswith(marker):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                body.append(lines[i])
            blocks.append(Block(kind="code", text="\n".join(body)))
        elif heading := _HEADING.match(line):
            flush_paragraph()
            blocks.append(Block(kind="heading", text=heading.group(2), level=len(heading.group(1))))
        elif _TABLE_ROW.match(line):
            flush_paragraph()
            rows = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i])
                i += 1
            blocks.append(Block(kind="table", text="\n".join(rows)))
            continue  # ``i`` already points at the first non-table line
        elif _LIST_ITEM.match(line):
            flush_paragraph()
            blocks.append(Block(kind="list_item", text=line.strip()))
        elif not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line)
        i += 1
    flush_paragraph()
    return [b for b in blocks if b.text.strip()]
