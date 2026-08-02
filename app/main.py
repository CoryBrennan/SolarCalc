"""Stateless calculation API — takes one project's worth of state (the same
shape the HMI draft's "Save project" button already produces) and returns
every computed result in one response.

/calculate and /generate/switchboard-config stay fully stateless (no DB
involved) — the changeset routes (app.changeset_routes) are what actually
use persistence, for the AutoCAD sync loop.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import (
    ampacity_calc,
    bonding_calc,
    document_header,
    etap_export,
    iv_curve_calc,
    jurisdiction_lookup,
    placarding_calc,
    switchboard_block,
    voltage_drop_calc,
)
from app.changeset_routes import router as changeset_router
from app.db import create_db_and_tables
from app.models import ProjectInput
from app.project_calc import compute_actual_capacity, compute_combiner_ocpd_switchboard, validate_module_skus

app = FastAPI(title="Solar Calc Engine", version="0.1.0")

# Dev-only: wide open so the HMI draft (served from whatever local port or
# claude.ai sandbox origin) can call this without a CORS error. Tighten this
# to a specific origin allowlist once there's a real deployment target.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(changeset_router)
create_db_and_tables()


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def serve_hmi() -> FileResponse:
    """The HMI itself, served same-origin — the published claude.ai artifact
    copy can't call this API at all (its sandbox blocks external fetch/XHR,
    confirmed via the artifact-capabilities skill: only downloads/mcp
    capabilities exist, neither fits an arbitrary REST backend). This route
    is what actually makes "Sync with Backend" work."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/calculate")
def calculate(project: ProjectInput) -> dict:
    validate_module_skus(project)

    site = project.site
    num_inverters = project.inverter.quantity

    jurisdiction_result = jurisdiction_lookup.resolve_nec_edition(
        state=project.jurisdiction.state,
        county=project.jurisdiction.county,
        ahj_override=project.jurisdiction.ahj_override,
    )

    combiner_result, ocpd_result, switchboard_result = compute_combiner_ocpd_switchboard(project, num_inverters)

    ampacity_result = ampacity_calc.size_conductor(
        module_sku=project.module.sku,
        inverter=project.inverter,
        ashrae=project.ashrae,
        ampacity_input=project.ampacity,
    )

    bonding_result = bonding_calc.size_bonding_and_grounding(project.transformer)

    voltage_drop_result = voltage_drop_calc.check_segment(
        current_a=project.inverter.max_output_current_a,
        length_ft=project.etap.length_ft,
        voltage_v=project.inverter.nominal_ac_voltage_v,
        limit_pct=project.voltage_drop_limits.inverter_to_switchboard_pct,
        conductor=project.etap.conductor,
    )

    placarding_result = placarding_calc.determine_placard_requirements(num_inverters)

    etap_result = etap_export.build_etap_export(
        num_inverters=num_inverters,
        inverter_ac_rating_w=project.inverter.ac_rating_w,
        assumptions=project.etap,
    )

    iv_expected = iv_curve_calc.expected_iv_point(
        module_sku=project.module.sku,
        irradiance_w_m2=project.iv_curve_conditions.irradiance_w_m2,
        cell_temp_c=project.iv_curve_conditions.cell_temp_c,
        modules_per_string=project.iv_curve_conditions.modules_per_string,
    )
    iv_validation = iv_curve_calc.validate_reading(
        expected=iv_expected,
        measured={
            "voc": project.iv_curve_reading.measured_voc,
            "isc": project.iv_curve_reading.measured_isc,
            "vmp": project.iv_curve_reading.measured_vmp,
            "imp": project.iv_curve_reading.measured_imp,
        },
        tolerance_pct=project.iv_curve_conditions.tolerance_pct,
    )

    header = document_header.build_document_header(
        site=site,
        client_info=project.client_info,
        ahj_name=jurisdiction_result["ahj_name"],
        nec_edition=jurisdiction_result["nec_edition"],
    )

    actual_dc_w, actual_ac_w = compute_actual_capacity(project)

    return {
        "site": {
            "num_inverters": num_inverters,
            "num_modules": project.module.quantity,
            "target_ac_capacity_w": site.target_ac_capacity_w,
            "target_dc_capacity_w": site.target_dc_capacity_w,
            "calculated_dc_ac_ratio": round(site.calculated_dc_ac_ratio, 4),
            "actual_dc_capacity_w": round(actual_dc_w, 2),
            "actual_ac_capacity_w": round(actual_ac_w, 2),
        },
        "jurisdiction": jurisdiction_result,
        "combiners": combiner_result,
        "ocpd": ocpd_result,
        "switchboard": switchboard_result,
        "ampacity": ampacity_result,
        "bonding": bonding_result,
        "voltage_drop": voltage_drop_result,
        "placarding": placarding_result,
        "etap": etap_result,
        "iv_curve": {"expected": iv_expected, "validation": iv_validation},
        "document_header": {"header": header, "missing_fields": document_header.missing_header_fields(header)},
    }


@app.post("/generate/switchboard-config")
def generate_switchboard_config(project: ProjectInput) -> dict:
    """Generator input contract for ac-switchboard-addin — same underlying
    calc as /calculate's ocpd + switchboard sections, reshaped to match the
    C# SwitchboardConfig class field-for-field."""
    validate_module_skus(project)

    num_inverters = project.inverter.quantity
    _combiner_result, ocpd_result, switchboard_result = compute_combiner_ocpd_switchboard(project, num_inverters)

    return switchboard_block.build_switchboard_config(
        tag="SWBD-1",
        inverter_phases=project.inverter.phases,
        busbar_rating_a=project.switchboard.busbar_rating_a,
        main_rating_a=project.switchboard.main_rating_a,
        num_inverters=num_inverters,
        inverter_ocpd_standard_size_a=ocpd_result["standard_size_a"],
        backfeed_total_a=switchboard_result["actual_backfed_a"],
    )
