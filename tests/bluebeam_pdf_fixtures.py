"""Builders for synthetic marked-up PDFs shaped the way Bluebeam Revu writes
them, so the Bluebeam tests exercise real annotation structure rather than a
convenient simplification.

What these deliberately reproduce, because each one broke something during
development:

- **Appearance streams** (`/AP` -> a form XObject). Consolidation clones
  annotations across documents, and a clone that silently drops the appearance
  stream produces a merged set whose markups are invisible until a viewer
  regenerates them.
- **Status as a reply annotation** (`/IRT` + `/State`), not a field on the
  markup, which is how Revu actually stores the Markups List status column —
  including the case where the reply sits *before* its parent in the page's
  `/Annots` array.
- **Reversed `/Rect` corners**, which Revu writes depending on drag direction.
- **Markups with no `/NM` GUID**, as produced by non-Revu PDF tools, which is
  what forces the content-hash fallback identity.

No real drawing set is committed to the repo — plan sets are large, and
client-confidential. See the README's note on the one thing these fixtures
cannot prove: that Revu itself opens the output correctly.
"""

from __future__ import annotations

import io

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)

#: ARCH D landscape (24" x 36") in points — a normal plan-set sheet size.
SHEET_W = 2592.0
SHEET_H = 1728.0


def blank_set(pages: int = 3, width: float = SHEET_W, height: float = SHEET_H) -> bytes:
    """A clean, unmarked drawing set — what reviewers get issued."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width, height)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _appearance(writer: PdfWriter, marker: str):
    stream = DecodedStreamObject()
    stream.set_data(f"1 0 0 RG 2 w 5 5 60 30 re S % {marker}".encode())
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    stream[NameObject("/BBox")] = ArrayObject(
        [FloatObject(0), FloatObject(0), FloatObject(70), FloatObject(40)]
    )
    stream[NameObject("/Resources")] = DictionaryObject()
    return writer._add_object(stream)


def marked_set(
    author: str,
    markups: list[dict],
    status_replies: list[dict] | None = None,
    pages: int = 3,
    width: float = SHEET_W,
    height: float = SHEET_H,
) -> bytes:
    """One reviewer's marked-up copy.

    markups: dicts of page, rect, contents, and optionally nm, subject,
    subtype, reversed_rect, custom.
    status_replies: dicts of page, parent_nm, state.
    """
    writer = PdfWriter(clone_from=io.BytesIO(blank_set(pages, width, height)))
    per_page: dict[int, list] = {}
    made: dict[str, object] = {}

    for i, spec in enumerate(markups):
        annot = DictionaryObject()
        annot[NameObject("/Type")] = NameObject("/Annot")
        annot[NameObject("/Subtype")] = NameObject(spec.get("subtype", "/Square"))
        x0, y0, x1, y1 = spec["rect"]
        corners = [x1, y1, x0, y0] if spec.get("reversed_rect") else [x0, y0, x1, y1]
        annot[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in corners])
        annot[NameObject("/T")] = TextStringObject(author)
        annot[NameObject("/Subj")] = TextStringObject(spec.get("subject", "Cloud+"))
        annot[NameObject("/Contents")] = TextStringObject(spec["contents"])
        annot[NameObject("/M")] = TextStringObject("D:20260815094500-05'00'")
        annot[NameObject("/CreationDate")] = TextStringObject("D:20260815093000-05'00'")
        annot[NameObject("/C")] = ArrayObject(
            [FloatObject(1), FloatObject(0), FloatObject(0)]
        )
        appearance = DictionaryObject()
        appearance[NameObject("/N")] = _appearance(writer, f"{author}-{i}")
        annot[NameObject("/AP")] = appearance
        if spec.get("nm"):
            annot[NameObject("/NM")] = TextStringObject(spec["nm"])
        for key, value in (spec.get("custom") or {}).items():
            annot[NameObject(key)] = TextStringObject(value)

        ref = writer._add_object(annot)
        made[spec.get("nm") or spec["contents"]] = ref
        per_page.setdefault(spec["page"], []).append(ref)

    for reply in status_replies or []:
        r = DictionaryObject()
        r[NameObject("/Type")] = NameObject("/Annot")
        r[NameObject("/Subtype")] = NameObject("/Text")
        r[NameObject("/Rect")] = ArrayObject([FloatObject(0)] * 4)
        r[NameObject("/IRT")] = made[reply["parent_nm"]]
        r[NameObject("/State")] = TextStringObject(reply["state"])
        r[NameObject("/StateModel")] = TextStringObject("Review")
        r[NameObject("/T")] = TextStringObject(reply.get("author", author))
        r[NameObject("/M")] = TextStringObject(
            reply.get("when", "D:20260816110000-05'00'")
        )
        # Insert ahead of the parent: Revu makes no ordering guarantee, and a
        # reply-before-parent array is what caught the /IRT relink bug.
        per_page.setdefault(reply["page"], []).insert(0, writer._add_object(r))

    for page_index, refs in per_page.items():
        writer.pages[page_index][NameObject("/Annots")] = ArrayObject(refs)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def noise_set(pages: int = 3) -> bytes:
    """A set whose only annotations are things that are *not* review markups —
    a popup companion window and a title-block form field."""
    writer = PdfWriter(clone_from=io.BytesIO(blank_set(pages)))
    popup = DictionaryObject()
    popup[NameObject("/Type")] = NameObject("/Annot")
    popup[NameObject("/Subtype")] = NameObject("/Popup")
    popup[NameObject("/Rect")] = ArrayObject([FloatObject(0)] * 4)

    widget = DictionaryObject()
    widget[NameObject("/Type")] = NameObject("/Annot")
    widget[NameObject("/Subtype")] = NameObject("/Widget")
    widget[NameObject("/Rect")] = ArrayObject([FloatObject(0)] * 4)
    widget[NameObject("/T")] = TextStringObject("TitleBlockRevision")

    writer.pages[0][NameObject("/Annots")] = ArrayObject(
        [writer._add_object(popup), writer._add_object(widget)]
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
