"""Reading markups out of a marked-up PDF, and writing them back out as
things Bluebeam Revu and a human reviewer can both consume.

The premise this whole integration rests on: **Bluebeam markups are ordinary
PDF annotations.** Revu's Markups List is a view over each page's `/Annots`
array, not a proprietary sidecar — which is why this integration needs no
Studio Prime subscription, no OAuth app registration, and no Revu install on
the server. `pypdf` reads and writes those annotations directly.

What that buys, and what it costs:

- Anything Revu writes, this reads. Author (`/T`), the markup type Revu shows
  in its Subject column (`/Subj`), the comment text (`/Contents`), colour,
  timestamps, and page/coordinates all come straight off the annotation.
- Revu's per-markup **status** (the Markups List "Status" column) is not a
  field on the markup itself. It is a *reply annotation* — a child pointing
  back at its parent via `/IRT`, carrying `/State` and `/StateModel`, exactly
  as the PDF spec models annotation states. So statuses are collected in a
  second pass (`_attach_states`) after every annotation on the page is known,
  and the newest reply wins.
- Bluebeam's **custom columns** are private dictionary keys with no published
  names. Rather than guess at a key like `/BluebeamCustomColumns` and quietly
  return nothing when the guess is wrong, every non-standard key is swept into
  `Markup.custom` generically. Whatever Revu called it, it survives the round
  trip and shows up in the CSV — unrecognised, but not lost.

`Markup.key` is the identity used everywhere downstream (dedupe during
consolidation, round-over-round diffing in bluebeam_review). It prefers
`/NM`, the annotation's own GUID, which Revu assigns once and preserves
across save/reopen — the only genuinely stable identifier available. When a
markup has no `/NM` (some non-Revu PDF tools omit it), it falls back to a
hash of the things that would have to all coincide for two markups to really
be the same one. That fallback is deliberately *not* a substitute: a reviewer
who nudges a cloud 2 points to the left produces a new hash, and the diff
will call it a new markup. Documented as a known limitation rather than
papered over with a fuzzy-match heuristic that would be wrong in the other
direction.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject


class BluebeamMarkupError(Exception):
    """Raised when a file handed in isn't a PDF we can read markups from."""


# Annotation subtypes that are real, human-authored markup. Deliberately a
# whitelist: /Popup is the companion note window (not a markup in its own
# right, and Revu doesn't list it), /Widget is an AcroForm field (title block
# form data, not a review comment), and /Link is navigation furniture.
MARKUP_SUBTYPES = frozenset(
    {
        "/Text", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon",
        "/PolyLine", "/Highlight", "/Underline", "/Squiggly", "/StrikeOut",
        "/Stamp", "/Caret", "/Ink", "/FileAttachment", "/Sound", "/Movie",
        "/Redact",
    }
)

_SKIP_SUBTYPES = frozenset({"/Popup", "/Widget", "/Link", "/Projection"})

# Keys handled explicitly below; everything else on the annotation gets swept
# into Markup.custom so Bluebeam's private column data survives.
_STANDARD_KEYS = frozenset(
    {
        "/Type", "/Subtype", "/Rect", "/Contents", "/P", "/NM", "/M", "/F",
        "/AP", "/AS", "/Border", "/C", "/IC", "/CA", "/T", "/Popup",
        "/CreationDate", "/RC", "/Subj", "/IRT", "/RT", "/State",
        "/StateModel", "/BS", "/BE", "/QuadPoints", "/InkList", "/Vertices",
        "/L", "/DA", "/Q", "/DS", "/StructParent", "/OC", "/Rotate", "/IT",
        "/ExData", "/Lang",
    }
)

_PDF_DATE = re.compile(
    r"D:(?P<y>\d{4})(?P<mo>\d{2})?(?P<d>\d{2})?(?P<h>\d{2})?(?P<mi>\d{2})?(?P<s>\d{2})?"
)


