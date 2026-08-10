"""Stateless calculation API — takes one project's worth of state (the same
shape the HMI draft's "Save project" button already produces) and returns
every computed result in one response.

/calculate and /generate/switchboard-config stay fully stateless (no DB
involved) — the changeset routes (app.changeset_routes) are what actually
use persistence, for the AutoCAD sync loop.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import (
    ampacity_calc,
    bonding_calc,
    document_header,
    etap_export,
    geocode_lookup,
    gps_validation_calc,
    iv_curve_calc,
    jurisdiction_lookup,
    placarding_calc,
    pvcase_bom_import,
    pvcase_dwg_scan,
    pvcase_plan,
    pvcase_validate,
    string_design_calc,
    switchboard_block,
    voltage_drop_calc,
)
from app.catalog_routes import router as catalog_router
from app.changeset_routes import router as changeset_router
from app.db import create_db_and_tables
from app.gps_validation_calc import GpsValidationRequest, PileValidationRequest
from app.models import ProjectInput, SiteAddress
from app.pdf_extract import PdfExtractionError, extract_pdf_text
from app.project_calc import compute_actual_capacity, compute_combiner_ocpd_switchboard, validate_module_skus
from app.pvcase_bom_import import PvcaseBomError
from app.pvcase_dwg_scan import PvcaseDwgError
from app.pvcase_plan import PvcasePlanRequest
from app.pvcase_validate import PvcaseValidateRequest

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
app.include_router(catalog_router)
create_db_and_tables()


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Reference screenshots for the I-V Curve Field Prep panel's PVA setup
# walkthrough (Part 1) -- static assets, not per-project generated content.
app.mount("/img", StaticFiles(directory=_STATIC_DIR / "img"), name="img")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Datasheet ingest is same-origin only (the HMI is served by this same app),
# so unlike /calculate — which the HMI can point at any backend URL via
# Backend Verification's URL field — this deliberately isn't reachable from a
# different host and needs no auth beyond that.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@app.post("/extract/pdf")
async def extract_pdf(file: UploadFile = File(...)) -> dict:
    """Text extraction only — no field parsing. The HMI's JS pulls Pmax, Voc,
    an AC rating, etc. out of the returned text with the same label/value
    regex approach the ASHRAE panel already uses on pasted station data, so
    there's one parsing style instead of two, and a designer can see exactly
    what the extraction produced before any field is pulled from it."""
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds 25 MB")
    try:
        text, pages = extract_pdf_text(data)
    except PdfExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"filename": file.filename, "pages": pages, "text": text}


@app.post("/site/geocode")
def geocode_site(address: SiteAddress) -> dict:
    """Resolves lat/long/elevation/timezone for a SiteAddress via free public
    services (app/geocode_lookup.py) — the coordinate data PVsystCLI's
    create-site command needs, which nothing in this app captured before now.
    Run explicitly by the engineer (not on every save) so a flaky external
    service can't block /calculate, and so a resolved address can be reviewed
    before it's trusted for a simulation input."""
    return geocode_lookup.geocode_address(address)


@app.post("/site/gps-validate")
def gps_validate(request: GpsValidationRequest) -> dict:
    """Compares an Emlid Flow 360 RTK survey export against a design point
    list (equipment tag + State Plane easting/northing/elevation) and flags
    any as-built point outside tolerance. Points below RTK-fixed quality
    (SINGLE/FLOAT solution status, or lateral RMS over the configured
    threshold) are rejected rather than compared -- see
    app/gps_validation_calc.py for the quality gate."""
    return gps_validation_calc.validate_request(request)


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

    string_length_result = string_design_calc.compute_string_length_range(
        module=project.module,
        inverter=project.inverter,
        ashrae=project.ashrae,
        voltage_drop_limits=project.voltage_drop_limits,
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
        "string_design": string_length_result,
        "document_header": {"header": header, "missing_fields": document_header.missing_header_fields(header)},
    }


@app.post("/pvcase/plan")
def pvcase_planning_brief(request: PvcasePlanRequest) -> dict:
    """App Planning step: what to key into PVCase before building the site
    layout -- target modules-per-string (from string_design_calc) and the
    exact equipment tags (INV/DCC/XFMR) PVCase should end up producing, so
    the later /pvcase/validate call has something concrete to check against."""
    return pvcase_plan.build_pvcase_plan(request.project, request.plan)


@app.post("/pvcase/validate")
def pvcase_validate_design(request: PvcaseValidateRequest) -> dict:
    """App Validation step: parses whichever of the BOM export / CAD Release
    DWG are supplied and checks their equipment tags against this project's
    plan (and against each other). DWG scanning drives a local, real AutoCAD
    session headlessly and can take a couple of minutes on a large drawing --
    that's inherent to accoreconsole, not a bug."""
    bom = None
    if request.bom_path:
        try:
            bom = pvcase_bom_import.parse_pvcase_bom(request.bom_path)
        except PvcaseBomError as exc:
            raise HTTPException(status_code=422, detail=f"BOM parse error: {exc}") from exc

    dwg_tags = None
    if request.dwg_path:
        try:
            dwg_tags = pvcase_dwg_scan.scan_device_tags(request.dwg_path)
        except PvcaseDwgError as exc:
            raise HTTPException(status_code=422, detail=f"DWG scan error: {exc}") from exc

    report = pvcase_validate.validate_pvcase_design(request.project, request.plan, bom=bom, dwg_tags=dwg_tags)
    return {"ok": report.ok(), **dataclasses.asdict(report)}


@app.post("/pvcase/gps-validate")
def pvcase_gps_validate(request: PileValidationRequest) -> dict:
    """As-built pile QA: pulls pile design coordinates straight from a
    PVCase BOM's "Piling information" sheet (lat/long -- see
    app/pvcase_bom_import.parse_piling_information) and checks them against
    an Emlid Flow 360 RTK survey export. Equipment (inverter/DC combiner/
    transformer) siting isn't covered here -- PVCase's BOM has no
    coordinates for those; that path is /pvcase/validate's DWG scan side
    instead. The design-vs-as-built tag match depends on the field crew's
    point names actually corresponding to PVCase's frame+pile tags -- see
    app/gps_validation_calc.piles_to_design_points."""
    try:
        return gps_validation_calc.validate_piles_request(request)
    except PvcaseBomError as exc:
        raise HTTPException(status_code=422, detail=f"BOM parse error: {exc}") from exc


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
