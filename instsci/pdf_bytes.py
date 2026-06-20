"""PDF byte validation helpers."""

from __future__ import annotations

from typing import Any


MIN_PDF_BYTES = 5_000


def is_plausible_pdf_bytes(body: Any, *, min_bytes: int = MIN_PDF_BYTES) -> bool:
    """Return whether bytes look like an actual PDF payload."""
    if not isinstance(body, (bytes, bytearray)):
        return False
    data = bytes(body)
    if not data.startswith(b"%PDF-") or len(data) <= min_bytes:
        return False
    if b"\xef\xbf\xbd" in data[:64]:
        return False
    eof = data.rfind(b"%%EOF")
    if eof == -1:
        return True
    return eof >= max(0, len(data) - 8192)


def describe_non_pdf_bytes(body: Any, *, min_bytes: int = MIN_PDF_BYTES) -> str:
    """Return a short reason for rejecting a non-PDF payload."""
    if not isinstance(body, (bytes, bytearray)):
        return "not_bytes"
    data = bytes(body)
    if not data:
        return "empty"
    prefix = data[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "html_response"
    if len(data) <= min_bytes:
        return "too_small"
    if not data.startswith(b"%PDF-"):
        return "missing_pdf_header"
    if b"\xef\xbf\xbd" in data[:64]:
        return "corrupt_pdf_header"
    eof = data.rfind(b"%%EOF")
    if eof != -1 and eof < max(0, len(data) - 8192):
        return "early_eof_with_trailing_payload"
    return "unknown_non_pdf"
