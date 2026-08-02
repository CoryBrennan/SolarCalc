"""Translates a module's STC datasheet numbers to the irradiance and cell
temperature present on test day, then validates a field-tracer reading
against that expected point — not against the raw STC nameplate.

Translation is a simplified single-point scaling, not a full IEC 60891
curve-shape translation.
"""

from __future__ import annotations

from app.module_catalog import MODULE_SKUS, TEMP_COEFF_ISC_PCT_PER_C, TEMP_COEFF_VOC_PCT_PER_C


def expected_iv_point(module_sku: str, irradiance_w_m2: float, cell_temp_c: float, modules_per_string: int) -> dict:
    module = MODULE_SKUS[module_sku]
    g = irradiance_w_m2 / 1000
    dt = cell_temp_c - 25

    voc_module = module.voc * (1 + TEMP_COEFF_VOC_PCT_PER_C / 100 * dt)
    vmp_module = module.vmp * (1 + TEMP_COEFF_VOC_PCT_PER_C / 100 * dt)
    isc_module = module.isc * g * (1 + TEMP_COEFF_ISC_PCT_PER_C / 100 * dt)
    imp_module = module.imp * g * (1 + TEMP_COEFF_ISC_PCT_PER_C / 100 * dt)

    voc = round(voc_module * modules_per_string, 2)
    isc = round(isc_module, 2)
    vmp = round(vmp_module * modules_per_string, 2)
    imp = round(imp_module, 2)

    return {"voc": voc, "isc": isc, "vmp": vmp, "imp": imp, "pmp": round(vmp * imp, 1)}


def validate_reading(expected: dict, measured: dict, tolerance_pct: float) -> dict:
    checks = []
    all_pass = True
    for name, unit in [("voc", "V"), ("isc", "A"), ("vmp", "V"), ("imp", "A")]:
        expected_value = expected[name]
        measured_value = measured[name]
        deviation_pct = round(abs(measured_value - expected_value) / expected_value * 1000) / 10 if expected_value else 0
        passes = deviation_pct <= tolerance_pct
        all_pass = all_pass and passes
        checks.append(
            {
                "parameter": name, "unit": unit, "expected": expected_value,
                "measured": measured_value, "deviation_pct": deviation_pct, "passes": passes,
            }
        )
    return {"checks": checks, "all_pass": all_pass}
