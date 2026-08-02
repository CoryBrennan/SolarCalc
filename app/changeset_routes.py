"""Project persistence + changeset endpoints — the sync layer between
project data changes and the AutoCAD add-in picking them up.

Workflow: PUT the current project state, then POST a /refresh for whichever
block you want checked. A new "regenerate" changeset is only created if the
computed config actually differs from the last one for that tag — refresh
is meant to be called often (including on every project edit) without
spamming duplicate changesets. The add-in polls GET /changesets/pending and
reports back via /applied or /failed.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import aux_panelboard_block, changesets, inverter_dc_block, static_device_block, switchboard_block
from app.db import get_session
from app.db_models import Changeset, Project
from app.models import ProjectInput
from app.project_calc import compute_combiner_ocpd_switchboard, validate_module_skus

router = APIRouter()


def _changeset_to_dict(cs: Changeset) -> dict:
    return {
        "changeset_id": cs.id,
        "operation": cs.operation,
        "target_tag": cs.target_tag,
        "block_type": cs.block_type,
        "config": json.loads(cs.config),
        "status": cs.status,
        "retry_count": cs.retry_count,
        "last_error": cs.last_error,
        "created_at": cs.created_at,
        "updated_at": cs.updated_at,
    }


def _load_project(session: Session, project_id: str = "default") -> ProjectInput:
    stored = session.get(Project, project_id)
    if stored is None:
        return ProjectInput()
    return ProjectInput.model_validate(json.loads(stored.data))


@router.put("/projects/{project_id}")
def put_project(project_id: str, project: ProjectInput, session: Session = Depends(get_session)) -> dict:
    validate_module_skus(project)
    existing = session.get(Project, project_id)
    payload = project.model_dump_json()
    if existing is None:
        existing = Project(id=project_id, data=payload)
    else:
        existing.data = payload
    session.add(existing)
    session.commit()
    return {"id": project_id, "saved": True}


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_session)) -> ProjectInput:
    stored = session.get(Project, project_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"No project stored with id {project_id!r}")
    return ProjectInput.model_validate(json.loads(stored.data))


@router.post("/changesets/switchboard/refresh")
def refresh_switchboard_changeset(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    """Recomputes the switchboard config from the stored project and enqueues
    a new "regenerate" changeset only if it differs from the last one."""
    project = _load_project(session, project_id)
    validate_module_skus(project)

    num_inverters = project.site.num_inverters
    _combiner_result, ocpd_result, switchboard_result = compute_combiner_ocpd_switchboard(project, num_inverters)

    config = switchboard_block.build_switchboard_config(
        tag="SWBD-1",
        inverter_phases=project.inverter.phases,
        busbar_rating_a=project.switchboard.busbar_rating_a,
        main_rating_a=project.switchboard.main_rating_a,
        num_inverters=num_inverters,
        inverter_ocpd_standard_size_a=ocpd_result["standard_size_a"],
        backfeed_total_a=switchboard_result["actual_backfed_a"],
    )

    changeset, created = changesets.refresh_changeset(
        session, target_tag=config["tag"], block_type="SWITCHBOARD", operation="regenerate", config=config
    )
    return {"created": created, "changeset": _changeset_to_dict(changeset)}


@router.post("/changesets/aux-panelboard/refresh")
def refresh_aux_panelboard_changeset(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    """Recomputes the aux load panelboard config from the stored project and
    enqueues a new "regenerate" changeset only if it differs from the last one."""
    project = _load_project(session, project_id)
    config = aux_panelboard_block.build_aux_panelboard_config(project.aux_panelboard)

    changeset, created = changesets.refresh_changeset(
        session, target_tag=config["tag"], block_type="AUX_PANELBOARD", operation="regenerate", config=config
    )
    return {"created": created, "changeset": _changeset_to_dict(changeset)}


@router.post("/changesets/inverter-dc/refresh")
def refresh_inverter_dc_changesets(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    """Recomputes the inverter DC block config(s) from the stored project.
    "direct" topology produces one INV-1 mppt block; "combiner" topology
    produces one DCC-N block per row in the combiner schedule — each gets
    its own changeset, refreshed (created only if changed) independently."""
    project = _load_project(session, project_id)
    validate_module_skus(project)

    results = []
    if project.inverter.dc_topology == "direct":
        config = inverter_dc_block.build_inverter_dc_mppt_config(project)
        changeset, created = changesets.refresh_changeset(
            session, target_tag=config["tag"], block_type="INVERTER_DC", operation="regenerate", config=config
        )
        results.append({"created": created, "changeset": _changeset_to_dict(changeset)})
    else:
        for config in inverter_dc_block.build_inverter_dc_combiner_configs(project):
            changeset, created = changesets.refresh_changeset(
                session, target_tag=config["tag"], block_type="INVERTER_DC", operation="regenerate", config=config
            )
            results.append({"created": created, "changeset": _changeset_to_dict(changeset)})

    return {"topology": project.inverter.dc_topology, "results": results}


@router.post("/changesets/transformer/refresh")
def refresh_transformer_changeset(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    """Static geometry — "attribute_update", not "regenerate". SDS_FLAG and
    GROUNDING_TYPE come straight from bonding_calc's existing SDS logic."""
    project = _load_project(session, project_id)
    config = static_device_block.build_transformer_config("XFMR-1", project.transformer)

    changeset, created = changesets.refresh_changeset(
        session, target_tag=config["tag"], block_type="TRANSFORMER", operation="attribute_update", config=config
    )
    return {"created": created, "changeset": _changeset_to_dict(changeset)}


