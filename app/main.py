"""Stateless calculation API — takes one project's worth of state (the same
shape the HMI draft's "Save project" button already produces) and returns
every computed result in one response.

/calculate and /generate/switchboard-config stay fully stateless (no DB
involved) — the changeset routes (app.changeset_routes) are what actually
use persistence, for the AutoCAD sync loop.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sqlmodel import Session

from app import (
    ampacity_calc,
    bonding_calc,
    device_templates,
    document_header,
    etap_export,
    fluke_export_import,
    fluke_validate,
    geocode_lookup,
    gps_validation_calc,
    iv_curve_calc,
    jurisdiction_lookup,
    placarding_calc,
    pvapx_generator,
    pvcase_bom_import,
    pvcase_dwg_scan,
    pvcase_plan,
    pvcase_routing,
    pvcase_validate,
    raceway_calc,
    string_design_calc,
    switchboard_block,
    trench_calc,
    voltage_drop_calc,
    wire_cost_calc,
)
from app.bluebeam_routes import router as bluebeam_router
from app.catalog_routes import router as catalog_router
from app.changeset_routes import router as changeset_router
from app.commissioning_routes import router as commissioning_router
from app.db import create_db_and_tables, engine
from app.device_template_routes import router as device_template_router
from app.skyvisor_routes import router as skyvisor_router
from app.fluke_export_import import FlukeImportError
from app.fluke_validate import FlukeValidateRequest
from app.gps_validation_calc import GpsValidationRequest, PileValidationRequest
from app.models import ProjectInput, SiteAddress
from app.module_catalog import MODULE_SKUS
from app.pdf_extract import PdfExtractionError, extract_pdf_text
from app.project_calc import compute_actual_capacity, compute_combiner_ocpd_switchboard, validate_module_skus
from app.pvapx_generator import FlukePvapxFromDesignRequest, FlukePvapxRequest
from app.pvcase_bom_import import PvcaseBomError
from app.pvcase_dwg_scan import PvcaseDwgError
from app.pvcase_plan import PvcasePlanRequest
from app.pvcase_routing import PvcaseRoutingRequest
from app.pvcase_validate import PvcaseValidateRequest
from app.trench_calc import TrenchDesignRequest, TrenchInputError
from app.wire_cost_calc import FeederValueEngineeringRequest, ProjectFeederVeRequest

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
app.include_router(skyvisor_router)
app.include_router(bluebeam_router)
app.include_router(commissioning_router)
app.include_router(device_template_router)

# A database that is merely unreachable must not stop the service booting.
# uvicorn imports this module to find `app`, so anything raised here kills the
# process outright -- and on the free tiers that is a routine occurrence, not
# an exotic one: the Render instance spins down on inactivity and re-imports on
# every wake, while a free Supabase project pauses after a week idle. Letting
# that abort startup would take down /calculate and /health too, neither of
# which touches a database. DB-backed routes fail individually instead, which
# is both a smaller blast radius and a clearer signal.
#
# A malformed DATABASE_URL is deliberately NOT caught here: app.db raises that
# before this line, because it is a config error someone has to fix rather than
# a transient one to serve around.
try:
    create_db_and_tables()
    with Session(engine) as _seed_session:
        device_templates.seed_default_templates(_seed_session)
except Exception:
    logging.getLogger(__name__).exception(
        "Could not reach the database at startup -- serving anyway. "
        "Stateless routes work; database-backed routes will fail until it is reachable."
    )


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
    is what actually makes "Sync with Backend" work.

    FileResponse sets no Cache-Control of its own, so browsers fall back to
    heuristic caching off Last-Modified and can serve a stale index.html for
    a while after a deploy without ever revalidating -- explicit no-cache
    forces an If-None-Match/If-Modified-Since check on every load instead
    (still a cheap 304 when unchanged, not a full re-fetch)."""
    return FileResponse(_STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/calculate")
def calculate(project: ProjectInput) -> dict:
    validate_module_skus(project)

    site = project.site
    num_inverters = project.inverter.quantity

    jurisdiction_result = jurisdiction_lookup.resolve_nec_edition(
        state=project.jurisdiction.state,
        county=project.jurisdiction.county,
        ahj_override=project.jurisdiction.ahj_override,
        nec_edition_override=project.jurisdiction.nec_edition_override,
    )

    combiner_result, ocpd_result, switchboard_result = compute_combiner_ocpd_switchboard(project, num_inverters)

    ampacity_result = ampacity_calc.size_conductor(
        module_sku=project.module.sku,
        inverter=project.inverter,
        ashrae=project.ashrae,
        ampacity_input=project.ampacity,
    )

    bonding_result = bonding_calc.size_bonding_and_grounding(project.transformer)

    raceway_result = raceway_calc.compute_raceway_runs(
        project.raceway_runs, ambient_c=project.ashrae.max_design_temp_c
    )

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
            "storage_target_wh": site.storage_target_wh,
            "other_generation_target_w": site.other_generation_target_w,
            "actual_dc_capacity_w": round(actual_dc_w, 2),
            "actual_ac_capacity_w": round(actual_ac_w, 2),
        },
        "jurisdiction": jurisdiction_result,
        "combiners": combiner_result,
        "ocpd": ocpd_result,
        "switchboard": switchboard_result,
        "ampacity": ampacity_result,
        "raceway": raceway_result,
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


