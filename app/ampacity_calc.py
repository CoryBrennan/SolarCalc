"""Conductor ampacity sizing — NEC 690.8 (PV circuits) / 705.28 (inverter output),
derated per Table 310.15(B)(1) (ambient temperature) and Table 310.15(C)(1)
(conduit fill), mirroring the HMI draft's Ampacity & Conductor Sizing panel.
"""

from __future__ import annotations

from app import module_catalog
from app.conductor_tables import select_conductor
from app.models import AmpacityInput, ASHRAESiteData, InverterSpec, ModuleSpec

# Table 310.15(B)(1), ambient in °C, banded by upper bound of each range.
_TEMP_CORRECTION_75C: list[tuple[float, float]] = [
    (25, 1.05), (30, 1.00), (35, 0.94), (40, 0.88), (45, 0.82),
    (50, 0.75), (55, 0.67), (60, 0.58), (65, 0.47), (70, 0.33), (76, 0.30),
]
_TEMP_CORRECTION_90C: list[tuple[float, float]] = [
    (25, 1.04), (30, 1.00), (35, 0.96), (40, 0.91), (45, 0.87),
    (50, 0.82), (55, 0.76), (60, 0.71), (65, 0.65), (70, 0.58), (76, 0.50),
]


def temp_correction_factor(ambient_c: float, insulation_rating: int) -> float:
    bands = _TEMP_CORRECTION_90C if insulation_rating == 90 else _TEMP_CORRECTION_75C
    for upper_bound, factor in bands:
        if ambient_c <= upper_bound:
            return factor
    return bands[-1][1]


def fill_adjustment_factor(conductor_count: int) -> float:
    """Table 310.15(C)(1), banded (simplified — not the full interpolated table)."""
    if conductor_count <= 3:
        return 1.00
    if conductor_count <= 6:
        return 0.80
    if conductor_count <= 9:
        return 0.70
    if conductor_count <= 20:
        return 0.50
    if conductor_count <= 30:
        return 0.45
    if conductor_count <= 40:
        return 0.40
    return 0.35


def size_conductor(
    module_sku: str,
    inverter: InverterSpec,
    ashrae: ASHRAESiteData,
    ampacity_input: AmpacityInput,
    module_fallback: ModuleSpec | None = None,
) -> dict:
    if ampacity_input.circuit_type == "dc_source":
        base_current = module_catalog.resolve_module_spec(module_sku, module_fallback).isc * 1.25
    elif ampacity_input.circuit_type == "dc_output":
        base_current = module_catalog.resolve_module_spec(module_sku, module_fallback).isc * 1.25 * ampacity_input.parallel_strings
    else:  # ac_output
        base_current = inverter.max_output_current_a * 1.25

    temp_factor = temp_correction_factor(ashrae.max_design_temp_c, ampacity_input.insulation_rating)
    fill_factor = fill_adjustment_factor(ampacity_input.conductor_count)
    required_ampacity = base_current / (temp_factor * fill_factor)
    conductor = select_conductor(required_ampacity, ampacity_input.insulation_rating)

    return {
        "base_continuous_current_a": round(base_current, 2),
        "temp_correction_factor": temp_factor,
        "fill_adjustment_factor": fill_factor,
        "required_ampacity_a": round(required_ampacity, 2),
        "selected_conductor": conductor,
    }
