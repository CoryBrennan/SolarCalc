from __future__ import annotations

import io

import pytest
from reportlab.pdfgen import canvas

from app.pdf_extract import PdfExtractionError, extract_pdf_text


def _make_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 750
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def test_extract_pdf_text_reads_embedded_text() -> None:
    pdf = _make_pdf(["Pmax: 700 W", "Voc: 48.60 V"])
    text, pages = extract_pdf_text(pdf)
    assert pages == 1
    assert "Pmax: 700 W" in text
    assert "Voc: 48.60 V" in text


def test_extract_pdf_text_multi_page_joins_with_form_feed() -> None:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Page one")
    c.showPage()
    c.drawString(72, 750, "Page two")
    c.save()
    text, pages = extract_pdf_text(buf.getvalue())
    assert pages == 2
    assert "\f" in text
    assert "Page one" in text
    assert "Page two" in text


def test_extract_pdf_text_rejects_non_pdf_bytes() -> None:
    with pytest.raises(PdfExtractionError):
        extract_pdf_text(b"this is not a pdf")
