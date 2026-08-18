"""Bluebeam plan-review endpoints: the rail 30 "Plan Review & Markups"
panel's backend.

Two flows, both file-based and neither needing a Bluebeam Studio Prime
subscription (see app/bluebeam_markup_io's module docstring for why that
works at all):

  1. **Inbound.** Create a review set from the clean master drawing set, upload
     each reviewer's marked-up copy, consolidate them all onto one set with a
     PDF layer per reviewer, then work the resulting markup list through
     disposition to sign-off.
  2. **Outbound.** Stamp the engine's own computed design data onto a plan set
     as live Revu markups (`/stamp`, `/stamp-schedule`).

Follows skyvisor_routes.py's shape: plain APIRouter, Pydantic bodies distinct
from the SQLModel tables, dict-serialising helpers rather than returning ORM
rows. Persistence matters here for the same reason it does in commissioning —
a review runs over days across several people, so it has to survive a reload.

**File size and where bytes live.** A full plan set is routinely far larger
than the 15 MB commissioning photos cap, so both the master and each
submission can be given as a `source_path` into the Dropbox-synced project
folder instead of an upload, exactly as pvcase_bom_import and
fluke_export_import already do. Upload puts the bytes in the DB row; a path
stores only the path and re-reads on demand. Paths keep the DB small but only
resolve on the machine that can see them, which for a plan set is the
engineer's own — the same split already documented for DWG scanning.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app import bluebeam_consolidate, bluebeam_review, bluebeam_stamp
from app.bluebeam_consolidate import ConsolidationError, Submission
from app.bluebeam_markup_io import (
    BluebeamMarkupError,
    Markup,
    extract_markups,
    markups_to_csv,
    page_geometry,
)
from app.bluebeam_review import ReviewStateError
from app.bluebeam_stamp import ControlPoint, PlanTransform, StampError, StampItem
from app.db import get_session
from app.db_models import MarkupAudit, MarkupItem, MarkupSubmission, PlanReviewSet

router = APIRouter(prefix="/bluebeam")

# Uploads land in a DB row, so this is a guard against a 300 MB plan set
# quietly becoming a 300 MB blob. Anything larger should come in by
# source_path instead, which is what the panel tells the user.
MAX_UPLOAD_BYTES = 150 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------

class ReviewSetCreate(BaseModel):
    name: str
    revision_label: str | None = None
    discipline: str | None = None
    master_source_path: str | None = None


class DispositionUpdate(BaseModel):
    disposition: str
    actor: str | None = None
    response: str | None = None
    assigned_to: str | None = None


class BulkDispositionUpdate(BaseModel):
    markup_item_ids: list[str]
    disposition: str
    actor: str | None = None
    response: str | None = None


class ApproveRequest(BaseModel):
    approved_by: str
    note: str | None = None


class ControlPointIn(BaseModel):
    model_x: float
    model_y: float
    page_x: float
    page_y: float


class StampItemIn(BaseModel):
    label: str
    page: int = 0
    tag: str | None = None
    detail: list[str] = []
    model_x: float | None = None
    model_y: float | None = None
    page_x: float | None = None
    page_y: float | None = None


class StampRequest(BaseModel):
    """Stamp design data onto a plan set held on the local filesystem.

    Local path in, local path out — a plan set is too big to round-trip
    through the browser, and it already lives in the project folder.
    """

    pdf_path: str
    output_path: str
    items: list[StampItemIn]
    control_points: list[ControlPointIn] | None = None
    subject: str = "Design Data"
    font_size: float = 7.5


class StampScheduleRequest(BaseModel):
    pdf_path: str
    output_path: str
    title: str
    rows: list[list[str]]
    page: int = 0
    corner: str = "top-right"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _read_source_path(path_str: str) -> bytes:
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"file not found: {path_str}")
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"not a file: {path_str}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not read {path_str}: {exc}") from exc


def _master_bytes(review_set: PlanReviewSet) -> bytes:
    if review_set.master_data is not None:
        return review_set.master_data
    if review_set.master_source_path:
        return _read_source_path(review_set.master_source_path)
    raise HTTPException(
        status_code=400,
        detail=(
            f"review set {review_set.id} has no master drawing set; upload one or "
            "set master_source_path before consolidating"
        ),
    )


def _submission_bytes(sub: MarkupSubmission) -> bytes:
    if sub.file_data is not None:
        return sub.file_data
    if sub.source_path:
        return _read_source_path(sub.source_path)
    raise HTTPException(
        status_code=400, detail=f"submission {sub.id} has neither uploaded bytes nor a source path"
    )


def _get_set_or_404(session: Session, review_set_id: str) -> PlanReviewSet:
    rs = session.get(PlanReviewSet, review_set_id)
    if rs is None:
        raise HTTPException(status_code=404, detail=f"review set {review_set_id} not found")
    return rs


def _get_item_or_404(session: Session, item_id: str) -> MarkupItem:
    item = session.get(MarkupItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"markup item {item_id} not found")
    return item


def _set_to_dict(rs: PlanReviewSet, session: Session | None = None) -> dict:
    out = {
        "id": rs.id,
        "name": rs.name,
        "revision_label": rs.revision_label,
        "discipline": rs.discipline,
        "master_filename": rs.master_filename,
        "master_source_path": rs.master_source_path,
        "has_master_bytes": rs.master_data is not None,
        "page_count": rs.page_count,
        "current_round": rs.current_round,
        "status": rs.status,
        "has_consolidated_pdf": rs.consolidated_data is not None,
        "consolidated_at": rs.consolidated_at,
        "approved_by": rs.approved_by,
        "approved_at": rs.approved_at,
        "approval_note": rs.approval_note,
        "created_at": rs.created_at,
        "updated_at": rs.updated_at,
    }
    if session is not None:
        items = _current_round_items(session, rs)
        out["summary"] = bluebeam_review.rollup([_item_to_dict(i) for i in items])
        out["submission_count"] = len(
            session.exec(
                select(MarkupSubmission).where(MarkupSubmission.review_set_id == rs.id)
            ).all()
        )
    return out


def _submission_to_dict(sub: MarkupSubmission) -> dict:
    return {
        "id": sub.id,
        "review_set_id": sub.review_set_id,
        "label": sub.label,
        "round_number": sub.round_number,
        "filename": sub.filename,
        "source_path": sub.source_path,
        "page_count": sub.page_count,
        "markup_count": sub.markup_count,
        "uploaded_by": sub.uploaded_by,
        "uploaded_at": sub.uploaded_at,
    }


def _item_to_dict(item: MarkupItem) -> dict:
    return {
        "id": item.id,
        "review_set_id": item.review_set_id,
        "submission_id": item.submission_id,
        "round_number": item.round_number,
        "key": item.markup_key,
        "fingerprint": item.fingerprint,
        "source_label": item.source_label,
        "also_from": json.loads(item.also_from_json) if item.also_from_json else [],
        "page": item.page,
        "page_label": item.page + 1,
        "subtype": item.subtype,
        "author": item.author,
        "subject": item.subject,
        "contents": item.contents,
        "colour": item.colour,
        "rect": json.loads(item.rect_json) if item.rect_json else None,
        "custom": json.loads(item.custom_json) if item.custom_json else {},
        "revu_status": item.revu_status,
        "equipment_tag": item.equipment_tag,
        "change": item.change,
        "disposition": item.disposition,
        "response": item.response,
        "assigned_to": item.assigned_to,
        "resolved_by": item.resolved_by,
        "resolved_at": item.resolved_at,
        "created_at": item.markup_created_at,
        "modified_at": item.markup_modified_at,
    }


def _current_round_items(session: Session, rs: PlanReviewSet) -> list[MarkupItem]:
    return list(
        session.exec(
            select(MarkupItem)
            .where(MarkupItem.review_set_id == rs.id)
            .where(MarkupItem.round_number == rs.current_round)
            .order_by(MarkupItem.page, MarkupItem.source_label)
        ).all()
    )


def _round_markups(session: Session, review_set_id: str, round_number: int) -> list[Markup]:
    """Rebuild Markup objects from stored rows, for bluebeam_review's diffing."""
    rows = session.exec(
        select(MarkupItem)
        .where(MarkupItem.review_set_id == review_set_id)
        .where(MarkupItem.round_number == round_number)
    ).all()
    out = []
    for r in rows:
        out.append(
            Markup(
                key=r.markup_key,
                page=r.page,
                subtype=r.subtype or "",
                author=r.author,
                subject=r.subject,
                contents=r.contents,
                colour=r.colour,
                rect=json.loads(r.rect_json) if r.rect_json else [0.0, 0.0, 0.0, 0.0],
                created_at=r.markup_created_at,
                modified_at=r.markup_modified_at,
                status=r.revu_status,
                nm=r.markup_key if not r.markup_key.startswith("h:") else None,
                source_label=r.source_label,
                custom=json.loads(r.custom_json) if r.custom_json else {},
            )
        )
    return out