def _parse_pdf_date(raw: object) -> datetime | None:
    """PDF date strings (D:YYYYMMDDHHmmSS then a zone tail) -> aware UTC datetime.

    The timezone offset tail is intentionally ignored rather than parsed: a
    markup timestamp is used here only for ordering (which reply is newest,
    which round came first), and a field reviewer's local offset is noise for
    that purpose. Naive-but-consistent beats half-right.
    """
    if raw is None:
        return None
    m = _PDF_DATE.match(str(raw).strip())
    if not m:
        return None
    parts = m.groupdict()
    try:
        return datetime(
            int(parts["y"]), int(parts["mo"] or 1), int(parts["d"] or 1),
            int(parts["h"] or 0), int(parts["mi"] or 0), int(parts["s"] or 0),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _plain(value: object) -> object:
    """Unwrap pypdf objects into JSON-safe Python primitives."""
    if isinstance(value, IndirectObject):
        try:
            value = value.get_object()
        except Exception:  # pragma: no cover - broken xref in a damaged file
            return None
    if isinstance(value, ArrayObject):
        return [_plain(v) for v in value]
    if isinstance(value, DictionaryObject):
        return {str(k): _plain(v) for k, v in value.items() if str(k) != "/Parent"}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _colour_hex(raw: object) -> str | None:
    """PDF colour arrays -> '#rrggbb'. Handles grey/RGB/CMYK component counts."""
    if not isinstance(raw, (ArrayObject, list)):
        return None
    try:
        vals = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if len(vals) == 1:
        rgb = (vals[0],) * 3
    elif len(vals) == 3:
        rgb = (vals[0], vals[1], vals[2])
    elif len(vals) == 4:
        c, m, y, k = vals
        rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    else:
        return None
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


@dataclass
class Markup:
    """One review markup, normalised out of a PDF annotation.

    `key` is the stable identity (see module docstring). `source_label` is
    whichever reviewer/submission this came from — set by the caller, since
    the annotation itself has no idea which copy of the set it lived on.
    """

    key: str
    page: int  # 0-based, matching pypdf; the HMI adds 1 for display
    subtype: str
    author: str | None
    subject: str | None  # Revu's "Subject" column: "Cloud+", "Callout", ...
    contents: str | None
    colour: str | None
    rect: list[float]
    created_at: datetime | None
    modified_at: datetime | None
    status: str | None = None  # from the /IRT reply chain, not the markup itself
    status_author: str | None = None
    nm: str | None = None
    source_label: str | None = None
    custom: dict = field(default_factory=dict)

    @property
    def page_label(self) -> int:
        return self.page + 1

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "page": self.page,
            "page_label": self.page_label,
            "subtype": self.subtype.lstrip("/"),
            "author": self.author,
            "subject": self.subject,
            "contents": self.contents,
            "colour": self.colour,
            "rect": self.rect,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "status": self.status,
            "status_author": self.status_author,
            "nm": self.nm,
            "source_label": self.source_label,
            "custom": self.custom,
        }

    def content_fingerprint(self) -> str:
        """What "the same markup, unchanged" means for round-over-round diffing.

        Position is rounded to whole points — a reviewer dragging a cloud by a
        hair shouldn't register as a change, but a real relocation should.

        Author is deliberately *not* part of this. Identity is already carried
        by `key`: a `/NM`-keyed markup is the same markup whoever's copy it
        arrived in, and a fallback-keyed one already folds the author into the
        key itself. Including it here instead made a comment that several
        reviewers had inherited read as "modified" purely because a different
        reviewer's copy happened to be uploaded first that round.
        """
        basis = "|".join(
            [
                # Normalised, because a subtype read straight off an annotation
                # ("/Square") and one rebuilt from a stored review row
                # ("Square") have to fingerprint identically -- otherwise a
                # markup nobody touched reads as modified on the next round.
                self.subtype.lstrip("/"),
                (self.contents or "").strip(),
                (self.subject or "").strip(),
                str(self.page),
                ",".join(f"{round(v)}" for v in self.rect),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _fallback_key(subtype: str, author: str | None, contents: str | None,
                  page: int, rect: list[float]) -> str:
    basis = "|".join(
        [subtype, (author or "").strip().lower(), (contents or "").strip(),
         str(page), ",".join(f"{round(v, 1)}" for v in rect)]
    )
    return "h:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _rect(raw: object) -> list[float]:
    """Normalise /Rect to [x0, y0, x1, y1] with x0<x1, y0<y1.

    PDF permits the corners in either order; Revu writes both depending on
    which way the user dragged. Normalising here means consolidation's
    overlap check and the fingerprint don't have to care.
    """
    try:
        vals = [float(v) for v in raw]  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]
    if len(vals) != 4:
        return [0.0, 0.0, 0.0, 0.0]
    x0, y0, x1, y1 = vals
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _attach_states(markups_by_nm: dict[str, Markup], replies: list[dict]) -> None:
    """Fold /IRT reply annotations carrying /State onto their parent markup.

    Revu records a status change as a reply, and a markup can accumulate
    several over a review cycle. The newest one is the current status, so
    replies are applied oldest-first and later ones overwrite.
    """
    replies.sort(key=lambda r: r["when"] or datetime.min.replace(tzinfo=timezone.utc))
    for reply in replies:
        parent = markups_by_nm.get(reply["irt"])
        if parent is not None:
            parent.status = reply["state"]
            parent.status_author = reply["author"]


def extract_markups(data: bytes, source_label: str | None = None) -> tuple[list[Markup], int]:
    """Pull every markup out of a PDF's bytes.

    Returns (markups, page_count). Page count comes back because
    consolidation needs it to check that two submissions are really copies of
    the same drawing set before merging them.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise BluebeamMarkupError(f"not a readable PDF: {exc}") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # pragma: no cover - pypdf's exceptions vary
            raise BluebeamMarkupError("PDF is password-protected") from exc

    markups: list[Markup] = []
    by_nm: dict[str, Markup] = {}
    replies: list[dict] = []

    for page_index, page in enumerate(reader.pages):
        try:
            annots = page.get("/Annots") or []
        except Exception:  # pragma: no cover - damaged page tree
            continue
        for ref in annots:
            try:
                obj = ref.get_object()
            except Exception:  # pragma: no cover - dangling reference
                continue
            if not isinstance(obj, DictionaryObject):
                continue
            subtype = str(obj.get("/Subtype", ""))
            if subtype in _SKIP_SUBTYPES:
                continue

            author = obj.get("/T")
            author = str(author) if author is not None else None
            modified = _parse_pdf_date(obj.get("/M"))

            # A status reply: points at a parent and carries a state. It is
            # not itself a markup, so it never enters the list.
            irt = obj.get("/IRT")
            state = obj.get("/State")
            if irt is not None and state is not None:
                irt_obj = irt.get_object() if isinstance(irt, IndirectObject) else irt
                parent_nm = irt_obj.get("/NM") if isinstance(irt_obj, DictionaryObject) else None
                if parent_nm is not None:
                    replies.append({
                        "irt": str(parent_nm),
                        "state": str(state),
                        "author": author,
                        "when": modified,
                    })
                continue

            if subtype not in MARKUP_SUBTYPES:
                continue

            rect = _rect(obj.get("/Rect"))
            contents = obj.get("/Contents")
            contents = str(contents) if contents is not None else None
            subject = obj.get("/Subj")
            subject = str(subject) if subject is not None else None
            nm = obj.get("/NM")
            nm = str(nm) if nm is not None else None

            custom = {
                str(k): _plain(v)
                for k, v in obj.items()
                if str(k) not in _STANDARD_KEYS
            }

            markup = Markup(
                key=nm or _fallback_key(subtype, author, contents, page_index, rect),
                page=page_index,
                subtype=subtype,
                author=author,
                subject=subject,
                contents=contents,
                colour=_colour_hex(obj.get("/C")) or _colour_hex(obj.get("/IC")),
                rect=rect,
                created_at=_parse_pdf_date(obj.get("/CreationDate")),
                modified_at=modified,
                nm=nm,
                source_label=source_label,
                custom=custom,
            )
            markups.append(markup)
            if nm:
                by_nm[nm] = markup

    _attach_states(by_nm, replies)
    return markups, len(reader.pages)


def page_geometry(data: bytes) -> list[tuple[float, float]]:
    """(width, height) in points for every page — consolidation's compatibility check."""
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise BluebeamMarkupError(f"not a readable PDF: {exc}") from exc
    out = []
    for page in reader.pages:
        box = page.mediabox
        out.append((round(float(box.width), 1), round(float(box.height), 1)))
    return out


CSV_COLUMNS = [
    "page", "subtype", "subject", "author", "source", "contents",
    "status", "review_disposition", "assigned_to", "response",
    "colour", "created_at", "modified_at", "key",
]


def markups_to_csv(rows: list[dict]) -> str:
    """Markup-summary CSV, shaped to line up with Revu's own Markups List export.

    Takes plain dicts rather than Markup objects so the review layer can hand
    over rows already carrying disposition/assignee/response — the columns a
    Revu summary has no concept of, which are the entire point of tracking
    approvals in the app instead of in the file.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        out = dict(row)
        out["page"] = row.get("page_label", (row.get("page") or 0) + 1)
        out["source"] = row.get("source_label")
        writer.writerow(out)
    return buf.getvalue()
