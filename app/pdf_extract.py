"""PDF text extraction for datasheet ingest.

Deliberately does one job: turn a PDF's bytes into text. Nothing here tries
to find a Pmax or a model number — the field-level parsing lives in the HMI's
JS, using the same label/value regex approach the ASHRAE panel already uses
for its pasted station data. Keeping it there means one parsing style, not
two, and lets a designer see exactly what text the extraction produced
before any field gets pulled out of it.

pypdf reads embedded text directly; it does not run OCR. A scanned datasheet
with no text layer will extract empty or near-empty text, which the caller
surfaces as a real "nothing to parse" state — not a silent zero-field result.
"""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class PdfExtractionError(Exception):
    """Raised when the bytes handed in aren't a PDF pypdf can open at all."""


def extract_pdf_text(data: bytes) -> tuple[str, int]:
    """Return (text, page_count) for a PDF's raw bytes.

    Pages are joined with a form-feed so the caller (or a human reading the
    captured-text box) can still tell where one page ended and the next
    began, without pypdf's own page-break conventions leaking through.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise PdfExtractionError(f"not a readable PDF: {exc}") from exc

    if reader.is_encrypted:
        # Most vendor datasheets aren't password-protected; the rare one that
        # is should fail loudly rather than silently returning empty text.
        try:
            reader.decrypt("")
        except Exception as exc:  # pragma: no cover - defensive, pypdf's own exceptions vary
            raise PdfExtractionError("PDF is password-protected") from exc

    pages = [page.extract_text() or "" for page in reader.pages]
    return "\f".join(pages), len(reader.pages)
