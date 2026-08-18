"""Writing this app's computed design data onto a drawing PDF as real
Bluebeam markups.

The other half of the integration. bluebeam_consolidate brings reviewers'
markups *in*; this puts the engine's own numbers *out* — equipment tags,
conductor and conduit callouts, trench sections — onto the plan set as
annotations a reviewer opens in Revu and sees in the Markups List alongside
every human comment. They are live annotations, not burned-in graphics: the
reviewer can move them, reply to them, change their status, and export them
in a markup summary.

Every stamp carries `/SolarCalcTag`, so a set that comes back marked up can be
tied comment-by-comment to the equipment it concerns without matching on text.

**Placement is the hard part, and it is not guessed at.** A callout needs a
page coordinate; the app knows equipment by tag and, via
`pvcase_dwg_scan.DwgDeviceTag`, by *model-space* coordinate. Those are not
page points — a site plan is model geometry plotted to a sheet at some scale,
rotation, and origin that lives in the drawing's layout, which the DWG
scanner does not read. Rather than invent a scale factor, `PlanTransform`
takes two control points: pick two pieces of equipment that are far apart and
identifiable on the sheet, give their PDF coordinates once, and the
similarity transform (uniform scale, rotation, translation) that maps every
other tag is solved from those. Two points is exactly what a similarity
transform needs — no more, and no fewer.

It is a *similarity* transform on purpose, not a general affine one. A plotted
site plan preserves shape: it can be scaled, rotated, and shifted, but not
stretched along one axis independently. Fitting an affine would silently
absorb the user's control-point error into a fake anisotropic scale and place
everything slightly wrong; a similarity transform can't, so the same error
instead shows up honestly in `residual_error_pt`, which is reported back so a
bad calibration is visible rather than baked in.

For anything with no coordinate at all — a conductor schedule, a general note
— `stamp_schedule` drops a table at a chosen corner of a chosen sheet. No
calibration involved, and always available.
"""

from __future__ import annotations

import io
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pypdf import PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

#: Private key tying a stamp back to the equipment tag it describes.
TAG_KEY = "/SolarCalcTag"
#: Marks a stamp as engine-authored, so a re-uploaded set can tell the app's
#: own callouts apart from human review comments.
ORIGIN_KEY = "/SolarCalcOrigin"
ORIGIN_VALUE = "solar-calc-engine"

STAMP_AUTHOR = "Solar Calc Engine"

# Helvetica advance widths average very close to this fraction of the font
# size across mixed-case text. Used only to size the box around a label --
# a few points of slack either way is invisible, and the alternative (parsing
# AFM metrics for one cosmetic decision) is not worth the dependency.
_HELV_AVG_ADVANCE = 0.52


class StampError(Exception):
    """Raised on an unusable calibration or an unreadable target PDF."""


def _pdf_now() -> str:
    return datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")