@app.post("/pvcase/routing-report")
def pvcase_routing_report(request: PvcaseRoutingRequest) -> dict:
    """Closes the other half of memory/pvcase_integration_gaps.md's
    routing-condition gap: PVCase's BOM lengths are module-connector-to-
    endpoint only, with no above-ground/free-air vs. underground-conduit
    breakdown, so ampacity_calc's Table 310.15(C)(1) fill derating can't be
    trusted straight off a flat PVCase length. Applies one routing template
    per circuit type (app/cable_routing_calc.py) across every real segment
    parsed from the BOM and reports the governing (worst-case) segment and
    conductor per circuit type -- not a single sitewide guess."""
    try:
        bom = pvcase_bom_import.parse_pvcase_bom(request.bom_path)
    except PvcaseBomError as exc:
        raise HTTPException(status_code=422, detail=f"BOM parse error: {exc}") from exc

    return pvcase_routing.compute_routing_report(request.project, bom, request.assumptions)


@app.post("/value-engineering/feeder")
def value_engineering_feeder(request: FeederValueEngineeringRequest) -> dict:
    """Copper vs. aluminum, single vs. parallel-set cost comparison for one
    feeder/branch run — app/wire_cost_calc.py. Standalone by design (takes
    its own current/voltage/length inputs rather than a full ProjectInput)
    so it can be pointed at any run by the foot, including one copied from a
    project's raceway_runs/ampacity inputs."""
    return wire_cost_calc.evaluate_feeder_value_engineering(request.scenario, request.pricing)


@app.post("/value-engineering/project-feeders")
def value_engineering_project_feeders(request: ProjectFeederVeRequest) -> dict:
    """Same comparison as /value-engineering/feeder, run once per row on the
    project's own raceway_runs (the "Conduit, Tray & Messenger" panel's
    schedule) instead of one manually-entered run -- so a saved project's
    feeders can be value-engineered without retyping current/voltage/length
    that's already on file."""
    return wire_cost_calc.evaluate_project_feeders(request)


@app.post("/trench/thermal-design")
def trench_thermal_design(request: TrenchDesignRequest) -> dict:
    """Trench ampacity for direct-buried conduits — app/trench_calc.py over the
    numerical solver in app/trench_thermal/.

    Conduits sharing a trench mutually heat each other through the soil, which
    is a different mechanism from NEC 310.15(C)(1) conduit fill (conductors
    crowding INSIDE one conduit) and is not covered by any table: the response
    reports the two as separate multiplying rows, never folded together.
    Inputs are pulled from the project's own raceway_runs — current, conductor,
    and trade size all come from what the Raceway (24) panel already sized, so
    nothing is re-entered and the NEC derates are not reapplied to the I^2*R
    heat term.

    Two modes. With `conditions.fixed_layout` set it checks one already-drawn
    arrangement (a fraction of a second). Without it, the full layer-count and
    1.5"-on-centre spacing search runs — tens of seconds for a large trench,
    which is why this is a "run calc" button and not a live recompute."""
    try:
        return trench_calc.compute_trench_design(request)
    except TrenchInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@app.post("/fluke/validate")
def fluke_validate_export(request: FlukeValidateRequest) -> dict:
    """Parses a real Solmetric PVA field export (app/fluke_export_import.py)
    and validates it: per-string pass/fail preferring the vendor's own
    Modeled/Deviation columns, a design-intent divergence check against this
    project's catalog module (catches a wrong module entered into the field
    tool's Solmetric project -- see app/fluke_validate.py's module
    docstring), and, if a BOM is supplied, a coverage check for strings the
    BOM expected but the export never tested."""
    try:
        readings = fluke_export_import.parse_fluke_export(request.export_path)
    except FlukeImportError as exc:
        raise HTTPException(status_code=422, detail=f"Fluke export parse error: {exc}") from exc

    bom = None
    if request.bom_path:
        try:
            bom = pvcase_bom_import.parse_pvcase_bom(request.bom_path)
        except PvcaseBomError as exc:
            raise HTTPException(status_code=422, detail=f"BOM parse error: {exc}") from exc

    report = fluke_validate.build_validation_report(
        readings,
        tolerance_pct=request.tolerance_pct,
        module_sku=request.project.module.sku,
        modules_per_string=request.project.iv_curve_conditions.modules_per_string,
        bom=bom,
    )
    return {"all_pass": report.all_pass(), "coverage_complete": report.coverage_complete(), **dataclasses.asdict(report)}


