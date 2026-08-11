"""SkyVisor integration endpoints: seed the module-level asset map (until a
real layout source -- e.g. PVCase piling data -- is wired in, these are
populated by hand or by an external script), export it for SkyVisor to
import, and ingest a SkyVisor anomaly export back against that asset map.

No public SkyVisor API/export docs exist yet (see app/skyvisor_export.py and
app/skyvisor_import.py docstrings), so this is deliberately file-based
(export a CSV, upload a CSV) rather than a live API integration -- swap to a
direct API call once SkyVisor's actual integration surface is confirmed.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app import skyvisor_export, skyvisor_import
from app.db import get_session
from app.db_models import ArrayTable, ModuleAsset, SkyvisorAnomaly, SkyvisorImportBatch, StringAsset

router = APIRouter(prefix="/skyvisor")


class ArrayTableCreate(BaseModel):
    block_tag: str
    row_label: str
    latitude: float
    longitude: float
    azimuth_deg: float | None = None


class StringAssetCreate(BaseModel):
    inverter_tag: str
    combiner_index: int | None = None
    mppt_index: int | None = None
    module_count: int
    module_sku: str


class ModuleAssetCreate(BaseModel):
    array_table_id: str
    string_asset_id: str
    position_in_string: int
    module_sku: str
    latitude: float
    longitude: float


def _batch_to_dict(batch: SkyvisorImportBatch) -> dict:
    return {
        "id": batch.id,
        "flight_date": batch.flight_date,
        "source_filename": batch.source_filename,
        "status": batch.status,
        "imported_at": batch.imported_at,
    }


def _anomaly_to_dict(anomaly: SkyvisorAnomaly) -> dict:
    return {
        "id": anomaly.id,
        "import_batch_id": anomaly.import_batch_id,
        "module_asset_id": anomaly.module_asset_id,
        "string_asset_id": anomaly.string_asset_id,
        "anomaly_type": anomaly.anomaly_type,
        "severity": anomaly.severity,
        "delta_t_c": anomaly.delta_t_c,
        "latitude": anomaly.latitude,
        "longitude": anomaly.longitude,
        "image_url": anomaly.image_url,
        "resolution_status": anomaly.resolution_status,
        "created_at": anomaly.created_at,
    }


@router.post("/array-tables/bulk")
def create_array_tables(tables: list[ArrayTableCreate], session: Session = Depends(get_session)) -> list[dict]:
    rows = [ArrayTable(**t.model_dump()) for t in tables]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return [{"id": r.id, "block_tag": r.block_tag, "row_label": r.row_label} for r in rows]


@router.post("/strings/bulk")
def create_string_assets(strings: list[StringAssetCreate], session: Session = Depends(get_session)) -> list[dict]:
    rows = [StringAsset(**s.model_dump()) for s in strings]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return [{"id": r.id, "inverter_tag": r.inverter_tag, "module_count": r.module_count} for r in rows]


@router.post("/modules/bulk")
def create_module_assets(modules: list[ModuleAssetCreate], session: Session = Depends(get_session)) -> list[dict]:
    known_tables = set(session.exec(select(ArrayTable.id)).all())
    known_strings = set(session.exec(select(StringAsset.id)).all())
    for m in modules:
        if m.array_table_id not in known_tables:
            raise HTTPException(status_code=422, detail=f"Unknown array_table_id: {m.array_table_id!r}")
        if m.string_asset_id not in known_strings:
            raise HTTPException(status_code=422, detail=f"Unknown string_asset_id: {m.string_asset_id!r}")

    rows = [ModuleAsset(**m.model_dump()) for m in modules]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return [{"id": r.id} for r in rows]


@router.get("/asset-map.csv", response_class=PlainTextResponse)
def export_asset_map(session: Session = Depends(get_session)) -> str:
    return skyvisor_export.build_asset_map_csv(session)


@router.post("/import")
async def import_anomalies(
    file: UploadFile = File(...),
    flight_date: str = Form(...),
    session: Session = Depends(get_session),
) -> dict:
    try:
        parsed_date = datetime.fromisoformat(flight_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"flight_date must be ISO 8601: {exc}") from exc

    data = await file.read()
    batch = skyvisor_import.import_batch(
        session,
        csv_text=data.decode("utf-8"),
        source_filename=file.filename or "unnamed",
        flight_date=parsed_date,
    )
    anomalies = session.exec(
        select(SkyvisorAnomaly).where(SkyvisorAnomaly.import_batch_id == batch.id)
    ).all()
    return {"batch": _batch_to_dict(batch), "anomalies": [_anomaly_to_dict(a) for a in anomalies]}


@router.get("/anomalies")
def list_anomalies(
    resolution_status: str | None = None,
    anomaly_type: str | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    statement = select(SkyvisorAnomaly)
    if resolution_status:
        statement = statement.where(SkyvisorAnomaly.resolution_status == resolution_status)
    if anomaly_type:
        statement = statement.where(SkyvisorAnomaly.anomaly_type == anomaly_type)
    statement = statement.order_by(SkyvisorAnomaly.created_at)
    return [_anomaly_to_dict(a) for a in session.exec(statement).all()]


@router.post("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly(anomaly_id: str, body: dict, session: Session = Depends(get_session)) -> dict:
    anomaly = session.get(SkyvisorAnomaly, anomaly_id)
    if anomaly is None:
        raise HTTPException(status_code=404, detail=f"No anomaly with id {anomaly_id!r}")

    resolution = body.get("resolution_status")
    if resolution not in ("resolved", "false_positive"):
        raise HTTPException(status_code=422, detail="resolution_status must be 'resolved' or 'false_positive'")

    anomaly.resolution_status = resolution
    session.add(anomaly)
    session.commit()
    session.refresh(anomaly)
    return _anomaly_to_dict(anomaly)