def _touch(session: Session, rs: PlanReviewSet) -> None:
    rs.updated_at = _now()
    session.add(rs)


# --------------------------------------------------------------------------
# review sets
# --------------------------------------------------------------------------

@router.post("/review-sets")
def create_review_set(body: ReviewSetCreate, session: Session = Depends(get_session)) -> dict:
    rs = PlanReviewSet(
        name=body.name,
        revision_label=body.revision_label,
        discipline=body.discipline,
        master_source_path=body.master_source_path,
    )
    if body.master_source_path:
        data = _read_source_path(body.master_source_path)
        try:
            rs.page_count = len(page_geometry(data))
        except BluebeamMarkupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        rs.master_filename = Path(body.master_source_path).name
    session.add(rs)
    session.commit()
    session.refresh(rs)
    return _set_to_dict(rs, session)


@router.get("/review-sets")
def list_review_sets(
    status: str | None = None, session: Session = Depends(get_session)
) -> list[dict]:
    stmt = select(PlanReviewSet).order_by(PlanReviewSet.created_at.desc())
    if status:
        stmt = stmt.where(PlanReviewSet.status == status)
    return [_set_to_dict(rs, session) for rs in session.exec(stmt).all()]


@router.get("/review-sets/{review_set_id}")
def get_review_set(review_set_id: str, session: Session = Depends(get_session)) -> dict:
    rs = _get_set_or_404(session, review_set_id)
    subs = session.exec(
        select(MarkupSubmission)
        .where(MarkupSubmission.review_set_id == rs.id)
        .order_by(MarkupSubmission.round_number, MarkupSubmission.uploaded_at)
    ).all()
    items = _current_round_items(session, rs)
    item_dicts = [_item_to_dict(i) for i in items]
    return {
        **_set_to_dict(rs, session),
        "submissions": [_submission_to_dict(s) for s in subs],
        "markups": item_dicts,
        "approval": bluebeam_review.approval_gate(item_dicts),
    }


