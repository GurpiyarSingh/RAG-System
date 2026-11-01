"""Text/Markdown parser and MIME detection."""

from __future__ import annotations

import pytest

from rag_service.core.errors import UnsupportedMediaTypeError
from rag_service.ingestion.mime import detect_mime
from rag_service.ingestion.parsers.text import decode_text, parse_text

MARKDOWN = b"""# Employee Handbook

Intro paragraph
spanning two lines.

## Leave

- Annual: 20 days
- Sick: 10 days

| Type | Days |
|------|------|
| A    | 20   |

```python
print("# not a heading")
```
"""


def test_markdown_structure() -> None:
    doc = parse_text(MARKDOWN, "text/markdown")
    kinds = [(b.kind, b.level) for b in doc.blocks]
    assert kinds == [
        ("heading", 1),
        ("paragraph", 0),
        ("heading", 2),
        ("list_item", 0),
        ("list_item", 0),
        ("table", 0),
        ("code", 0),
    ]
    assert doc.title == "Employee Handbook"
    assert doc.blocks[1].text == "Intro paragraph\nspanning two lines."
    # A '#' inside a fenced code block must not become a heading.
    assert "# not a heading" in doc.blocks[-1].text


def test_plain_text_paragraphs() -> None:
    doc = parse_text(b"First para.\n\n\nSecond para.\r\nstill second.", "text/plain")
    assert [b.text for b in doc.blocks] == ["First para.", "Second para.\nstill second."]


def test_decode_text_handles_bom_and_latin1() -> None:
    assert decode_text("﻿hello".encode()) == "hello"
    assert decode_text("héllo".encode("utf-16")) == "héllo"
    assert decode_text("café".encode("latin-1")) == "café"


@pytest.mark.parametrize(
    ("data", "filename", "expected"),
    [
        (b"%PDF-1.7\n%...", "x.pdf", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "x.png", "image/png"),
        (b"# Title\n", "notes.md", "text/markdown"),
        (b"<!DOCTYPE html><html></html>", "page", "text/html"),
        (b"a,b\n1,2\n", "data.csv", "text/csv"),
        (b"just text", None, "text/plain"),
    ],
)
def test_detect_mime(data: bytes, filename: str | None, expected: str) -> None:
    assert detect_mime(data, filename) == expected


def test_detect_mime_trusts_content_over_extension() -> None:
    # A PDF renamed to .txt is still a PDF.
    assert detect_mime(b"%PDF-1.4 ...", "innocent.txt") == "application/pdf"


@pytest.mark.parametrize("data", [b"", b"\x7fELF\x02\x01\x01" + b"\x00" * 64])
def test_detect_mime_rejects_empty_and_unsupported(data: bytes) -> None:
    with pytest.raises(UnsupportedMediaTypeError):
        detect_mime(data, "file.bin")
