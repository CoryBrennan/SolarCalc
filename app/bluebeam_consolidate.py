"""Merging several separately marked-up copies of one drawing set onto a
single consolidated set.

The workflow this exists for: a drawing set goes out to four reviewers, each
opens their own copy in Revu, each marks it up, and four PDFs come back. Revu
itself has no way to combine those without a Studio Session — and a Studio
Session needs Studio Prime, which is exactly the subscription this
integration is built to not require. So the merge happens here.

**How the merge works.** Every markup is an annotation object living in a
page's `/Annots` array (see bluebeam_markup_io). Consolidating is therefore
not a page-image operation — nothing is flattened, rasterised, or redrawn.
Each reviewer's annotation objects are *cloned* into the master document,
appearance streams and all, and appended to the matching page's `/Annots`.
The result is a normal PDF whose markups stay live: still selectable in Revu,
still editable, still listed individually in the Markups List with their
original author and timestamp. A flattened merge would have destroyed all of
that, which is why it isn't done that way.

**Reviewers become PDF layers.** Each submission gets its own optional
content group (OCG), and every markup cloned from it carries `/OC` pointing
at that group. In Revu this surfaces as the Layers panel: the consolidator
can switch one reviewer's comments on and off over the base drawing. That is
the single most useful thing about a consolidated set, and it comes free from
a PDF primitive rather than from anything Bluebeam-specific.

**Two reviewers marking the same thing.** Handled by identity, not by
geometry. If two submissions carry a markup with the same `/NM` GUID, it is
genuinely one markup that was present in both copies before they diverged —
typically a prior round's comment that everyone's copy inherited — so it is
cloned once and recorded as `also_from`. Markups that merely *overlap* on the
page are left alone and both kept: two reviewers circling the same conduit
are two separate opinions, and silently dropping one would lose a real
review comment. `overlap_clusters` reports them for a human to look at
instead of guessing.

**The page-compatibility gate.** A merge is only meaningful if all
submissions are copies of the same set, so page count and page dimensions
must match the master. This is the same constraint Revu enforces on its own
markup import, and for the same reason: annotation coordinates are page
coordinates, so a markup from an 11x17 sheet lands somewhere meaningless on a
30x42 one. A mismatch raises rather than merging something wrong — with the
specific offending pages named, since the usual cause is one reviewer having
been sent a stale revision.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    TextStringObject,
)

from app.bluebeam_markup_io import (
    MARKUP_SUBTYPES,
    BluebeamMarkupError,
    Markup,
    extract_markups,
    page_geometry,
)

# Private key stamped onto every cloned markup recording which submission it
# came from. Bluebeam ignores keys it doesn't know, and bluebeam_markup_io
# sweeps unknown keys into Markup.custom -- so a consolidated set that is
# itself re-uploaded later still knows each markup's origin.
SOURCE_KEY = "/SolarCalcSource"

# Page dimensions are compared in points with a small tolerance. Distillers
# and plotters routinely differ in the last decimal on an identical sheet;
# half a point is far below any real sheet-size difference (the smallest
# genuine step, 11x17 to 12x18, is 72 points).
_SIZE_TOLERANCE_PT = 0.5


class ConsolidationError(Exception):
    """Raised when submissions can't be merged (page mismatch, unreadable file)."""


@dataclass
class Submission:
    """One reviewer's marked-up copy of the set, on the way in."""

    label: str  # reviewer or firm name -- becomes the PDF layer name
    data: bytes
    submission_id: str | None = None


@dataclass
class ConsolidationResult:
    pdf: bytes
    markups: list[Markup]
    page_count: int
    layers: list[str]
    duplicates: list[dict] = field(default_factory=list)
    overlap_clusters: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        by_source: dict[str, int] = {}
        for m in self.markups:
            by_source[m.source_label or "(unattributed)"] = (
                by_source.get(m.source_label or "(unattributed)", 0) + 1
            )
        return {
            "page_count": self.page_count,
            "markup_count": len(self.markups),
            "layers": self.layers,
            "markups_by_source": by_source,
            "duplicate_count": len(self.duplicates),
            "duplicates": self.duplicates,
            "overlap_cluster_count": len(self.overlap_clusters),
            "overlap_clusters": self.overlap_clusters,
            "skipped": self.skipped,
        }


