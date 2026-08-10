"""PV string length range — mirrors the HMI's computeStringLengthRange()/
recomputeStringLength() on the PV String Design panel exactly: same
single-coefficient temp-correction approximation as iv_curve_calc (Voc and
Vmp both scaled by the module's Voc temp coefficient), just evaluated at
ASHRAE design temperatures instead of a field test-day reading.

Cold Voc (NEC 690.7 max system voltage) caps the max string length; hot Vmp
sagging below the inverter's MPPT minimum caps the min string length.
"""

from __future__ import annotations

import math

from app.models import ASHRAESiteData, ClientVoltageDropLimits, InverterSpec, ModuleSpec
from app.module_catalog import MODULE_SKUS

MPPT_LIFE_TARGET_YEARS = 15.0


def mppt_life_years(
    modules_per_string: int,
    vmp_per_module_hot: float,
    mppt_v_min: float,
    v_drop_frac: float,
    first_year_deg_frac: float,
    annual_deg_frac: float,
) -> float:
    """Years the hot-day string Vmp stays above the inverter's MPPT minimum,
    after the DC voltage-drop allowance, the datasheet's first-year degradation,
    and a fixed annual slice of that same starting voltage thereafter."""
    vmp_after_drop = vmp_per_module_hot * modules_per_string * (1 - v_drop_frac)
    vmp_after_year_one = vmp_after_drop * (1 - first_year_deg_frac)
    annual_loss_v = vmp_after_drop * annual_deg_frac
    if annual_loss_v <= 0:
        return math.inf
    if vmp_after_year_one <= mppt_v_min:
        return 0.0
    return 1 + (vmp_after_year_one - mppt_v_min) / annual_loss_v


def compute_string_length_range(
    module: ModuleSpec,
    inverter: InverterSpec,
    ashrae: ASHRAESiteData,
    voltage_drop_limits: ClientVoltageDropLimits,
) -> dict:
    d = MODULE_SKUS[module.sku]

    # Per-SKU coefficient (see module_catalog.py) -- was a shared global
    # constant until a second, real module family with a different Voc
    # coefficient (Znshine ZXM7-UHLDD144, -0.25%/C vs. RS9's -0.24%/C) was
    # added, which would otherwise size Znshine strings off the wrong number.
    voc_per_module_cold = d.voc * (1 + (d.temp_coeff_voc_pct_per_c / 100) * (ashrae.min_design_temp_c - 25))
    vmp_per_module_hot = d.vmp * (1 + (d.temp_coeff_voc_pct_per_c / 100) * (ashrae.avg_high_temp_c - 25))
    voltage_ceiling = min(module.max_system_voltage_v, inverter.max_dc_voltage_v)

    max_len = math.floor(voltage_ceiling / voc_per_module_cold) if voc_per_module_cold > 0 else 0
    min_len = math.ceil(inverter.mppt_v_min / vmp_per_module_hot) if vmp_per_module_hot > 0 else 0
    valid = max_len >= min_len and min_len > 0

    v_drop_frac = voltage_drop_limits.total_dc_pct / 100
    first_year_deg_frac = module.first_year_degradation_pct / 100
    annual_deg_frac = module.annual_degradation_pct / 100

    def life(n: int) -> float:
        return mppt_life_years(
            n, vmp_per_module_hot, inverter.mppt_v_min, v_drop_frac, first_year_deg_frac, annual_deg_frac
        )

    # Longer strings sit higher above the MPPT floor, so the longest length that
    # still clears the cold-Voc ceiling is also the longest-lived one.
    recommended = None
    if valid:
        for n in range(max_len, min_len - 1, -1):
            if life(n) >= MPPT_LIFE_TARGET_YEARS:
                recommended = n
                break

    return {
        "voc_per_module_cold_v": round(voc_per_module_cold, 4),
        "vmp_per_module_hot_v": round(vmp_per_module_hot, 4),
        "min_string_length": min_len,
        "max_string_length": max_len,
        "recommended_string_length": recommended,
        "mppt_life_target_years": MPPT_LIFE_TARGET_YEARS,
        "recommended_mppt_life_years": round(life(recommended), 2) if recommended else None,
    }
