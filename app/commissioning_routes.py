"""Field commissioning QC endpoints: inverter/switchboard/load-center
commissioning records, torque + visual/mechanical checks, wire + electrical
readings, and field photos — the rail 28 "Commissioning & QC" panel's
backend.

Unlike /calculate (stateless, in-memory ProjectInput), this is a real
persisted workflow the same way the changeset and SkyVisor systems are —
a field crew fills this in over hours or days, so it has to survive a page
reload. Follows skyvisor_routes.py's shape closely: plain APIRouter, Pydantic
request bodies distinct from the SQLModel tables, dict-serializing helpers
instead of returning ORM rows directly.

The panel groups its four child types into two sections, and the routes
below follow that grouping:
  - Visual & Mechanical Inspection: TorquePoint + InspectionItem
  - Electrical Inspection: WireInspectionItem + ElectricalReading

Two endpoints derive their rows from the project's own design data instead
of requiring manual entry — auto_populate_wire_items (matches Raceway (24)
runs by tag and reuses wire_cost_calc's NEC-compliant sizing) and
auto_populate_electrical_readings (derives an AC voltage band from
ProjectInput.inverter). Both are additive/idempotent: re-running a sync
updates rows it already created rather than duplicating them, and manual
add/edit/delete stays available alongside them for anything the auto-derive
can't reach (e.g. a raceway run that doesn't share the unit's tag).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app import commissioning_calc, wire_cost_calc
from app.db import get_session
from app.db_models import (
    CommissioningPhoto,
    CommissioningUnit,
    ElectricalReading,
    InspectionItem,
    TorquePoint,
    WireInspectionItem,
)
from app.models import ProjectInput

router = APIRouter(prefix="/commissioning")

_EQUIPMENT_TYPES = {"inverter", "switchboard", "load_center"}
_PHOTO_CATEGORIES = {"visual_mechanical", "electrical"}
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


class InspectionItemCreate(BaseModel):
    label: str


class InspectionItemUpdate(BaseModel):
    result: str | None = None  # "pass" | "fail" | "pending"
    notes: str | None = None


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


class ElectricalReadingCreate(BaseModel):
    label: str
    reading_type: str = "ac_voltage"
    design_min: float | None = None
    design_max: float | None = None
    unit: str = "VAC"


class ElectricalReadingUpdate(BaseModel):
    design_min: float | None = None
    design_max: float | None = None
    measured_value: float | None = None
    unit: str | None = None


class AutoPopulateWireItemsRequest(BaseModel):
    project: ProjectInput


class AutoPopulateElectricalReadingsRequest(BaseModel):
    project: ProjectInput
    tolerance_pct: float = commissioning_calc.DEFAULT_AC_VOLTAGE_TOLERANCE_PCT


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


def _unit_inspection_items(session: Session, unit_id: str) -> list[InspectionItem]:
    return list(session.exec(select(InspectionItem).where(InspectionItem.unit_id == unit_id).order_by(InspectionItem.created_at)).all())


def _unit_wire_items(session: Session, unit_id: str) -> list[WireInspectionItem]:
    return list(session.exec(select(WireInspectionItem).where(WireInspectionItem.unit_id == unit_id).order_by(WireInspectionItem.created_at)).all())


def _unit_electrical_readings(session: Session, unit_id: str) -> list[ElectricalReading]:
    return list(session.exec(select(ElectricalReading).where(ElectricalReading.unit_id == unit_id).order_by(ElectricalReading.created_at)).all())


def _unit_photos(session: Session, unit_id: str) -> list[CommissioningPhoto]:
    return list(session.exec(select(CommissioningPhoto).where(CommissioningPhoto.unit_id == unit_id).order_by(CommissioningPhoto.uploaded_at)).all())


def _summarize(session: Session, unit_id: str) -> dict:
    return commissioning_calc.summarize_unit(
        [t.result for t in _unit_torque_points(session, unit_id)],
        [i.result for i in _unit_inspection_items(session, unit_id)],
        [w.result for w in _unit_wire_items(session, unit_id)],
        [e.result for e in _unit_electrical_readings(session, unit_id)],
    )


def _recompute_unit_status(session: Session, unit_id: str) -> dict:
    """Re-derives CommissioningUnit.status from its current children —
    called after every child write so status never drifts out of sync with
    what's actually been checked (see CommissioningUnit's docstring)."""
    unit = _get_unit_or_404(session, unit_id)
    summary = _summarize(session, unit_id)
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


def _inspection_item_to_dict(i: InspectionItem) -> dict:
    return {
        "id": i.id,
        "unit_id": i.unit_id,
        "label": i.label,
        "notes": i.notes,
        "result": i.result,
        "checked_at": i.checked_at,
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


def _electrical_reading_to_dict(e: ElectricalReading) -> dict:
    return {
        "id": e.id,
        "unit_id": e.unit_id,
        "label": e.label,
        "reading_type": e.reading_type,
        "design_min": e.design_min,
        "design_max": e.design_max,
        "unit": e.unit,
        "measured_value": e.measured_value,
        "result": e.result,
        "checked_at": e.checked_at,
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
    """Checklist-label starting points for the Visual & Mechanical panel's
    "quick add" -- see commissioning_calc.DEFAULT_TORQUE_CHECKLIST and
    DEFAULT_VISUAL_MECHANICAL_CHECKLIST's docstrings for why neither carries
    values, just labels."""
    _require_equipment_type(equipment_type)
    return {
        "equipment_type": equipment_type,
        "torque_labels": commissioning_calc.DEFAULT_TORQUE_CHECKLIST[equipment_type],
        "visual_mechanical_labels": commissioning_calc.DEFAULT_VISUAL_MECHANICAL_CHECKLIST[equipment_type],
    }


@router.post("/units")
def create_unit(body: UnitCreate, session: Session = Depends(get_session)) -> dict:
    _require_equipment_type(body.equipment_type)
    unit = CommissioningUnit(**body.model_dump())
    session.add(unit)
    session.commit()
    session.refresh(unit)
    empty = {"total": 0, "pass": 0, "fail": 0, "pending": 0}
    return _unit_to_dict(unit, {"visual_mechanical": empty, "electrical": dict(empty), "overall": "not_started"})


@router.get("/units")
def list_units(equipment_type: str | None = None, status: str | None = None, session: Session = Depends(get_session)) -> list[dict]:
    statement = select(CommissioningUnit)
    if equipment_type:
        statement = statement.where(CommissioningUnit.equipment_type == equipment_type)
    if status:
        statement = statement.where(CommissioningUnit.status == status)
    statement = statement.order_by(CommissioningUnit.created_at)
    units = session.exec(statement).all()
    return [_unit_to_dict(unit, _summarize(session, unit.id)) for unit in units]


@router.get("/units/{unit_id}")
def get_unit(unit_id: str, session: Session = Depends(get_session)) -> dict:
    unit = _get_unit_or_404(session, unit_id)
    return {
        **_unit_to_dict(unit, _summarize(session, unit_id)),
        "torque_points": [_torque_point_to_dict(t) for t in _unit_torque_points(session, unit_id)],
        "inspection_items": [_inspection_item_to_dict(i) for i in _unit_inspection_items(session, unit_id)],
        "wire_items": [_wire_item_to_dict(w) for w in _unit_wire_items(session, unit_id)],
        "electrical_readings": [_electrical_reading_to_dict(e) for e in _unit_electrical_readings(session, unit_id)],
        "photos": [_photo_to_dict(p) for p in _unit_photos(session, unit_id)],
    }


@router.patch("/units/{unit_id}")
def update_unit(unit_id: str, body: UnitUpdate, session: Session = Depends(get_session)) -> dict:
    unit = _get_unit_or_404(session, unit_id)
    updates = body.model_dump(exclude_unset=True)

    if "commissioned_by" in updates and updates["commissioned_by"]:
        summary = _summarize(session, unit_id)
        if summary["overall"] != "complete":
            raise HTTPException(status_code=422, detail=f"Cannot sign off unit {unit_id!r}: status is {summary['overall']!r}, not every item has a pass result yet.")
        unit.commissioned_at = _now()

    for field, value in updates.items():
        setattr(unit, field, value)
    session.add(unit)
    session.commit()
    session.refresh(unit)
    return _unit_to_dict(unit, _summarize(session, unit_id))


@router.delete("/units/{unit_id}")
def delete_unit(unit_id: str, session: Session = Depends(get_session)) -> dict:
    """Hard delete, cascading to every child row -- unlike Changeset/
    IngestionJob, a commissioning unit isn't an audit trail of automated
    work; it's just as likely to be test/demo data (created while trying the
    panel out) as a real field record, so there's a real need to remove one
    cleanly rather than keep it around forever."""
    unit = _get_unit_or_404(session, unit_id)
    for photo in _unit_photos(session, unit_id):
        session.delete(photo)
    for point in _unit_torque_points(session, unit_id):
        session.delete(point)
    for item in _unit_inspection_items(session, unit_id):
        session.delete(item)
    for wire in _unit_wire_items(session, unit_id):
        session.delete(wire)
    for reading in _unit_electrical_readings(session, unit_id):
        session.delete(reading)
    session.delete(unit)
    session.commit()
    return {"deleted": True, "id": unit_id}


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


@router.delete("/torque-points/{point_id}")
def delete_torque_point(point_id: str, session: Session = Depends(get_session)) -> dict:
    point = session.get(TorquePoint, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"No torque point with id {point_id!r}")
    unit_id = point.unit_id
    session.delete(point)
    session.commit()
    _recompute_unit_status(session, unit_id)
    return {"deleted": True, "id": point_id}


@router.post("/units/{unit_id}/inspection-items")
def create_inspection_items(unit_id: str, items: list[InspectionItemCreate], session: Session = Depends(get_session)) -> list[dict]:
    _get_unit_or_404(session, unit_id)
    rows = [InspectionItem(unit_id=unit_id, **i.model_dump()) for i in items]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    _recompute_unit_status(session, unit_id)
    return [_inspection_item_to_dict(r) for r in rows]


@router.patch("/inspection-items/{item_id}")
def update_inspection_item(item_id: str, body: InspectionItemUpdate, session: Session = Depends(get_session)) -> dict:
    item = session.get(InspectionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No inspection item with id {item_id!r}")

    updates = body.model_dump(exclude_unset=True)
    if "result" in updates:
        if updates["result"] not in ("pending", "pass", "fail", None):
            raise HTTPException(status_code=422, detail="result must be one of: pending, pass, fail")
        if updates["result"] is None:
            updates["result"] = "pending"
    for field, value in updates.items():
        setattr(item, field, value)
    if item.result != "pending":
        item.checked_at = _now()

    session.add(item)
    session.commit()
    session.refresh(item)
    _recompute_unit_status(session, item.unit_id)
    return _inspection_item_to_dict(item)


@router.delete("/inspection-items/{item_id}")
def delete_inspection_item(item_id: str, session: Session = Depends(get_session)) -> dict:
    item = session.get(InspectionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No inspection item with id {item_id!r}")
    unit_id = item.unit_id
    session.delete(item)
    session.commit()
    _recompute_unit_status(session, unit_id)
    return {"deleted": True, "id": item_id}


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


@router.delete("/wire-items/{item_id}")
def delete_wire_item(item_id: str, session: Session = Depends(get_session)) -> dict:
    item = session.get(WireInspectionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No wire inspection item with id {item_id!r}")
    unit_id = item.unit_id
    session.delete(item)
    session.commit()
    _recompute_unit_status(session, unit_id)
    return {"deleted": True, "id": item_id}


@router.post("/units/{unit_id}/wire-items/auto-populate")
def auto_populate_wire_items(unit_id: str, body: AutoPopulateWireItemsRequest, session: Session = Depends(get_session)) -> dict:
    """Populates wire-inspection rows straight from the project's own
    Raceway (24) schedule instead of the engineer typing each circuit in by
    hand -- see commissioning_calc.match_raceway_runs_for_tag for the tag
    match rule and wire_cost_calc.evaluate_feeder_value_engineering for the
    NEC-compliant sizing this reuses (same code the Wire Value Engineering
    (27) panel runs, with FeederVeSettings() defaults since this endpoint
    has no per-run settings UI of its own). Idempotent: a run tag that
    already has a wire item gets its design_conductor refreshed (and result
    re-graded) rather than duplicated, so re-syncing after a raceway change
    is safe to click again."""
    unit = _get_unit_or_404(session, unit_id)
    matches = commissioning_calc.match_raceway_runs_for_tag(body.project.raceway_runs, unit.tag)
    ambient_c = body.project.ashrae.max_design_temp_c
    existing_by_label = {w.circuit_label: w for w in _unit_wire_items(session, unit_id)}

    created: list[WireInspectionItem] = []
    updated: list[WireInspectionItem] = []
    skipped_tags: list[str] = []

    for run in matches:
        settings = wire_cost_calc.FeederVeSettings()
        scenario = wire_cost_calc.feeder_scenario_from_raceway_run(run, settings, ambient_c)
        ve_result = wire_cost_calc.evaluate_feeder_value_engineering(scenario)
        recommended = ve_result.get("recommended")
        if not recommended:
            skipped_tags.append(run.tag)
            continue

        design_conductor = f"{recommended['phase_conductor']} {recommended['material']}"
        existing = existing_by_label.get(run.tag)
        if existing:
            existing.design_conductor = design_conductor
            existing.result = commissioning_calc.score_wire_item(
                design_conductor=design_conductor,
                as_built_conductor=existing.as_built_conductor,
                termination_ok=existing.termination_ok,
                labeling_ok=existing.labeling_ok,
                continuity_ok=existing.continuity_ok,
                insulation_resistance_megohm=existing.insulation_resistance_megohm,
            )
            session.add(existing)
            updated.append(existing)
        else:
            item = WireInspectionItem(unit_id=unit_id, circuit_label=run.tag, design_conductor=design_conductor)
            session.add(item)
            created.append(item)

    session.commit()
    for row in created + updated:
        session.refresh(row)
    _recompute_unit_status(session, unit_id)

    return {
        "matched_run_count": len(matches),
        "created": [_wire_item_to_dict(r) for r in created],
        "updated": [_wire_item_to_dict(r) for r in updated],
        "skipped_tags": skipped_tags,
    }


@router.post("/units/{unit_id}/electrical-readings")
def create_electrical_readings(unit_id: str, readings: list[ElectricalReadingCreate], session: Session = Depends(get_session)) -> list[dict]:
    _get_unit_or_404(session, unit_id)
    rows = [ElectricalReading(unit_id=unit_id, **r.model_dump()) for r in readings]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    _recompute_unit_status(session, unit_id)
    return [_electrical_reading_to_dict(r) for r in rows]


@router.patch("/electrical-readings/{reading_id}")
def update_electrical_reading(reading_id: str, body: ElectricalReadingUpdate, session: Session = Depends(get_session)) -> dict:
    reading = session.get(ElectricalReading, reading_id)
    if reading is None:
        raise HTTPException(status_code=404, detail=f"No electrical reading with id {reading_id!r}")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(reading, field, value)

    reading.result = commissioning_calc.score_measurement_band(reading.design_min, reading.design_max, reading.measured_value)
    if reading.result != "pending":
        reading.checked_at = _now()

    session.add(reading)
    session.commit()
    session.refresh(reading)
    _recompute_unit_status(session, reading.unit_id)
    return _electrical_reading_to_dict(reading)


@router.delete("/electrical-readings/{reading_id}")
def delete_electrical_reading(reading_id: str, session: Session = Depends(get_session)) -> dict:
    reading = session.get(ElectricalReading, reading_id)
    if reading is None:
        raise HTTPException(status_code=404, detail=f"No electrical reading with id {reading_id!r}")
    unit_id = reading.unit_id
    session.delete(reading)
    session.commit()
    _recompute_unit_status(session, unit_id)
    return {"deleted": True, "id": reading_id}


@router.post("/units/{unit_id}/electrical-readings/auto-populate")
def auto_populate_electrical_readings(unit_id: str, body: AutoPopulateElectricalReadingsRequest, session: Session = Depends(get_session)) -> dict:
    """Populates AC line-to-line voltage checkpoints from the project's own
    nominal_ac_voltage_v/phases (ProjectInput.inverter) -- see
    commissioning_calc.derive_ac_voltage_readings. Meaningful for inverter
    and switchboard units (both operate at that same AC bus voltage); a
    load_center's voltage is a free-text field on AuxPanelboardConfig (e.g.
    "120/240V") with no reliable numeric value to derive a design band
    from, so this is a no-op there -- add electrical readings by hand for a
    load center instead. Idempotent the same way auto_populate_wire_items
    is: a label that already exists gets its design band refreshed rather
    than duplicated."""
    unit = _get_unit_or_404(session, unit_id)
    if unit.equipment_type not in ("inverter", "switchboard"):
        return {"derived_count": 0, "created": [], "updated": [], "note": "No numeric AC voltage source for this equipment type -- add electrical readings manually."}

    derived = commissioning_calc.derive_ac_voltage_readings(
        body.project.inverter.nominal_ac_voltage_v, body.project.inverter.phases, body.tolerance_pct
    )
    existing_by_label = {e.label: e for e in _unit_electrical_readings(session, unit_id)}

    created: list[ElectricalReading] = []
    updated: list[ElectricalReading] = []
    for row in derived:
        existing = existing_by_label.get(row["label"])
        if existing:
            existing.design_min = row["design_min"]
            existing.design_max = row["design_max"]
            existing.result = commissioning_calc.score_measurement_band(row["design_min"], row["design_max"], existing.measured_value)
            session.add(existing)
            updated.append(existing)
        else:
            reading = ElectricalReading(unit_id=unit_id, **row)
            session.add(reading)
            created.append(reading)

    session.commit()
    for row in created + updated:
        session.refresh(row)
    _recompute_unit_status(session, unit_id)

    return {
        "derived_count": len(derived),
        "created": [_electrical_reading_to_dict(r) for r in created],
        "updated": [_electrical_reading_to_dict(r) for r in updated],
    }


@router.post("/units/{unit_id}/photos")
async def upload_photo(
    unit_id: str,
    file: UploadFile = File(...),
    category: str = Form("visual_mechanical"),
    caption: str | None = Form(None),
    torque_point_id: str | None = Form(None),
    wire_item_id: str | None = Form(None),
    session: Session = Depends(get_session),
) -> dict:
    _get_unit_or_404(session, unit_id)
    if category not in _PHOTO_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of {sorted(_PHOTO_CATEGORIES)}")

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
    return [_photo_to_dict(p) for p in _unit_photos(session, unit_id)]


@router.get("/photos/{photo_id}")
def get_photo(photo_id: str, session: Session = Depends(get_session)) -> Response:
    photo = session.get(CommissioningPhoto, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail=f"No photo with id {photo_id!r}")
    return Response(content=photo.file_data, media_type=photo.content_type)


@router.delete("/photos/{photo_id}")
def delete_photo(photo_id: str, session: Session = Depends(get_session)) -> dict:
    photo = session.get(CommissioningPhoto, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail=f"No photo with id {photo_id!r}")
    session.delete(photo)
    session.commit()
    return {"deleted": True, "id": photo_id}
