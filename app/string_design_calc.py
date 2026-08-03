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

from app.models import ASHRAESiteData, InverterSpec, ModuleSpec
from app.module_catalog import MODULE_SKUS, TEMP_COEFF_VOC_PCT_PER_C


def compute_string_length_range(module: ModuleSpec, inverter: InverterSpec, ashrae: ASHRAESiteData) -> dict:
    d = MODULE_SKUS[module.sku]

    voc_per_module_cold = d.voc * (1 + (TEMP_COEFF_VOC_PCT_PER_C / 100) * (ashrae.min_design_temp_c - 25))
    vmp_per_module_hot = d.vmp * (1 + (TEMP_COEFF_VOC_PCT_PER_C / 100) * (ashrae.avg_high_temp_c - 25))
    voltage_ceiling = min(module.max_system_voltage_v, inverter.max_dc_voltage_v)

    max_len = math.floor(voltage_ceiling / voc_per_module_cold) if voc_per_module_cold > 0 else 0
    min_len = math.ceil(inverter.mppt_v_min / vmp_per_module_hot) if vmp_per_module_hot > 0 else 0
    valid = max_len >= min_len and min_len > 0

    return {
        "voc_per_module_cold_v": round(voc_per_module_cold, 4),
        "vmp_per_module_hot_v": round(vmp_per_module_hot, 4),
        "min_string_length": min_len,
        "max_string_length": max_len,
        "recommended_string_length": max_len if valid else None,
    }