@router.delete("/review-sets/{review_set_id}")
def delete_review_set(review_set_id: str, session: Session = Depends(get_session)) -> dict:
    rs = _get_set_or_404(session, review_set_id)
    items = session.exec(
        select(MarkupItem).where(MarkupItem.review_set_id == review_set_id)
    ).all()
    for item in items:
        for audit in session.exec(
            select(MarkupAudit).where(MarkupAudit.markup_item_id == item.id)
        ).all():
            session.delete(audit)
        session.delete(item)
    for sub in session.exec(
        select(MarkupSubmission).where(MarkupSubmission.review_set_id == review_set_id)
    ).all():
        session.delete(sub)
    session.delete(rs)
    session.commit()
    return {"deleted": review_set_id, "markup_items_deleted": len(items)}


@router.post("/review-sets/{review_set_id}/master")
async def upload_master(
    review_set_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> dict:
    """Attach the clean, unmarked drawing set the reviewers were issued."""
    rs = _get_set_or_404(session, review_set_id)
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{file.filename} is {len(data) / 1e6:.0f} MB, over the "
                f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB upload limit. Set "
                "master_source_path to a local file path instead."
            ),
        )
    try:
        geo = page_geometry(data)
    except BluebeamMarkupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rs.master_data = data
    rs.master_filename = file.filename
    rs.master_source_path = None
    rs.page_count = len(geo)
    _touch(session, rs)
    session.commit()
    session.refresh(rs)
    return _set_to_dict(rs, session)


