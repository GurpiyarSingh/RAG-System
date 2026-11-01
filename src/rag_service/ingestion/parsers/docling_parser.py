"""Docling-based parser for PDF, DOCX, PPTX, XLSX, HTML and images.

Why Docling (ARCHITECTURE.md §2, §7.1): it does layout analysis and table-structure
recognition, and returns a structured document tree (headings, paragraphs, tables
with rows and columns, page numbers). Structure-aware chunking needs exactly that.

Behavior worth knowing:
* OCR runs only on *bitmap regions* (scanned pages, images). Pages that already have
  a text layer use it directly, which is faster and more accurate than OCR.
* Tables are exported as Markdown, which LLMs read well and which keeps row/column
  relationships that flattened text would lose.
* Conversion is CPU-bound, synchronous code. It runs in a worker thread so the
  Temporal worker's event loop keeps heartbeating while a large PDF is processed.
* Layout/OCR models are baked into the ``rag-parser`` image at build time
  (``DOCLING_ARTIFACTS_PATH``), so containers start without downloading anything.
"""

from __future__ import annotations

import asyncio
import io
import os
from typing import Any

from rag_service.core.logging import get_logger
from rag_service.ingestion.types import Block, BlockKind, ParsedDocument

log = get_logger(__name__)

# Docling item labels that carry no retrievable content (repeated on every page).
_SKIP_LABELS = {"page_header", "page_footer", "picture", "chart"}
_LABEL_TO_KIND: dict[str, BlockKind] = {
    "title": "heading",
    "section_header": "heading",
    "list_item": "list_item",
    "code": "code",
    "table": "table",
}

_converter: Any = None
# A DocumentConverter isn't documented as thread-safe; one conversion at a time per
# process. Parse throughput is scaled by running more parse-worker containers.
_lock = asyncio.Lock()


def _get_converter() -> Any:
    """Build the converter once per process; model loading takes several seconds."""
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import (
            DocumentConverter,
            ImageFormatOption,
            PdfFormatOption,
        )

        options = PdfPipelineOptions(
            artifacts_path=os.environ.get("DOCLING_ARTIFACTS_PATH") or None,
            do_ocr=True,
            do_table_structure=True,
        )
        _converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
            }
        )
    return _converter


def _convert(data: bytes, filename: str) -> ParsedDocument:
    from docling.datamodel.base_models import DocumentStream
    from docling_core.types.doc import TableItem

    result = _get_converter().convert(DocumentStream(name=filename, stream=io.BytesIO(data)))
    doc = result.document

    blocks: list[Block] = []
    title: str | None = None
    for item, _depth in doc.iterate_items():
        label = str(getattr(item, "label", "")).split(".")[-1].lower()
        if label in _SKIP_LABELS:
            continue
        prov = getattr(item, "prov", None)
        page = prov[0].page_no if prov else None

        if isinstance(item, TableItem):
            text = item.export_to_markdown(doc=doc)
            kind: BlockKind = "table"
        else:
            text = getattr(item, "text", "") or ""
            kind = _LABEL_TO_KIND.get(label, "paragraph")
        if not text.strip():
            continue

        level = 0
        if kind == "heading":
            # The document title is level 1; section headers start one level below it.
            level = 1 if label == "title" else int(getattr(item, "level", 1)) + 1
            if label == "title" and title is None:
                title = text.strip()
        blocks.append(Block(kind=kind, text=text.strip(), level=level, page=page))

    page_count = doc.num_pages() if hasattr(doc, "num_pages") else None
    return ParsedDocument(
        blocks=blocks, title=title, page_count=page_count or None, parser="docling"
    )


async def parse_with_docling(data: bytes, mime_type: str, filename: str) -> ParsedDocument:
    async with _lock:
        log.info("docling_parse_started", filename=filename, mime_type=mime_type, size=len(data))
        parsed = await asyncio.to_thread(_convert, data, filename)
    log.info("docling_parse_finished", blocks=len(parsed.blocks), pages=parsed.page_count)
    return parsed