@router.post("/changesets/mv-recloser/refresh")
def refresh_mv_recloser_changeset(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    project = _load_project(session, project_id)
    config = static_device_block.build_mv_recloser_config(project.mv_recloser)

    changeset, created = changesets.refresh_changeset(
        session, target_tag=config["tag"], block_type="MV_RECLOSER", operation="attribute_update", config=config
    )
    return {"created": created, "changeset": _changeset_to_dict(changeset)}


@router.post("/changesets/mv-goab/refresh")
def refresh_mv_goab_changeset(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    project = _load_project(session, project_id)
    config = static_device_block.build_mv_goab_config(project.mv_goab)

    changeset, created = changesets.refresh_changeset(
        session, target_tag=config["tag"], block_type="MV_GOAB", operation="attribute_update", config=config
    )
    return {"created": created, "changeset": _changeset_to_dict(changeset)}


@router.post("/changesets/mv-meter/refresh")
def refresh_mv_meter_changeset(project_id: str = "default", session: Session = Depends(get_session)) -> dict:
    project = _load_project(session, project_id)
    config = static_device_block.build_mv_meter_config(project.mv_meter)

    changeset, created = changesets.refresh_changeset(
        session, target_tag=config["tag"], block_type="MV_METER", operation="attribute_update", config=config
    )
    return {"created": created, "changeset": _changeset_to_dict(changeset)}


@router.get("/changesets")
def list_changesets(
    status: str | None = None,
    block_type: str | None = None,
    target_tag: str | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    results = changesets.list_changesets(session, status=status, block_type=block_type, target_tag=target_tag)
    return [_changeset_to_dict(cs) for cs in results]


@router.get("/changesets/pending")
def list_pending_changesets(
    block_type: str | None = None,
    target_tag: str | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    results = changesets.list_changesets(session, status="pending", block_type=block_type, target_tag=target_tag)
    return [_changeset_to_dict(cs) for cs in results]


@router.get("/changesets/{changeset_id}")
def get_changeset(changeset_id: str, session: Session = Depends(get_session)) -> dict:
    cs = changesets.get_changeset(session, changeset_id)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"No changeset with id {changeset_id!r}")
    return _changeset_to_dict(cs)


@router.post("/changesets/{changeset_id}/applied")
def mark_changeset_applied(changeset_id: str, session: Session = Depends(get_session)) -> dict:
    cs = changesets.get_changeset(session, changeset_id)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"No changeset with id {changeset_id!r}")
    return _changeset_to_dict(changesets.mark_applied(session, cs))


@router.post("/changesets/{changeset_id}/failed")
def mark_changeset_failed(changeset_id: str, body: dict, session: Session = Depends(get_session)) -> dict:
    cs = changesets.get_changeset(session, changeset_id)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"No changeset with id {changeset_id!r}")
    error = body.get("error", "unknown error")
    return _changeset_to_dict(changesets.mark_failed(session, cs, error))


@router.post("/changesets/{changeset_id}/retry")
def retry_changeset(changeset_id: str, session: Session = Depends(get_session)) -> dict:
    cs = changesets.get_changeset(session, changeset_id)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"No changeset with id {changeset_id!r}")
    return _changeset_to_dict(changesets.retry(session, cs))