def check_compatibility(master: bytes, submissions: list[Submission]) -> list[dict]:
    """Compare every submission's page grid against the master's.

    Returns a list of problem records (empty when everything lines up). Not
    raising here is deliberate: the HMI calls this before a merge to show the
    user what's wrong while there's still a chance to swap in the right file.
    """
    base_geo = page_geometry(master)
    problems: list[dict] = []
    for sub in submissions:
        try:
            geo = page_geometry(sub.data)
        except BluebeamMarkupError as exc:
            problems.append({"label": sub.label, "issue": "unreadable", "detail": str(exc)})
            continue
        if len(geo) != len(base_geo):
            problems.append({
                "label": sub.label,
                "issue": "page_count_mismatch",
                "detail": f"master has {len(base_geo)} pages, this set has {len(geo)}",
            })
            continue
        bad_pages = [
            {
                "page_label": i + 1,
                "master": f"{bw} x {bh} pt",
                "submission": f"{sw} x {sh} pt",
            }
            for i, ((bw, bh), (sw, sh)) in enumerate(zip(base_geo, geo))
            if abs(bw - sw) > _SIZE_TOLERANCE_PT or abs(bh - sh) > _SIZE_TOLERANCE_PT
        ]
        if bad_pages:
            problems.append({
                "label": sub.label,
                "issue": "page_size_mismatch",
                "detail": f"{len(bad_pages)} page(s) differ in size",
                "pages": bad_pages,
            })
    return problems


def _register_layer(writer: PdfWriter, name: str) -> IndirectObject:
    """Create an optional content group and register it on the catalog.

    Built by hand rather than through a pypdf helper because the annotation
    /OC linkage needs the OCG's indirect reference, and the catalog's
    /OCProperties has to accumulate one entry per reviewer across repeated
    calls.
    """
    ocg = DictionaryObject()
    ocg[NameObject("/Type")] = NameObject("/OCG")
    ocg[NameObject("/Name")] = TextStringObject(name)
    ref = writer._add_object(ocg)

    root = writer._root_object
    if "/OCProperties" not in root:
        props = DictionaryObject()
        props[NameObject("/OCGs")] = ArrayObject()
        default = DictionaryObject()
        default[NameObject("/Order")] = ArrayObject()
        default[NameObject("/ON")] = ArrayObject()
        props[NameObject("/D")] = default
        root[NameObject("/OCProperties")] = props

    props = root["/OCProperties"]
    props[NameObject("/OCGs")].append(ref)
    default = props["/D"]
    default[NameObject("/Order")].append(ref)
    default[NameObject("/ON")].append(ref)
    return ref


def _page_annots(page: DictionaryObject) -> ArrayObject:
    if "/Annots" not in page:
        page[NameObject("/Annots")] = ArrayObject()
    annots = page["/Annots"]
    # A page inherited from the base document can hold /Annots as an indirect
    # reference; append has to hit the resolved array, not the reference.
    if isinstance(annots, IndirectObject):
        annots = annots.get_object()
    return annots