# --------------------------------------------------------------------------
# submissions
# --------------------------------------------------------------------------

def _ingest_submission(
    session: Session,
    rs: PlanReviewSet,
    label: str,
    data: bytes,
    round_number: int,
    filename: str | None,
    source_path: str | None,
    uploaded_by: str | None,
) -> dict:
    try:
        markups, page_count = extract_markups(data, source_label=label)
    except BluebeamMarkupError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: {exc}") from exc

    if rs.page_count is not None and page_count != rs.page_count:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{label}'s set has {page_count} pages but the master has "
                f"{rs.page_count}. Markup coordinates are page coordinates, so "
                "merging sets with different page grids would place comments on "
                "the wrong sheets. Check the reviewer was issued this revision."
            ),
        )

    sub = MarkupSubmission(
        review_set_id=rs.id,
        label=label,
        round_number=round_number,
        filename=filename,
        source_path=source_path,
        file_data=data if source_path is None else None,
        page_count=page_count,
        markup_count=len(markups),
        uploaded_by=uploaded_by,
    )
    session.add(sub)
    session.flush()

    # Carry dispositions forward from the previous round before the new rows
    # are written, so an unchanged comment doesn't reappear as undecided.
    carried: dict[str, dict] = {}
    if round_number > 1:
        previous = _round_markups(session, rs.id, round_number - 1)
        if previous:
            diff = bluebeam_review.diff_rounds(previous, markups)
            prior_dispositions = {
                r.markup_key: {
                    "disposition": r.disposition,
                    "response": r.response,
                    "assigned_to": r.assigned_to,
                }
                for r in session.exec(
                    select(MarkupItem)
                    .where(MarkupItem.review_set_id == rs.id)
                    .where(MarkupItem.round_number == round_number - 1)
                ).all()
            }
            carried = bluebeam_review.carry_forward_dispositions(diff, prior_dispositions)
            change_by_key = {
                row["key"]: row["change"]
                for row in diff.added + diff.modified + diff.unchanged
            }
        else:
            change_by_key = {}
    else:
        change_by_key = {m.key: "added" for m in markups}

    # A markup already recorded in this round arrived in someone else's copy
    # too -- inherited from a prior round, so every issued copy carries it.
    # Record the extra source on the existing row instead of creating a second
    # one, keeping the review table's markup count equal to the consolidated
    # set's (bluebeam_consolidate merges the same /NM once for the same reason).
    existing_in_round = {
        r.markup_key: r
        for r in session.exec(
            select(MarkupItem)
            .where(MarkupItem.review_set_id == rs.id)
            .where(MarkupItem.round_number == round_number)
        ).all()
    }

    duplicate_of_other_submission = 0
    for m in markups:
        already = existing_in_round.get(m.key)
        if already is not None:
            also = json.loads(already.also_from_json) if already.also_from_json else []
            if label not in also and label != already.source_label:
                also.append(label)
                already.also_from_json = json.dumps(also)
                already.updated_at = _now()
                session.add(already)
            duplicate_of_other_submission += 1
            continue

        prior = carried.get(m.key, {})
        session.add(
            MarkupItem(
                review_set_id=rs.id,
                submission_id=sub.id,
                round_number=round_number,
                markup_key=m.key,
                fingerprint=m.content_fingerprint(),
                source_label=label,
                page=m.page,
                subtype=m.subtype.lstrip("/"),
                author=m.author,
                subject=m.subject,
                contents=m.contents,
                colour=m.colour,
                rect_json=json.dumps(m.rect),
                custom_json=json.dumps(m.custom, default=str),
                revu_status=m.status,
                equipment_tag=m.custom.get(bluebeam_stamp.TAG_KEY),
                markup_created_at=m.created_at,
                markup_modified_at=m.modified_at,
                change=change_by_key.get(m.key, "added"),
                disposition=prior.get("disposition", bluebeam_review.OPEN),
                response=prior.get("response"),
                assigned_to=prior.get("assigned_to"),
            )
        )

    if round_number > rs.current_round:
        rs.current_round = round_number
    if rs.page_count is None:
        rs.page_count = page_count
    # New markups invalidate an approval; the set goes back to open.
    if rs.status == "approved":
        rs.status = "open"
        rs.approved_by = None
        rs.approved_at = None
    _touch(session, rs)
    session.commit()
    session.refresh(sub)
    return {
        **_submission_to_dict(sub),
        "carried_forward": len(carried),
        "already_recorded_from_another_copy": duplicate_of_other_submission,
    }


