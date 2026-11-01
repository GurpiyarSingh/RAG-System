"""Detect and validate the type of an uploaded file.

Binary formats are identified by their **magic bytes** (via the pure-Python
``filetype`` package, so there's no libmagic system dependency on Windows hosts).
The file name and the client's declared Content-Type are only hints, because both
can be wrong or spoofed. Text formats have no magic bytes, so they're recognized by
decoding the content and then using the extension to tell Markdown/HTML/CSV apart.
"""

from __future__ import annotations

import os

import filetype

from rag_service.core.errors import UnsupportedMediaTypeError

# MIME type -> extension used for the stored original (and as a hint to Docling).
SUPPORTED: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}

_TEXT_BY_EXTENSION = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
}


def _looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False  # NUL bytes: binary (unless it's UTF-16 with a BOM)
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A multi-byte character cut off at the sample boundary is still text.
        return exc.start >= len(sample) - 4


def detect_mime(data: bytes, filename: str | None, declared: str | None = None) -> str:
    """Return the canonical MIME type of ``data`` or raise 415 if unsupported."""
    if not data:
        raise UnsupportedMediaTypeError("The file is empty.")

    kind = filetype.guess(data)
    if kind is not None:
        mime: str = kind.mime
        if mime in SUPPORTED:
            return mime
        raise UnsupportedMediaTypeError(f"Unsupported file type: {mime}.")

    if _looks_like_text(data):
        ext = os.path.splitext(filename or "")[1].lower()
        if ext in _TEXT_BY_EXTENSION:
            return _TEXT_BY_EXTENSION[ext]
        if declared in ("text/markdown", "text/html", "text/csv"):
            return declared
        head = data[:512].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            return "text/html"
        return "text/plain"

    raise UnsupportedMediaTypeError("Could not determine the file type, or it isn't supported.")


def extension_for(mime: str) -> str:
    return SUPPORTED.get(mime, ".bin")
