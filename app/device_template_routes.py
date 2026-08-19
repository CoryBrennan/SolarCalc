"""CRUD for reusable custom-device templates, plus the connectable-target
registry the structured connects-to picker reads from. Project persistence
itself (PUT /projects/{id}, which is where CustomDeviceInstance rows are
actually saved) stays in changeset_routes.py alongside every other project
field — templates are project-independent, so they get their own router.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import device_templates
from app.connectable_targets import list_connectable_targets
from app.db import get_session
from app.db_models import Project
from app.models import ConnectableCategory, DeviceTemplate, ProjectInput

router = APIRouter()


def _load_project(session: Session, project_id: str = "default") -> ProjectInput:
    stored = session.get(Project, project_id)
    if stored is None:
        return ProjectInput()
    return ProjectInput.model_validate(json.loads(stored.data))


@router.post("/device-templates")
def create_device_template(template: DeviceTemplate, session: Session = Depends(get_session)) -> DeviceTemplate:
    return device_templates.create_template(session, template)


@router.get("/device-templates")
def list_device_templates(session: Session = Depends(get_session)) -> list[DeviceTemplate]:
    return device_templates.list_templates(session)


@router.get("/device-templates/{template_id}")
def get_device_template(template_id: str, session: Session = Depends(get_session)) -> DeviceTemplate:
    template = device_templates.get_template(session, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"No device template with id {template_id!r}")
    return template


@router.put("/device-templates/{template_id}")
def update_device_template(
    template_id: str, template: DeviceTemplate, session: Session = Depends(get_session)
) -> DeviceTemplate:
    updated = device_templates.update_template(session, template_id, template)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No device template with id {template_id!r}")
    return updated


@router.delete("/device-templates/{template_id}")
def delete_device_template(template_id: str, session: Session = Depends(get_session)) -> dict:
    deleted = device_templates.delete_template(session, template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No device template with id {template_id!r}")
    return {"deleted": True}


@router.get("/connectable-targets")
def get_connectable_targets(
    category: ConnectableCategory | None = None,
    project_id: str = "default",
    session: Session = Depends(get_session),
) -> list[dict]:
    project = _load_project(session, project_id)
    return list_connectable_targets(project, category)
