"""Datasheet ingestion pipeline: upload router, extraction-agent orchestration,
merge into a versioned catalog record, and the review/approve/reject queue.

Pipeline (POST /ingest/upload): route by extension -> extraction agent
(PDF only; .PAN/.OND have no parser yet, see NOTE below) -> merge into a
pending_review CatalogVersion, reusing an existing pending draft for the
same manufacturer/model if one exists (partial-coverage: a second document,
e.g. an O&M manual, filling gaps a datasheet left) -> engineer reviews and
approves or rejects.

NOTE on .PAN/.OND: no parser for these exists anywhere in this codebase
today (confirmed against the whole repo before writing this module). Rather
than guess at its output shape, uploads of these types are marked
needs_attention with a clear message. catalog_merge.merge_catalog_fields
already accepts an optional Source A dict, so wiring in a real parser later
means populating that argument here, not rewriting the merge logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app import catalog_merge
from app.catalog_merge import CatalogMergeError, FieldValue
from app.catalog_models import CatalogDefault, CatalogVersion, IngestionJob
from app.db import get_session
from app.extraction_agent import ExtractionAgentError, call_extraction_agent
from app.extraction_schema import ExtractionAgentResponse, flatten_inverter_fields, flatten_module_variant
from app.pdf_extract import PdfExtractionError, extract_pdf_text

router = APIRouter()

MAX_RETRIES_BEFORE_NEEDS_ATTENTION = 5
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ("pdf", "pan", "ond"):
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {ext!r}")
    return ext


def _load_fields(version: CatalogVersion) -> dict[str, FieldValue]:
    raw = json.loads(version.fields)
    return {name: FieldValue.model_validate(value) for name, value in raw.items()}


def _dump_fields(fields: dict[str, FieldValue]) -> str:
    return json.dumps({name: fv.model_dump() for name, fv in fields.items()}, sort_keys=True)


def _find_pending_draft(session: Session, equipment_type: str, manufacturer: str, model: str) -> CatalogVersion | None:
    statement = select(CatalogVersion).where(
        CatalogVersion.equipment_type == equipment_type,
        CatalogVersion.manufacturer == manufacturer,
        CatalogVersion.model == model,
        CatalogVersion.status == "pending_review",
    )
    return session.exec(statement).first()


def _find_latest_active(session: Session, equipment_type: str, manufacturer: str, model: str) -> CatalogVersion | None:
    statement = (
        select(CatalogVersion)
        .where(
            CatalogVersion.equipment_type == equipment_type,
            CatalogVersion.manufacturer == manufacturer,
            CatalogVersion.model == model,
            CatalogVersion.status == "active",
        )
        .order_by(CatalogVersion.approved_at.desc())
    )
    return session.exec(statement).first()


def _upsert_draft(
    session: Session, equipment_type: str, manufacturer: str, model: str, field_names: list[str], source_b: dict
) -> CatalogVersion:
    draft = _find_pending_draft(session, equipment_type, manufacturer, model)
    if draft is not None:
        baseline = _load_fields(draft)
    else:
        active = _find_latest_active(session, equipment_type, manufacturer, model)
        baseline = _load_fields(active) if active is not None else None

    merged = catalog_merge.merge_catalog_fields(field_names, None, source_b, baseline)

    if draft is not None:
        draft.fields = _dump_fields(merged)
        draft.updated_at = _now()
    else:
        draft = CatalogVersion(
            equipment_type=equipment_type,
            manufacturer=manufacturer,
            model=model,
            status="pending_review",
            fields=_dump_fields(merged),
        )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def _job_to_dict(job: IngestionJob) -> dict:
    return {
        "job_id": job.id,
        "filename": job.filename,
        "file_type": job.file_type,
        "equipment_type": job.equipment_type,
        "status": job.status,
        "retry_count": job.retry_count,
        "last_error": job.last_error,
        "catalog_version_id": job.catalog_version_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _version_to_dict(version: CatalogVersion) -> dict:
    return {
        "version_id": version.id,
        "equipment_type": version.equipment_type,
        "manufacturer": version.manufacturer,
        "model": version.model,
        "status": version.status,
        "fields": json.loads(version.fields),
        "created_at": version.created_at,
        "updated_at": version.updated_at,
        "approved_at": version.approved_at,
    }


def _mark_needs_attention(session: Session, job: IngestionJob, error: str) -> None:
    job.status = "needs_attention"
    job.retry_count += 1
    job.last_error = error
    job.updated_at = _now()
    session.add(job)
    session.commit()
    session.refresh(job)


def _process_pdf_job(session: Session, job: IngestionJob) -> list[CatalogVersion]:
    try:
        extract_pdf_text(job.file_data)
    except PdfExtractionError as exc:
        _mark_needs_attention(session, job, f"not a readable PDF: {exc}")
        return []

    try:
        response: ExtractionAgentResponse = call_extraction_agent(job.file_data)
    except ExtractionAgentError as exc:
        _mark_needs_attention(session, job, str(exc))
        return []

    try:
        catalog_merge.check_document_type_match(job.equipment_type, response)
    except CatalogMergeError as exc:
        _mark_needs_attention(session, job, str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    manufacturer = job.manufacturer_hint or response.manufacturer.value or "Unknown"
    versions: list[CatalogVersion] = []

    if job.equipment_type == "module":
        for variant in response.module_fields.variants:
            model = variant.model_variant or job.model_hint or response.model.value
            if not model:
                continue
            flat = flatten_module_variant(variant, response.module_fields)
            versions.append(
                _upsert_draft(session, "module", str(manufacturer), str(model), catalog_merge.MODULE_FIELD_NAMES, flat)
            )
    else:
        model = job.model_hint or response.model.value or "Unknown"
        flat = flatten_inverter_fields(response.inverter_fields)
        versions.append(
            _upsert_draft(session, "inverter", str(manufacturer), str(model), catalog_merge.INVERTER_FIELD_NAMES, flat)
        )

    job.status = "merged"
    job.catalog_version_id = versions[0].id if versions else None
    job.updated_at = _now()
    session.add(job)
    session.commit()
    session.refresh(job)
    return versions


def _process_job(session: Session, job: IngestionJob) -> list[CatalogVersion]:
    job.status = "processing"
    job.updated_at = _now()
    session.add(job)
    session.commit()

    if job.file_type in ("pan", "ond"):
        _mark_needs_attention(session, job, f"no .{job.file_type.upper()} parser implemented")
        return []

    return _process_pdf_job(session, job)


@router.post("/ingest/upload")
async def ingest_upload(
    file: UploadFile = File(...),
    equipment_type: str = Form(...),
    manufacturer: str | None = Form(None),
    model: str | None = Form(None),
    session: Session = Depends(get_session),
) -> dict:
    if equipment_type not in ("module", "inverter"):
        raise HTTPException(status_code=422, detail=f"equipment_type must be 'module' or 'inverter', got {equipment_type!r}")

    file_type = _file_type(file.filename or "")
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds 25 MB")

    job = IngestionJob(
        filename=file.filename or "unnamed",
        file_type=file_type,
        equipment_type=equipment_type,
        manufacturer_hint=manufacturer,
        model_hint=model,
        file_data=data,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    versions = _process_job(session, job)
    return {"job": _job_to_dict(job), "catalog_versions": [_version_to_dict(v) for v in versions]}


@router.post("/ingest/{job_id}/retry")
def retry_ingestion(job_id: str, session: Session = Depends(get_session)) -> dict:
    job = session.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No ingestion job with id {job_id!r}")
    if job.file_data is None:
        raise HTTPException(status_code=400, detail="Job has no stored file data to retry")

    versions = _process_job(session, job)
    return {"job": _job_to_dict(job), "catalog_versions": [_version_to_dict(v) for v in versions]}


@router.get("/ingest/{job_id}")
def get_ingestion_job(job_id: str, session: Session = Depends(get_session)) -> dict:
    job = session.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No ingestion job with id {job_id!r}")
    return _job_to_dict(job)


@router.get("/catalog/pending")
def list_pending_catalog_versions(session: Session = Depends(get_session)) -> list[dict]:
    statement = select(CatalogVersion).where(CatalogVersion.status == "pending_review").order_by(CatalogVersion.created_at)
    return [_version_to_dict(v) for v in session.exec(statement).all()]


@router.get("/catalog/{equipment_type}/{manufacturer}/{model}/versions")
def list_catalog_versions(equipment_type: str, manufacturer: str, model: str, session: Session = Depends(get_session)) -> dict:
    statement = (
        select(CatalogVersion)
        .where(
            CatalogVersion.equipment_type == equipment_type,
            CatalogVersion.manufacturer == manufacturer,
            CatalogVersion.model == model,
        )
        .order_by(CatalogVersion.created_at)
    )
    versions = session.exec(statement).all()
    default = session.get(CatalogDefault, f"{equipment_type}|{manufacturer}|{model}")
    return {
        "versions": [_version_to_dict(v) for v in versions],
        "default_version_id": default.version_id if default else None,
    }


@router.put("/catalog/versions/{version_id}")
def edit_catalog_version(version_id: str, body: dict[str, dict], session: Session = Depends(get_session)) -> dict:
    version = session.get(CatalogVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"No catalog version with id {version_id!r}")
    if version.status != "pending_review":
        raise HTTPException(status_code=400, detail=f"Only pending_review versions can be edited, got status {version.status!r}")

    fields = _load_fields(version)
    for field_name, raw_value in body.items():
        fields[field_name] = FieldValue.model_validate(raw_value)
    version.fields = _dump_fields(fields)
    version.updated_at = _now()
    session.add(version)
    session.commit()
    session.refresh(version)
    return _version_to_dict(version)


@router.post("/catalog/versions/{version_id}/approve")
def approve_catalog_version(version_id: str, session: Session = Depends(get_session)) -> dict:
    version = session.get(CatalogVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"No catalog version with id {version_id!r}")
    if version.status != "pending_review":
        raise HTTPException(status_code=400, detail=f"Only pending_review versions can be approved, got status {version.status!r}")

    version.status = "active"
    version.approved_at = _now()
    version.updated_at = _now()
    session.add(version)

    key = f"{version.equipment_type}|{version.manufacturer}|{version.model}"
    default = session.get(CatalogDefault, key)
    default_auto_set = False
    if default is None:
        # First version for this model — no ambiguity, safe to default to it.
        default = CatalogDefault(
            key=key,
            equipment_type=version.equipment_type,
            manufacturer=version.manufacturer,
            model=version.model,
            version_id=version.id,
        )
        session.add(default)
        default_auto_set = True
    # Else: a default already exists — per the versioning spec, approving a
    # new version never silently changes it. The engineer must call
    # /catalog/{...}/set-default explicitly.

    session.commit()
    session.refresh(version)
    return {
        "version": _version_to_dict(version),
        "default_auto_set": default_auto_set,
        "current_default_version_id": default.version_id,
        "needs_default_choice": not default_auto_set,
    }


@router.post("/catalog/versions/{version_id}/reject")
def reject_catalog_version(version_id: str, session: Session = Depends(get_session)) -> dict:
    version = session.get(CatalogVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"No catalog version with id {version_id!r}")
    if version.status != "pending_review":
        raise HTTPException(status_code=400, detail=f"Only pending_review versions can be rejected, got status {version.status!r}")

    # Kept, not deleted — audit trail, same spirit as Changeset never
    # hard-deleting a failed row.
    version.status = "rejected"
    version.updated_at = _now()
    session.add(version)
    session.commit()
    session.refresh(version)
    return _version_to_dict(version)


@router.post("/catalog/{equipment_type}/{manufacturer}/{model}/set-default")
def set_catalog_default(
    equipment_type: str, manufacturer: str, model: str, body: dict, session: Session = Depends(get_session)
) -> dict:
    version_id = body.get("version_id")
    if not version_id:
        raise HTTPException(status_code=422, detail="version_id is required")

    version = session.get(CatalogVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"No catalog version with id {version_id!r}")
    if version.status != "active":
        raise HTTPException(status_code=400, detail="Only an active (approved) version can be set as default")
    if (version.equipment_type, version.manufacturer, version.model) != (equipment_type, manufacturer, model):
        raise HTTPException(status_code=422, detail="version does not belong to this equipment_type/manufacturer/model")

    key = f"{equipment_type}|{manufacturer}|{model}"
    default = session.get(CatalogDefault, key)
    if default is None:
        default = CatalogDefault(key=key, equipment_type=equipment_type, manufacturer=manufacturer, model=model, version_id=version_id)
    else:
        default.version_id = version_id
        default.updated_at = _now()
    session.add(default)
    session.commit()
    session.refresh(default)
    return {"key": key, "default_version_id": default.version_id}
