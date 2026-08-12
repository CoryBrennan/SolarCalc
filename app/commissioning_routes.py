"""Field commissioning QC endpoints: inverter/switchboard/load-center
commissioning records, torque checks, wire inspections, and field photos —
the rail 28 "Commissioning & QC" panel's backend.

Unlike /calculate (stateless, in-memory ProjectInput), this is a real
persisted workflow the same way the changeset and SkyVisor systems are —
a field crew fills this in over hours or days, so it has to survive a page
reload. Follows skyvisor_routes.py's shape closely: plain APIRouter, Pydantic
request bodies distinct from the SQLModel tables, dict-serializing helpers
instead of returning ORM rows directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app import commissioning_calc
from app.db import get_session
from app.db_models import CommissioningPhoto, CommissioningUnit, TorquePoint, WireInspectionItem

router = APIRouter(prefix="/commissioning")

_EQUIPMENT_TYPES = {"inverter", "switchboard", "load_center"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UnitCreate(BaseModel):
    equipment_type: str
    tag: str
    manufacturer: str | None = None
    model: str | None = None
    notes: str | None = None


class UnitUpdate(BaseModel):
    manufacturer: str | None = None
    model: str | None = None
    notes: str | None = None
    commissioned_by: str | None = None  # setting this signs off the unit — requires overall == "complete"


class TorquePointCreate(BaseModel):
    connection_label: str
    design_torque_min: float | None = None
    design_torque_max: float | None = None
    torque_unit: str = "ft-lb"


class TorquePointUpdate(BaseModel):
    design_torque_min: float | None = None
    design_torque_max: float | None = None
    torque_unit: str | None = None
    measured_torque_value: float | None = None
    wrench_id: str | None = None
    tech_initials: str | None = None


class WireItemCreate(BaseModel):
    circuit_label: str
    design_conductor: str


class WireItemUpdate(BaseModel):
    as_built_conductor: str | None = None
    termination_ok: bool | None = None
    labeling_ok: bool | None = None
    continuity_ok: bool | None = None
    insulation_resistance_megohm: float | None = None
    min_insulation_resistance_megohm: float = commissioning_calc.DEFAULT_MIN_INSULATION_RESISTANCE_MEGOHM
    notes: str | None = None


def _require_equipment_type(equipment_type: str) -> None:
    if equipment_type not in _EQUIPMENT_TYPES:
        raise HTTPException(status_code=422, detail=f"equipment_type must be one of {sorted(_EQUIPMENT_TYPES)}, got {equipment_type!r}")


def _get_unit_or_404(session: Session, unit_id: str) -> CommissioningUnit:
    unit = session.get(CommissioningUnit, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail=f"No commissioning unit with id {unit_id!r}")
    return unit


def _unit_torque_points(session: Session, unit_id: str) -> list[TorquePoint]:
    return list(session.exec(select(TorquePoint).where(TorquePoint.unit_id == unit_id).order_by(TorquePoint.created_at)).all())


def _unit_wire_items(session: Session, unit_id: str) -> list[WireInspectionItem]:
    return list(session.exec(select(WireInspectionItem).where(WireInspectionItem.unit_id == unit_id).order_by(WireInspectionItem.created_at)).all())


def _recompute_unit_status(session: Session, unit_id: str) -> dict:
    """Re-derives CommissioningUnit.status from its current children —
    called after every torque-point/wire-item write so status never drifts
    out of sync with what's actually been checked (see CommissioningUnit's
    docstring)."""
    unit = _get_unit_or_404(session, unit_id)
    torque_points = _unit_torque_points(session, unit_id)
    wire_items = _unit_wire_items(session, unit_id)
    summary = commissioning_calc.summarize_unit(
        [t.result for t in torque_points], [w.result for w in wire_items]
    )
    if unit.status != summary["overall"]:
        unit.status = summary["overall"]
        if unit.status != "complete":
            unit.commissioned_by = None
            unit.commissioned_at = None
        session.add(unit)
        session.commit()
    return summary


def _unit_to_dict(unit: CommissioningUnit, summary: dict | None = None) -> dict:
    return {
        "id": unit.id,
        "equipment_type": unit.equipment_type,
        "tag": unit.tag,
        "manufacturer": unit.manufacturer,
        "model": unit.model,
        "status": unit.status,
        "commissioned_by": unit.commissioned_by,
        "commissioned_at": unit.commissioned_at,
        "notes": unit.notes,
        "created_at": unit.created_at,
        "updated_at": unit.updated_at,
        **({"summary": summary} if summary is not None else {}),
    }


def _torque_point_to_dict(t: TorquePoint) -> dict:
    return {
        "id": t.id,
        "unit_id": t.unit_id,
        "connection_label": t.connection_label,
        "design_torque_min": t.design_torque_min,
        "design_torque_max": t.design_torque_max,
        "torque_unit": t.torque_unit,
        "measured_torque_value": t.measured_torque_value,
        "wrench_id": t.wrench_id,
        "tech_initials": t.tech_initials,
        "result": t.result,
        "checked_at": t.checked_at,
    }


def _wire_item_to_dict(w: WireInspectionItem) -> dict:
    return {
        "id": w.id,
        "unit_id": w.unit_id,
        "circuit_label": w.circuit_label,
        "design_conductor": w.design_conductor,
        "as_built_conductor": w.as_built_conductor,
        "termination_ok": w.termination_ok,
        "labeling_ok": w.labeling_ok,
        "continuity_ok": w.continuity_ok,
        "insulation_resistance_megohm": w.insulation_resistance_megohm,
        "notes": w.notes,
        "result": w.result,
        "checked_at": w.checked_at,
    }


def _photo_to_dict(p: CommissioningPhoto) -> dict:
    return {
        "id": p.id,
        "unit_id": p.unit_id,
        "category": p.category,
        "torque_point_id": p.torque_point_id,
        "wire_item_id": p.wire_item_id,
        "caption": p.caption,
        "filename": p.filename,
        "content_type": p.content_type,
        "uploaded_at": p.uploaded_at,
        "size_bytes": len(p.file_data),
    }


@router.get("/checklist-templates")
def get_checklist_template(equipment_type: str) -> dict:
    """Connection-label starting points for the Torque panel's "quick add"
    -- see commissioning_calc.DEFAULT_TORQUE_CHECKLIST's docstring for why
    this has no torque values in it."""
    _require_equipment_type(equipment_type)
    return {"equipment_type": equipment_type, "connection_labels": commissioning_calc.DEFAULT_TORQUE_CHECKLIST[equipment_type]}


@router.post("/units")
def create_unit(body: UnitCreate, session: Session = Depends(get_session)) -> dict:
    _require_equipment_type(body.equipment_type)
    unit = CommissioningUnit(**body.model_dump())
    session.add(unit)
    session.commit()
    session.refresh(unit)
    return _unit_to_dict(unit, {"torque": {"total": 0, "pass": 0, "fail": 0, "pending": 0}, "wire": {"total": 0, "pass": 0, "fail": 0, "pending": 0}, "overall": "not_started"})


@router.get("/units")
def list_units(equipment_type: str | None = None, status: str | None = None, session: Session = Depends(get_session)) -> list[dict]:
    statement = select(CommissioningUnit)
    if equipment_type:
        statement = statement.where(CommissioningUnit.equipment_type == equipment_type)
    if status:
        statement = statement.where(CommissioningUnit.status == status)
    statement = statement.order_by(CommissioningUnit.created_at)
    units = session.exec(statement).all()
    result = []
    for unit in units:
        summary = commissioning_calc.summarize_unit(
            [t.result for t in _unit_torque_points(session, unit.id)],
            [w.result for w in _unit_wire_items(session, unit.id)],
        )
        result.append(_unit_to_dict(unit, summary))
    return result


@router.get("/units/{unit_id}")
def get_unit(unit_id: str, session: Session = Depends(get_session)) -> dict:
    unit = _get_unit_or_404(session, unit_id)
    torque_points = _unit_torque_points(session, unit_id)
    wire_items = _unit_wire_items(session, unit_id)
    photos = session.exec(select(CommissioningPhoto).where(CommissioningPhoto.unit_id == unit_id).order_by(CommissioningPhoto.uploaded_at)).all()
    summary = commissioning_calc.summarize_unit([t.result for t in torque_points], [w.result for w in wire_items])
    return {
        **_unit_to_dict(unit, summary),
        "torque_points": [_torque_point_to_dict(t) for t in torque_points],
        "wire_items": [_wire_item_to_dict(w) for w in wire_items],
        "photos": [_photo_to_dict(p) for p in photos],
    }


@router.patch("/units/{unit_id}")
def update_unit(unit_id: str, body: UnitUpdate, session: Session = Depends(get_session)) -> dict:
    unit = _get_unit_or_404(session, unit_id)
    updates = body.model_dump(exclude_unset=True)

    if "commissioned_by" in updates and updates["commissioned_by"]:
        summary = commissioning_calc.summarize_unit(
            [t.result for t in _unit_torque_points(session, unit_id)],
            [w.result for w in _unit_wire_items(session, unit_id)],
        )
        if summary["overall"] != "complete":
            raise HTTPException(status_code=422, detail=f"Cannot sign off unit {unit_id!r}: status is {summary['overall']!r}, not every torque/wire item has a pass result yet.")
        unit.commissioned_at = _now()

    for field, value in updates.items():
        setattr(unit, field, value)
    session.add(unit)
    session.commit()
    session.refresh(unit)
    summary = commissioning_calc.summarize_unit(
        [t.result for t in _unit_torque_points(session, unit_id)],
        [w.result for w in _unit_wire_items(session, unit_id)],
    )
    return _unit_to_dict(unit, summary)


@router.post("/units/{unit_id}/torque-points")
def create_torque_points(unit_id: str, points: list[TorquePointCreate], session: Session = Depends(get_session)) -> list[dict]:
    _get_unit_or_404(session, unit_id)
    rows = [TorquePoint(unit_id=unit_id, **p.model_dump()) for p in points]
    for row in rows:
        row.result = commissioning_calc.score_torque_point(row.design_torque_min, row.design_torque_max, row.measured_torque_value)
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    _recompute_unit_status(session, unit_id)
    return [_torque_point_to_dict(r) for r in rows]


@router.patch("/torque-points/{point_id}")
def update_torque_point(point_id: str, body: TorquePointUpdate, session: Session = Depends(get_session)) -> dict:
    point = session.get(TorquePoint, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"No torque point with id {point_id!r}")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(point, field, value)

    point.result = commissioning_calc.score_torque_point(point.design_torque_min, point.design_torque_max, point.measured_torque_value)
    if point.result != "pending":
        point.checked_at = _now()

    session.add(point)
    session.commit()
    session.refresh(point)
    _recompute_unit_status(session, point.unit_id)
    return _torque_point_to_dict(point)


@router.post("/units/{unit_id}/wire-items")
def create_wire_items(unit_id: str, items: list[WireItemCreate], session: Session = Depends(get_session)) -> list[dict]:
    _get_unit_or_404(session, unit_id)
    rows = [WireInspectionItem(unit_id=unit_id, **i.model_dump()) for i in items]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    _recompute_unit_status(session, unit_id)
    return [_wire_item_to_dict(r) for r in rows]


@router.patch("/wire-items/{item_id}")
def update_wire_item(item_id: str, body: WireItemUpdate, session: Session = Depends(get_session)) -> dict:
    item = session.get(WireInspectionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No wire inspection item with id {item_id!r}")

    updates = body.model_dump(exclude_unset=True)
    min_megohm = updates.pop("min_insulation_resistance_megohm", commissioning_calc.DEFAULT_MIN_INSULATION_RESISTANCE_MEGOHM)
    for field, value in updates.items():
        setattr(item, field, value)

    item.result = commissioning_calc.score_wire_item(
        design_conductor=item.design_conductor,
        as_built_conductor=item.as_built_conductor,
        termination_ok=item.termination_ok,
        labeling_ok=item.labeling_ok,
        continuity_ok=item.continuity_ok,
        insulation_resistance_megohm=item.insulation_resistance_megohm,
        min_insulation_resistance_megohm=min_megohm,
    )
    if item.result != "pending":
        item.checked_at = _now()

    session.add(item)
    session.commit()
    session.refresh(item)
    _recompute_unit_status(session, item.unit_id)
    return _wire_item_to_dict(item)


@router.post("/units/{unit_id}/photos")
async def upload_photo(
    unit_id: str,
    file: UploadFile = File(...),
    category: str = Form("general"),
    caption: str | None = Form(None),
    torque_point_id: str | None = Form(None),
    wire_item_id: str | None = Form(None),
    session: Session = Depends(get_session),
) -> dict:
    _get_unit_or_404(session, unit_id)
    if category not in ("torque", "wiring", "nameplate", "general"):
        raise HTTPException(status_code=422, detail="category must be one of: torque, wiring, nameplate, general")

    data = await file.read()
    if len(data) > _MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Photo exceeds 15 MB")

    photo = CommissioningPhoto(
        unit_id=unit_id,
        category=category,
        torque_point_id=torque_point_id,
        wire_item_id=wire_item_id,
        caption=caption,
        filename=file.filename or "photo",
        content_type=file.content_type or "application/octet-stream",
        file_data=data,
    )
    session.add(photo)
    session.commit()
    session.refresh(photo)
    return _photo_to_dict(photo)


@router.get("/units/{unit_id}/photos")
def list_photos(unit_id: str, session: Session = Depends(get_session)) -> list[dict]:
    _get_unit_or_404(session, unit_id)
    photos = session.exec(select(CommissioningPhoto).where(CommissioningPhoto.unit_id == unit_id).order_by(CommissioningPhoto.uploaded_at)).all()
    return [_photo_to_dict(p) for p in photos]


@router.get("/photos/{photo_id}")
def get_photo(photo_id: str, session: Session = Depends(get_session)) -> Response:
    photo = session.get(CommissioningPhoto, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail=f"No photo with id {photo_id!r}")
    return Response(content=photo.file_data, media_type=photo.content_type)