@router.post("/review-sets/{review_set_id}/submissions")
async def upload_submission(
    review_set_id: str,
    label: str = Form(...),
    round_number: int = Form(1),
    uploaded_by: str | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> dict:
    """Upload one reviewer's marked-up copy of the set."""
    rs = _get_set_or_404(session, review_set_id)
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{file.filename} is {len(data) / 1e6:.0f} MB, over the "
                f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB upload limit. Use the "
                "local-path form instead."
            ),
        )
    return _ingest_submission(
        session, rs, label, data, round_number, file.filename, None, uploaded_by
    )


class SubmissionByPath(BaseModel):
    label: str
    source_path: str
    round_number: int = 1
    uploaded_by: str | None = None


@router.post("/review-sets/{review_set_id}/submissions-by-path")
def add_submission_by_path(
    review_set_id: str, body: SubmissionByPath, session: Session = Depends(get_session)
) -> dict:
    """Register a marked-up copy that already sits in the project folder."""
    rs = _get_set_or_404(session, review_set_id)
    data = _read_source_path(body.source_path)
    return _ingest_submission(
        session, rs, body.label, data, body.round_number,
        Path(body.source_path).name, body.source_path, body.uploaded_by,
    )


@router.delete("/submissions/{submission_id}")
def delete_submission(submission_id: str, session: Session = Depends(get_session)) -> dict:
    sub = session.get(MarkupSubmission, submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"submission {submission_id} not found")
    items = session.exec(
        select(MarkupItem).where(MarkupItem.submission_id == submission_id)
    ).all()
    for item in items:
        for audit in session.exec(
            select(MarkupAudit).where(MarkupAudit.markup_item_id == item.id)
        ).all():
            session.delete(audit)
        session.delete(item)
    session.delete(sub)
    session.commit()
    return {"deleted": submission_id, "markup_items_deleted": len(items)}


# --------------------------------------------------------------------------
# consolidation
# --------------------------------------------------------------------------

@router.post("/review-sets/{review_set_id}/check-compatibility")
def check_compatibility(
    review_set_id: str, round_number: int | None = None, session: Session = Depends(get_session)
) -> dict:
    """Dry run the page-grid check before merging, so a stale revision is
    caught while there's still time to swap the file."""
    rs = _get_set_or_404(session, review_set_id)
    round_no = round_number or rs.current_round
    subs = session.exec(
        select(MarkupSubmission)
        .where(MarkupSubmission.review_set_id == rs.id)
        .where(MarkupSubmission.round_number == round_no)
    ).all()
    if not subs:
        raise HTTPException(status_code=400, detail=f"no submissions in round {round_no}")
    problems = bluebeam_consolidate.check_compatibility(
        _master_bytes(rs),
        [Submission(label=s.label, data=_submission_bytes(s), submission_id=s.id) for s in subs],
    )
    return {
        "review_set_id": rs.id,
        "round_number": round_no,
        "submission_count": len(subs),
        "compatible": not problems,
        "problems": problems,
    }