def _rects_overlap(a: list[float], b: list[float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _find_overlaps(markups: list[Markup]) -> list[dict]:
    """Group markups from *different* reviewers that cover the same spot.

    Reported, never merged -- see the module docstring. Two markups from the
    same reviewer overlapping is just how a cloud plus its callout looks, so
    those are not flagged.
    """
    clusters: list[dict] = []
    by_page: dict[int, list[Markup]] = {}
    for m in markups:
        by_page.setdefault(m.page, []).append(m)

    for page, items in sorted(by_page.items()):
        used: set[int] = set()
        for i, a in enumerate(items):
            if i in used:
                continue
            group = [a]
            for j in range(i + 1, len(items)):
                if j in used:
                    continue
                b = items[j]
                if b.source_label == a.source_label:
                    continue
                if any(_rects_overlap(g.rect, b.rect) for g in group):
                    group.append(b)
                    used.add(j)
            if len(group) > 1:
                used.add(i)
                clusters.append({
                    "page_label": page + 1,
                    "sources": sorted({g.source_label or "(unattributed)" for g in group}),
                    "markups": [
                        {
                            "key": g.key,
                            "source_label": g.source_label,
                            "author": g.author,
                            "subject": g.subject,
                            "contents": g.contents,
                        }
                        for g in group
                    ],
                })
    return clusters


def consolidate(
    master: bytes,
    submissions: list[Submission],
    layer_per_source: bool = True,
) -> ConsolidationResult:
    """Clone every submission's markups onto `master` and return the merged PDF.

    `master` is the clean set the reviewers were all working from. Passing one
    of the marked-up copies instead works, but then that copy's markups arrive
    unattributed and unlayered, so the HMI always sends the clean set.
    """
    if not submissions:
        raise ConsolidationError("no submissions to consolidate")

    problems = check_compatibility(master, submissions)
    if problems:
        detail = "; ".join(
            f"{p['label']}: {p['detail']}" for p in problems
        )
        raise ConsolidationError(
            f"submissions don't match the master set -- {detail}. "
            "Every copy must be the same revision of the same drawing set "
            "(same page count and sheet sizes) before markups can be merged."
        )

    try:
        writer = PdfWriter(clone_from=io.BytesIO(master))
    except PdfReadError as exc:
        raise ConsolidationError(f"master set is not a readable PDF: {exc}") from exc

    all_markups: list[Markup] = []
    duplicates: list[dict] = []
    skipped: list[dict] = []
    layers: list[str] = []
    # /NM -> the label that first contributed it, so a repeat is recognisable
    # as the same markup rather than merged blindly.
    seen_nm: dict[str, str] = {}
    # /NM -> cloned reference, so a status reply can be re-pointed at whichever
    # copy of its parent actually made it into the master.
    cloned_by_nm: dict[str, IndirectObject] = {}

    for sub in submissions:
        try:
            reader = PdfReader(io.BytesIO(sub.data))
        except PdfReadError as exc:
            raise ConsolidationError(f"{sub.label}: not a readable PDF: {exc}") from exc

        layer_ref = _register_layer(writer, sub.label) if layer_per_source else None
        if layer_per_source:
            layers.append(sub.label)

        # Extraction is the source of truth for what counts as a markup, so
        # the merged file and the review table can never disagree about it.
        sub_markups, _ = extract_markups(sub.data, source_label=sub.label)
        keep_by_nm = {m.nm: m for m in sub_markups if m.nm}
        keep_fallback = [m for m in sub_markups if not m.nm]
        fallback_iter = iter(keep_fallback)

        pending_replies: list[tuple[DictionaryObject, str]] = []

        for page_index, page in enumerate(reader.pages):
            target = writer.pages[page_index]
            annots = _page_annots(target)

            for ref in page.get("/Annots") or []:
                try:
                    obj = ref.get_object()
                except Exception:  # pragma: no cover - dangling reference
                    continue
                if not isinstance(obj, DictionaryObject):
                    continue
                subtype = str(obj.get("/Subtype", ""))

                irt = obj.get("/IRT")
                state = obj.get("/State")
                is_reply = irt is not None and state is not None

                if not is_reply and subtype not in MARKUP_SUBTYPES:
                    continue

                nm = obj.get("/NM")
                nm = str(nm) if nm is not None else None

                if not is_reply:
                    if nm and nm in seen_nm:
                        duplicates.append({
                            "key": nm,
                            "page_label": page_index + 1,
                            "first_from": seen_nm[nm],
                            "also_from": sub.label,
                            "author": str(obj.get("/T")) if obj.get("/T") else None,
                            "contents": str(obj.get("/Contents")) if obj.get("/Contents") else None,
                        })
                        continue
                    markup = keep_by_nm.get(nm) if nm else next(fallback_iter, None)
                    if markup is None:  # pragma: no cover - extraction/clone divergence
                        skipped.append({
                            "label": sub.label,
                            "page_label": page_index + 1,
                            "reason": "annotation not matched to an extracted markup",
                        })
                        continue

                # /Popup is a viewer-regenerated companion window and /P is the
                # old document's page pointer -- both are meaningless here.
                # /IRT is dropped and re-linked below so cloning a reply does
                # not drag in a second copy of its parent.
                cloned = obj.clone(writer, ignore_fields=("/Popup", "/P", "/IRT"))
                cloned_ref = cloned.indirect_reference
                cloned[NameObject("/P")] = target.indirect_reference
                cloned[NameObject(SOURCE_KEY)] = TextStringObject(sub.label)
                if layer_ref is not None:
                    cloned[NameObject("/OC")] = layer_ref

                annots.append(cloned_ref)

                if is_reply:
                    irt_obj = irt.get_object() if isinstance(irt, IndirectObject) else irt
                    parent_nm = (
                        irt_obj.get("/NM") if isinstance(irt_obj, DictionaryObject) else None
                    )
                    if parent_nm is not None:
                        pending_replies.append((cloned, str(parent_nm)))
                    continue

                if nm:
                    seen_nm[nm] = sub.label
                    cloned_by_nm[nm] = cloned_ref
                all_markups.append(markup)

        # Re-link replies once every markup in this submission has landed --
        # a reply can appear before its parent in the /Annots array.
        for reply, parent_nm in pending_replies:
            parent_ref = cloned_by_nm.get(parent_nm)
            if parent_ref is not None:
                reply[NameObject("/IRT")] = parent_ref

    out = io.BytesIO()
    writer.write(out)

    return ConsolidationResult(
        pdf=out.getvalue(),
        markups=all_markups,
        page_count=len(writer.pages),
        layers=layers,
        duplicates=duplicates,
        overlap_clusters=_find_overlaps(all_markups),
        skipped=skipped,
    )
