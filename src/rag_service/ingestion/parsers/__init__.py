"""Format-specific parsers producing a :class:`~rag_service.ingestion.types.ParsedDocument`.

Plain text and Markdown are handled by the lightweight :mod:`.text` parser. Everything
else (PDF, Office, HTML, images) goes to Docling, which is installed only in the
``rag-parser`` image (``pip install .[parser]``), so it's imported lazily.
"""

from __future__ import annotations

from rag_service.ingestion.parsers.text import TEXT_MIME_TYPES, parse_text
from rag_service.ingestion.types import ParsedDocument


async def parse_bytes(data: bytes, mime_type: str, filename: str) -> ParsedDocument:
    """Dispatch to the right parser for ``mime_type``."""
    if mime_type in TEXT_MIME_TYPES:
        return parse_text(data, mime_type)
    # Imported here so API/index containers never need the heavy Docling stack.
    from rag_service.ingestion.parsers.docling_parser import parse_with_docling

    return await parse_with_docling(data, mime_type, filename)