@router.post("/review-sets/{review_set_id}/consolidate")
def consolidate_round(
    review_set_id: str,
    round_number: int | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Merge every submission in a round onto one set, one PDF layer each."""
    rs = _get_set_or_404(session, review_set_id)
    round_no = round_number or rs.current_round
    subs = session.exec(
        select(MarkupSubmission)
        .where(MarkupSubmission.review_set_id == rs.id)
        .where(MarkupSubmission.round_number == round_no)
        .order_by(MarkupSubmission.uploaded_at)
    ).all()
    if not subs:
        raise HTTPException(status_code=400, detail=f"no submissions in round {round_no}")

    try:
        result = bluebeam_consolidate.consolidate(
            _master_bytes(rs),
            [
                Submission(label=s.label, data=_submission_bytes(s), submission_id=s.id)
                for s in subs
            ],
        )
    except ConsolidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    rs.consolidated_data = result.pdf
    rs.consolidated_at = _now()
    if rs.status == "open":
        rs.status = "consolidated"
    _touch(session, rs)
    session.commit()

    return {
        "review_set_id": rs.id,
        "round_number": round_no,
        "download_url": f"/bluebeam/review-sets/{rs.id}/consolidated.pdf",
        "size_bytes": len(result.pdf),
        **result.summary(),
    }


@router.get("/review-sets/{review_set_id}/consolidated.pdf")
def download_consolidated(review_set_id: str, session: Session = Depends(get_session)) -> Response:
    rs = _get_set_or_404(session, review_set_id)
    if rs.consolidated_data is None:
        raise HTTPException(
            status_code=404, detail="no consolidated set yet — run /consolidate first"
        )
    stem = (rs.name or "review-set").replace(" ", "_")
    suffix = f"_{rs.revision_label.replace(' ', '_')}" if rs.revision_label else ""
    return Response(
        content=rs.consolidated_data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}{suffix}_consolidated.pdf"'
        },
    )


# --------------------------------------------------------------------------
# dispositions + approval
# --------------------------------------------------------------------------

def _apply_disposition(
    session: Session,
    item: MarkupItem,
    disposition: str,
    actor: str | None,
    response: str | None,
    assigned_to: str | None = None,
) -> MarkupItem:
    try:
        bluebeam_review.validate_transition(item.disposition, disposition)
    except ReviewStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    effective_response = response if response is not None else item.response
    if bluebeam_review.requires_response(disposition) and not (effective_response or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                f"a {disposition} disposition needs a written response — the "
                "reviewer is owed a reason, and it's what the audit trail records"
            ),
        )

    previous = item.disposition
    response_changed = response is not None and response != item.response

    item.disposition = disposition
    if response is not None:
        item.response = response
    if assigned_to is not None:
        item.assigned_to = assigned_to
    if disposition in bluebeam_review.TERMINAL_DISPOSITIONS:
        # Re-asserting the same terminal disposition shouldn't rewrite when it
        # was resolved -- that timestamp is the audit answer to "when was this
        # closed out", not "when was this row last touched".
        if previous != disposition:
            item.resolved_by = actor
            item.resolved_at = _now()
    else:
        item.resolved_by = None
        item.resolved_at = None
    item.updated_at = _now()
    session.add(item)

    # Only real changes go in the trail. A bulk action that sweeps up rows
    # already in the target disposition would otherwise fill the history with
    # "incorporated -> incorporated" entries and bury the decisions that
    # actually happened.
    if previous != disposition or response_changed:
        session.add(
            MarkupAudit(
                markup_item_id=item.id,
                from_disposition=previous,
                to_disposition=disposition,
                actor=actor,
                note=response,
            )
        )
    return item


@router.patch("/markups/{item_id}/disposition")
def set_disposition(
    item_id: str, body: DispositionUpdate, session: Session = Depends(get_session)
) -> dict:
    item = _get_item_or_404(session, item_id)
    _apply_disposition(
        session, item, body.disposition, body.actor, body.response, body.assigned_to
    )
    rs = session.get(PlanReviewSet, item.review_set_id)
    if rs is not None and rs.status == "approved":
        # A decision changing after sign-off has to unwind the sign-off.
        rs.status = "consolidated" if rs.consolidated_data is not None else "open"
        rs.approved_by = None
        rs.approved_at = None
        _touch(session, rs)
    session.commit()
    session.refresh(item)
    return _item_to_dict(item)


@router.post("/review-sets/{review_set_id}/dispositions/bulk")
def bulk_disposition(
    review_set_id: str, body: BulkDispositionUpdate, session: Session = Depends(get_session)
) -> dict:
    """Disposition many markups at once — the common case when a whole
    reviewer's batch is accepted together."""
    rs = _get_set_or_404(session, review_set_id)
    updated, failed = [], []
    for item_id in body.markup_item_ids:
        item = session.get(MarkupItem, item_id)
        if item is None or item.review_set_id != rs.id:
            failed.append({"id": item_id, "error": "not found in this review set"})
            continue
        try:
            _apply_disposition(session, item, body.disposition, body.actor, body.response)
            updated.append(item.id)
        except HTTPException as exc:
            failed.append({"id": item_id, "error": exc.detail})
    session.commit()
    return {"updated": updated, "updated_count": len(updated), "failed": failed}


@router.get("/markups/{item_id}/audit")
def markup_audit(item_id: str, session: Session = Depends(get_session)) -> list[dict]:
    _get_item_or_404(session, item_id)
    rows = session.exec(
        select(MarkupAudit)
        .where(MarkupAudit.markup_item_id == item_id)
        .order_by(MarkupAudit.at)
    ).all()
    return [
        {
            "id": r.id,
            "from_disposition": r.from_disposition,
            "to_disposition": r.to_disposition,
            "actor": r.actor,
            "note": r.note,
            "at": r.at,
        }
        for r in rows
    ]


@router.get("/review-sets/{review_set_id}/approval")
def approval_status(review_set_id: str, session: Session = Depends(get_session)) -> dict:
    rs = _get_set_or_404(session, review_set_id)
    items = [_item_to_dict(i) for i in _current_round_items(session, rs)]
    return {
        "review_set_id": rs.id,
        "status": rs.status,
        "approved_by": rs.approved_by,
        "approved_at": rs.approved_at,
        **bluebeam_review.approval_gate(items),
    }


@router.post("/review-sets/{review_set_id}/approve")
def approve_set(
    review_set_id: str, body: ApproveRequest, session: Session = Depends(get_session)
) -> dict:
    """Sign off a review set. Refused while the gate reports blockers."""
    rs = _get_set_or_404(session, review_set_id)
    items = [_item_to_dict(i) for i in _current_round_items(session, rs)]
    gate = bluebeam_review.approval_gate(items)
    if not gate["ready_to_approve"]:
        if gate["empty"]:
            raise HTTPException(
                status_code=422,
                detail="nothing to approve — this review set has no markups in the current round",
            )
        raise HTTPException(
            status_code=422,
            detail=(
                f"{gate['blocker_count']} markup(s) still block sign-off: "
                + "; ".join(f"{b['count']} {b['disposition']} ({b['reason']})" for b in gate["blockers"])
            ),
        )
    rs.status = "approved"
    rs.approved_by = body.approved_by
    rs.approved_at = _now()
    rs.approval_note = body.note
    _touch(session, rs)
    session.commit()
    session.refresh(rs)
    return _set_to_dict(rs, session)


# --------------------------------------------------------------------------
# round-over-round diff + summary export
# --------------------------------------------------------------------------

@router.get("/review-sets/{review_set_id}/rounds/{round_number}/diff")
def round_diff(
    review_set_id: str, round_number: int, session: Session = Depends(get_session)
) -> dict:
    """What changed between round N-1 and round N."""
    rs = _get_set_or_404(session, review_set_id)
    if round_number < 2:
        raise HTTPException(
            status_code=400,
            detail="a diff needs a prior round; round 1 is the baseline",
        )
    previous = _round_markups(session, rs.id, round_number - 1)
    current = _round_markups(session, rs.id, round_number)
    if not previous and not current:
        raise HTTPException(
            status_code=404, detail=f"no markups stored for rounds {round_number - 1} or {round_number}"
        )
    return {
        "review_set_id": rs.id,
        "from_round": round_number - 1,
        "to_round": round_number,
        **bluebeam_review.diff_rounds(previous, current).to_dict(),
    }


@router.get("/review-sets/{review_set_id}/markups.csv")
def markups_csv(
    review_set_id: str,
    round_number: int | None = None,
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    """Markup summary as CSV — Revu's own columns plus the disposition,
    assignee and response columns Revu has no concept of."""
    rs = _get_set_or_404(session, review_set_id)
    round_no = round_number or rs.current_round
    rows = session.exec(
        select(MarkupItem)
        .where(MarkupItem.review_set_id == rs.id)
        .where(MarkupItem.round_number == round_no)
        .order_by(MarkupItem.page, MarkupItem.source_label)
    ).all()
    payload = []
    for r in rows:
        d = _item_to_dict(r)
        d["review_disposition"] = d.pop("disposition")
        d["status"] = d.pop("revu_status")
        payload.append(d)
    stem = (rs.name or "review-set").replace(" ", "_")
    return PlainTextResponse(
        content=markups_to_csv(payload),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}_r{round_no}_markups.csv"'
        },
    )


