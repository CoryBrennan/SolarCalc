"""Shared calc orchestration used by both the stateless /calculate endpoint
and the changeset routes — kept separate from main.py so neither module has
to import the other.
"""

from __future__ import annotations

from fastapi import HTTPException

from app import combiner_calc, ocpd_calc, switchboard_calc
from app.models import ProjectInput
from app.module_catalog import MODULE_SKUS


def validate_module_skus(project: ProjectInput) -> None:
    if project.module.sku not in MODULE_SKUS:
        raise HTTPException(status_code=422, detail=f"Unknown module SKU: {project.module.sku!r}")
    for row in project.combiner_rows:
        if row.module_sku not in MODULE_SKUS:
            raise HTTPException(status_code=422, detail=f"Unknown module SKU on combiner row: {row.module_sku!r}")


def compute_actual_capacity(project: ProjectInput) -> tuple[float, float]:
    """Actual DC/AC capacity — nameplate rating x quantity actually on site,
    as distinct from SiteConfig's target_dc/ac_capacity_w (design goals that
    guide but never override ModuleSpec.quantity / InverterSpec.quantity)."""
    module_pmax_w = MODULE_SKUS[project.module.sku].pmax
    actual_dc_w = module_pmax_w * project.module.quantity
    actual_ac_w = project.inverter.ac_rating_w * project.inverter.quantity
    return actual_dc_w, actual_ac_w


def resolve_ocpd_continuous_current(project: ProjectInput, combiner_result: dict) -> float:
    """Mirrors the HMI's ocpdSyncDefault(): what continuous current feeds OCPD
    Sizing depends on which circuit reference is selected."""
    circuit = project.ocpd.circuit
    if circuit == "inverter_output":
        return project.inverter.max_output_current_a
    if circuit == "dc_combiner_output":
        rows = combiner_result["rows"]
        idx = project.ocpd.combiner_index
        if not rows:
            return 0.0
        row = rows[idx] if 0 <= idx < len(rows) else rows[0]
        return row["output_ampacity_a"]
    # pv_source
    return MODULE_SKUS[project.module.sku].isc


def compute_combiner_ocpd_switchboard(project: ProjectInput, num_inverters: int) -> tuple[dict, dict, dict]:
    combiner_result = (
        combiner_calc.size_combiners(project.combiner_rows, project.module.max_series_fuse_rating_a)
        if project.inverter.dc_topology == "combiner"
        else {"rows": [], "combiner_count": 0, "total_strings": 0, "max_output_ampacity_a": 0.0}
    )

    ocpd_continuous_current = (
        project.ocpd.continuous_current_override_a
        if project.ocpd.continuous_current_override_a is not None
        else resolve_ocpd_continuous_current(project, combiner_result)
    )
    ocpd_result = ocpd_calc.size_ocpd(
        continuous_current_a=ocpd_continuous_current,
        circuit=project.ocpd.circuit,
        manufacturer_max_ocpd_a=project.inverter.manufacturer_max_ocpd_a,
    )

    switchboard_result = switchboard_calc.check_120_percent_rule(
        busbar_rating_a=project.switchboard.busbar_rating_a,
        main_rating_a=project.switchboard.main_rating_a,
        backfed_ratings_a=[ocpd_result["standard_size_a"]] * num_inverters,
    )

    return combiner_result, ocpd_result, switchboard_result