@app.post("/fluke/pvapx")
def fluke_generate_pvapx(request: FlukePvapxRequest) -> dict:
    """Generates a real Solmetric PVA `.pvapx` project file from a parsed
    PVCase BOM + this project's catalog module, by cloning a real template
    project and rewriting its module data and switchboard/inverter/combiner/
    string tree -- see app/pvapx_generator.py's module docstring, including
    the MANDATORY validation gate: open the output in real Solmetric PVA
    software and confirm it loads before trusting it on an actual job. Only
    meaningful running on the engineer's own machine, same as DWG scanning
    and BOM/export parsing -- all three are local Dropbox-synced file paths."""
    if request.project.module.sku not in MODULE_SKUS:
        raise HTTPException(status_code=422, detail=f"Unknown module SKU {request.project.module.sku!r} -- not in module_catalog.MODULE_SKUS")

    try:
        bom = pvcase_bom_import.parse_pvcase_bom(request.bom_path)
    except PvcaseBomError as exc:
        raise HTTPException(status_code=422, detail=f"BOM parse error: {exc}") from exc

    module = MODULE_SKUS[request.project.module.sku]
    counts = pvapx_generator.generate_pvapx(
        request.template_path,
        request.output_path,
        bom,
        module,
        manufacturer=request.manufacturer,
        model_name=request.model_name or request.project.module.sku,
        modules_per_string=request.modules_per_string,
        noct_c=request.noct_c,
    )
    return {
        "ok": True,
        "output_path": request.output_path,
        **dataclasses.asdict(counts),
        "validation_gate": (
            "UNVERIFIED until opened in real Solmetric PVA software and confirmed to load -- "
            "do not trust this file on an actual job before that check."
        ),
    }


@app.post("/fluke/pvapx-from-design")
def fluke_generate_pvapx_from_design(request: FlukePvapxFromDesignRequest) -> dict:
    """Same as /fluke/pvapx, but needs no PVCase BOM export at all -- the
    switchboard/inverter/combiner/string tree is derived purely from this
    project's own design: app/pvcase_plan.py's tag generation (naming
    convention + switchboard layout, same one-DC-combiner-per-inverter
    assumption it already documents) for the inverter/combiner tags and
    NEC 690.7(A)(2) string sizing for modules-per-string, plus module/
    inverter quantity to derive a strings-per-combiner count (overridable
    via `strings_per_combiner` if the site doesn't distribute strings
    evenly). See app/pvapx_generator.py's build_hierarchy_from_plan()
    docstring for exactly what's derived vs. assumed, and the same
    MANDATORY validation gate as /fluke/pvapx."""
    if request.project.module.sku not in MODULE_SKUS:
        raise HTTPException(status_code=422, detail=f"Unknown module SKU {request.project.module.sku!r} -- not in module_catalog.MODULE_SKUS")
    if request.project.inverter.dc_topology != "combiner":
        raise HTTPException(status_code=422, detail="Design-derived .pvapx generation assumes dc_topology == 'combiner' (one DC combiner per inverter) -- see build_hierarchy_from_plan()'s docstring.")

    plan_result = pvcase_plan.build_pvcase_plan(request.project, request.plan)
    modules_per_string = plan_result["modules_per_string_to_use_in_pvcase"]
    if not modules_per_string:
        raise HTTPException(status_code=422, detail="No valid string length could be computed for this design -- check Module Spec/Inverter Spec/ASHRAE inputs.")

    n_inverters = len(plan_result["expected_tags"]["inverters"])
    strings_per_combiner = request.strings_per_combiner
    if strings_per_combiner is None:
        modules_per_inverter = request.project.module.quantity / n_inverters
        strings_per_combiner = round(modules_per_inverter / modules_per_string)

    module = MODULE_SKUS[request.project.module.sku]
    counts = pvapx_generator.generate_pvapx_from_plan(
        request.template_path,
        request.output_path,
        plan_result,
        module,
        manufacturer=request.manufacturer,
        model_name=request.model_name or request.project.module.sku,
        modules_per_string=modules_per_string,
        strings_per_combiner=strings_per_combiner,
        noct_c=request.noct_c,
    )
    return {
        "ok": True,
        "output_path": request.output_path,
        "modules_per_string": modules_per_string,
        "strings_per_combiner": strings_per_combiner,
        **dataclasses.asdict(counts),
        "validation_gate": (
            "UNVERIFIED until opened in real Solmetric PVA software and confirmed to load -- "
            "do not trust this file on an actual job before that check."
        ),
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