# --------------------------------------------------------------------------
# outbound: stamping design data onto a plan set
# --------------------------------------------------------------------------

def _build_transform(points: list[ControlPointIn] | None) -> PlanTransform | None:
    if not points:
        return None
    if len(points) != 2:
        raise HTTPException(
            status_code=422,
            detail=(
                f"calibration needs exactly 2 control points, got {len(points)}. "
                "A similarity transform (scale, rotation, translation) is fully "
                "determined by two — see app/bluebeam_stamp for why it isn't an "
                "affine fit over more."
            ),
        )
    try:
        return PlanTransform.from_control_points(
            ControlPoint(**points[0].model_dump()), ControlPoint(**points[1].model_dump())
        )
    except StampError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _write_output(path_str: str, data: bytes) -> dict:
    path = Path(path_str)
    if not path.parent.exists():
        raise HTTPException(
            status_code=400, detail=f"output folder does not exist: {path.parent}"
        )
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"could not write {path_str}: {exc}") from exc
    return {"output_path": str(path), "size_bytes": len(data)}


@router.post("/stamp")
def stamp_design_data(body: StampRequest) -> dict:
    """Write equipment tags and conductor/conduit callouts onto a plan set."""
    pdf = _read_source_path(body.pdf_path)
    transform = _build_transform(body.control_points)
    items = [
        StampItem(
            label=i.label, page=i.page, tag=i.tag, detail=i.detail,
            model_x=i.model_x, model_y=i.model_y, page_x=i.page_x, page_y=i.page_y,
        )
        for i in body.items
    ]
    try:
        out, placements = bluebeam_stamp.stamp_markups(
            pdf, items, transform=transform, subject=body.subject, font_size=body.font_size
        )
    except StampError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    written = _write_output(body.output_path, out)
    return {
        **written,
        "stamped_count": len(placements),
        "placements": placements,
        "calibration": transform.to_dict() if transform else None,
        "validation_gate": (
            "Open the output in Bluebeam Revu and confirm the callouts land where "
            "expected before issuing it. Placement depends entirely on the two "
            "control points supplied; a mis-picked control point produces a set "
            "that is plausibly wrong rather than obviously wrong."
        ),
    }


@router.post("/stamp-schedule")
def stamp_schedule(body: StampScheduleRequest) -> dict:
    """Drop a titled table on a sheet — no coordinate calibration needed."""
    pdf = _read_source_path(body.pdf_path)
    try:
        out, info = bluebeam_stamp.stamp_schedule(
            pdf, body.title, body.rows, page=body.page, corner=body.corner
        )
    except StampError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**_write_output(body.output_path, out), **info}