def _esc(text: str) -> str:
    """Escape a string for a PDF content-stream literal."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


@dataclass
class ControlPoint:
    """One model-space location and where it actually sits on the sheet."""

    model_x: float
    model_y: float
    page_x: float
    page_y: float


@dataclass
class PlanTransform:
    """Similarity transform (scale + rotation + translation), model -> page points."""

    scale: float
    rotation_rad: float
    offset_x: float
    offset_y: float
    residual_error_pt: float = 0.0

    @classmethod
    def from_control_points(cls, a: ControlPoint, b: ControlPoint) -> "PlanTransform":
        mdx, mdy = b.model_x - a.model_x, b.model_y - a.model_y
        pdx, pdy = b.page_x - a.page_x, b.page_y - a.page_y
        model_dist = math.hypot(mdx, mdy)
        page_dist = math.hypot(pdx, pdy)

        if model_dist < 1e-9:
            raise StampError(
                "the two control points share a model-space location; pick two "
                "different pieces of equipment"
            )
        if page_dist < 1e-9:
            raise StampError(
                "the two control points share a page location; pick two points "
                "that are far apart on the sheet"
            )

        scale = page_dist / model_dist
        rotation = math.atan2(pdy, pdx) - math.atan2(mdy, mdx)

        cos_r, sin_r = math.cos(rotation), math.sin(rotation)
        offset_x = a.page_x - scale * (cos_r * a.model_x - sin_r * a.model_y)
        offset_y = a.page_y - scale * (sin_r * a.model_x + cos_r * a.model_y)

        t = cls(scale, rotation, offset_x, offset_y)
        # Exact by construction at both control points, so the residual only
        # ever reports a real defect (degenerate input, numerical blow-up).
        bx, by = t.apply(b.model_x, b.model_y)
        t.residual_error_pt = round(math.hypot(bx - b.page_x, by - b.page_y), 4)
        return t

    def apply(self, model_x: float, model_y: float) -> tuple[float, float]:
        cos_r, sin_r = math.cos(self.rotation_rad), math.sin(self.rotation_rad)
        return (
            self.scale * (cos_r * model_x - sin_r * model_y) + self.offset_x,
            self.scale * (sin_r * model_x + cos_r * model_y) + self.offset_y,
        )

    def to_dict(self) -> dict:
        return {
            "scale": self.scale,
            "rotation_deg": round(math.degrees(self.rotation_rad), 4),
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "residual_error_pt": self.residual_error_pt,
        }


@dataclass
class StampItem:
    """One callout to place.

    Exactly one of (`model_x`/`model_y` + a transform) or (`page_x`/`page_y`)
    has to resolve to a location; `resolve` enforces that rather than
    defaulting a missing coordinate to the page origin, where a silently
    mislaid callout would be worse than a loud failure.
    """

    label: str
    page: int = 0  # 0-based
    tag: str | None = None
    detail: list[str] = field(default_factory=list)
    model_x: float | None = None
    model_y: float | None = None
    page_x: float | None = None
    page_y: float | None = None
    colour: tuple[float, float, float] = (0.85, 0.2, 0.1)

    def resolve(self, transform: PlanTransform | None) -> tuple[float, float]:
        if self.page_x is not None and self.page_y is not None:
            return self.page_x, self.page_y
        if self.model_x is not None and self.model_y is not None:
            if transform is None:
                raise StampError(
                    f"{self.label!r} has model coordinates but no calibration was "
                    "supplied; provide two control points or give page coordinates"
                )
            return transform.apply(self.model_x, self.model_y)
        raise StampError(f"{self.label!r} has no page or model coordinates to place it at")


def _text_box_stream(
    writer: PdfWriter,
    lines: list[str],
    width: float,
    height: float,
    font_size: float,
    colour: tuple[float, float, float],
) -> DictionaryObject:
    """A form XObject drawing the callout: white panel, coloured border, text.

    Written explicitly instead of relying on the viewer to synthesise an
    appearance from /DA. Revu does generate appearances for FreeText, but a
    PDF whose annotations only render after a particular viewer touches them
    is a PDF that looks empty everywhere else -- including in the thumbnail a
    reviewer sees before opening it.
    """
    r, g, b = colour
    ops = [
        "q",
        "1 1 1 rg",
        f"0 0 {width:.2f} {height:.2f} re f",
        f"{r:.3f} {g:.3f} {b:.3f} RG",
        "1.2 w",
        f"0.6 0.6 {width - 1.2:.2f} {height - 1.2:.2f} re S",
        "BT",
        f"/Helv {font_size:.2f} Tf",
        f"{r:.3f} {g:.3f} {b:.3f} rg",
    ]
    leading = font_size * 1.25
    y = height - leading + (leading - font_size) / 2
    for i, line in enumerate(lines):
        # First line is the label (bold-ish by colour), rest are detail rows
        # in near-black so the label still reads as the heading.
        if i == 1:
            ops.append("0.15 0.15 0.15 rg")
        ops.append(f"1 0 0 1 {4:.2f} {y:.2f} Tm ({_esc(line)}) Tj")
        y -= leading
    ops += ["ET", "Q"]

    stream = DecodedStreamObject()
    stream.set_data("\n".join(ops).encode("latin-1", "replace"))
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    stream[NameObject("/FormType")] = NumberObject(1)
    stream[NameObject("/BBox")] = ArrayObject(
        [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
    )

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
    fonts = DictionaryObject()
    fonts[NameObject("/Helv")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    stream[NameObject("/Resources")] = resources
    return stream


def _make_annotation(
    writer: PdfWriter,
    page_ref,
    lines: list[str],
    x: float,
    y: float,
    font_size: float,
    colour: tuple[float, float, float],
    subject: str,
    tag: str | None,
) -> DictionaryObject:
    longest = max((len(l) for l in lines), default=0)
    width = max(48.0, longest * font_size * _HELV_AVG_ADVANCE + 10)
    height = len(lines) * font_size * 1.25 + 6

    annot = DictionaryObject()
    annot[NameObject("/Type")] = NameObject("/Annot")
    annot[NameObject("/Subtype")] = NameObject("/FreeText")
    annot[NameObject("/Rect")] = ArrayObject(
        [FloatObject(x), FloatObject(y), FloatObject(x + width), FloatObject(y + height)]
    )
    annot[NameObject("/Contents")] = TextStringObject("\n".join(lines))
    annot[NameObject("/T")] = TextStringObject(STAMP_AUTHOR)
    annot[NameObject("/Subj")] = TextStringObject(subject)
    annot[NameObject("/NM")] = TextStringObject(f"sce-{uuid.uuid4().hex[:16]}")
    annot[NameObject("/M")] = TextStringObject(_pdf_now())
    annot[NameObject("/CreationDate")] = TextStringObject(_pdf_now())
    annot[NameObject("/C")] = ArrayObject([FloatObject(c) for c in colour])
    annot[NameObject("/F")] = NumberObject(4)  # print
    annot[NameObject("/DA")] = TextStringObject(
        f"{colour[0]:.3f} {colour[1]:.3f} {colour[2]:.3f} rg /Helv {font_size:.2f} Tf"
    )
    annot[NameObject("/P")] = page_ref
    annot[NameObject(ORIGIN_KEY)] = TextStringObject(ORIGIN_VALUE)
    if tag:
        annot[NameObject(TAG_KEY)] = TextStringObject(tag)

    ap = DictionaryObject()
    ap[NameObject("/N")] = writer._add_object(
        _text_box_stream(writer, lines, width, height, font_size, colour)
    )
    annot[NameObject("/AP")] = ap
    return annot


def _annots_array(page) -> ArrayObject:
    if "/Annots" not in page:
        page[NameObject("/Annots")] = ArrayObject()
    annots = page["/Annots"]
    if hasattr(annots, "get_object") and not isinstance(annots, ArrayObject):
        annots = annots.get_object()
    return annots


def stamp_markups(
    pdf: bytes,
    items: list[StampItem],
    transform: PlanTransform | None = None,
    subject: str = "Design Data",
    font_size: float = 7.5,
) -> tuple[bytes, list[dict]]:
    """Place `items` onto `pdf` as FreeText markups. Returns (pdf, placements)."""
    try:
        writer = PdfWriter(clone_from=io.BytesIO(pdf))
    except PdfReadError as exc:
        raise StampError(f"target PDF is not readable: {exc}") from exc

    page_count = len(writer.pages)
    placements: list[dict] = []

    for item in items:
        if not 0 <= item.page < page_count:
            raise StampError(
                f"{item.label!r} targets page {item.page + 1}, but the set has "
                f"{page_count} page(s)"
            )
        x, y = item.resolve(transform)
        page = writer.pages[item.page]
        lines = [item.label] + list(item.detail)
        annot = _make_annotation(
            writer, page.indirect_reference, lines, x, y, font_size, item.colour,
            subject, item.tag,
        )
        ref = writer._add_object(annot)
        _annots_array(page).append(ref)
        placements.append({
            "label": item.label,
            "tag": item.tag,
            "page_label": item.page + 1,
            "page_x": round(x, 2),
            "page_y": round(y, 2),
            "from_model_coords": item.model_x is not None and item.page_x is None,
        })

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), placements


CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


def stamp_schedule(
    pdf: bytes,
    title: str,
    rows: list[list[str]],
    page: int = 0,
    corner: str = "top-right",
    margin: float = 24.0,
    font_size: float = 7.0,
    colour: tuple[float, float, float] = (0.1, 0.35, 0.7),
) -> tuple[bytes, dict]:
    """Drop a titled table onto one sheet — the no-coordinates-needed path.

    Rows are padded per column so the table reads as a grid in a proportional
    font without drawing rules for every cell.
    """
    if corner not in CORNERS:
        raise StampError(f"corner must be one of {', '.join(CORNERS)}")
    try:
        writer = PdfWriter(clone_from=io.BytesIO(pdf))
    except PdfReadError as exc:
        raise StampError(f"target PDF is not readable: {exc}") from exc
    if not 0 <= page < len(writer.pages):
        raise StampError(
            f"schedule targets page {page + 1}, but the set has {len(writer.pages)} page(s)"
        )

    widths: dict[int, int] = {}
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths.get(i, 0), len(str(cell)))
    lines = [title] + [
        "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) for row in rows
    ]

    longest = max((len(l) for l in lines), default=0)
    width = max(48.0, longest * font_size * _HELV_AVG_ADVANCE + 10)
    height = len(lines) * font_size * 1.25 + 6

    target = writer.pages[page]
    box = target.mediabox
    page_w, page_h = float(box.width), float(box.height)
    x = margin if "left" in corner else max(margin, page_w - width - margin)
    y = margin if "bottom" in corner else max(margin, page_h - height - margin)

    annot = _make_annotation(
        writer, target.indirect_reference, lines, x, y, font_size, colour,
        "Design Schedule", None,
    )
    _annots_array(target).append(writer._add_object(annot))

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), {
        "title": title,
        "page_label": page + 1,
        "corner": corner,
        "row_count": len(rows),
        "page_x": round(x, 2),
        "page_y": round(y, 2),
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
    }
