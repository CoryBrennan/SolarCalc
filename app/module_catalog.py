"""Module electrical catalog. Started as a single family (ReneSola
RS9-700~720NBG-E1 2384+1303+33.pdf, ported from the HMI draft's MODULE_SKUS
table) — temp coefficients used to be two bare module-level constants
because every SKU shared one datasheet's Characteristics table. That broke
the moment a second, real family (Znshine PV-Tech ZXM7-UHLDD144, the actual
module on the Encore Brighton project — see
solar_calc_engine/iv_curve_calc.py's verified `.pvapx` ModuleDefinitions,
2026-08-10) needed its own, different coefficients. `ModuleElectricalSpec`
now carries its coefficients per-SKU; `TEMP_COEFF_VOC_PCT_PER_C`/
`TEMP_COEFF_ISC_PCT_PER_C` stay as the RS9 family's defaults so existing
callers/behavior for those SKUs don't change.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.models import ModuleSpec

TEMP_COEFF_VOC_PCT_PER_C = -0.24
TEMP_COEFF_ISC_PCT_PER_C = 0.04


class ModuleElectricalSpec(BaseModel):
    pmax: float
    voc: float
    vmp: float
    isc: float
    imp: float
    bifacial_pmax: float = 0.0
    temp_coeff_voc_pct_per_c: float = TEMP_COEFF_VOC_PCT_PER_C
    temp_coeff_isc_pct_per_c: float = TEMP_COEFF_ISC_PCT_PER_C


MODULE_SKUS: dict[str, ModuleElectricalSpec] = {
    "700": ModuleElectricalSpec(pmax=700, voc=48.60, vmp=40.50, isc=18.32, imp=17.29, bifacial_pmax=770),
    "705": ModuleElectricalSpec(pmax=705, voc=48.80, vmp=40.70, isc=18.36, imp=17.33, bifacial_pmax=776),
    "710": ModuleElectricalSpec(pmax=710, voc=49.00, vmp=40.90, isc=18.40, imp=17.36, bifacial_pmax=781),
    "715": ModuleElectricalSpec(pmax=715, voc=49.20, vmp=41.10, isc=18.44, imp=17.40, bifacial_pmax=787),
    "720": ModuleElectricalSpec(pmax=720, voc=49.40, vmp=41.30, isc=18.49, imp=17.44, bifacial_pmax=792),
    # Znshine PV-Tech ZXM7-UHLDD144, 580W -- STC electricals and the Voc temp
    # coefficient (-0.25%/C) pulled from the real Encore Brighton 1/2 .pvapx
    # project files (ModuleDefinitions), not a guess. Solmetric's own project
    # data doesn't carry an Isc coefficient for this module either (only
    # Beta/Voc and Gamma/Pmax were populated) -- temp_coeff_isc_pct_per_c
    # below is the RS9-family default (typical crystalline-silicon), not a
    # verified Znshine datasheet value. bifacial_pmax similarly isn't in
    # Solmetric's data -- left at the class default (0.0) rather than invented.
    "ZXM7-UHLDD144": ModuleElectricalSpec(
        pmax=580, voc=51.5, vmp=42.8, isc=14.35, imp=13.56,
        temp_coeff_voc_pct_per_c=-0.25, temp_coeff_isc_pct_per_c=TEMP_COEFF_ISC_PCT_PER_C,
    ),
}


def resolve_module_spec(module_sku: str, fallback: ModuleSpec | None = None) -> ModuleElectricalSpec:
    """Static catalog first. Falls back to the request's own inline
    electricals (ModuleSpec.pmax/voc/vmp/isc/imp/...) for a SKU that was
    never added to this file's small hardcoded set -- the common case now
    that the HMI's Module Spec panel has no default parts list of its own
    and every module is either manually entered or datasheet-approved.
    Only trusts the fallback when its own sku matches the one being
    resolved (so a combiner row referencing a genuinely different, unknown
    SKU still fails loudly instead of silently borrowing the main module's
    electricals) and it actually carries real data (pmax/voc/isc > 0).
    Raises KeyError, same as the plain dict lookup this replaces, when
    neither source has the SKU.
    """
    if module_sku in MODULE_SKUS:
        return MODULE_SKUS[module_sku]
    if (
        fallback is not None
        and fallback.sku == module_sku
        and fallback.pmax > 0
        and fallback.voc > 0
        and fallback.isc > 0
    ):
        return ModuleElectricalSpec(
            pmax=fallback.pmax, voc=fallback.voc, vmp=fallback.vmp,
            isc=fallback.isc, imp=fallback.imp, bifacial_pmax=fallback.bifacial_pmax,
            temp_coeff_voc_pct_per_c=fallback.temp_coeff_voc_pct_per_c,
            temp_coeff_isc_pct_per_c=fallback.temp_coeff_isc_pct_per_c,
        )
    raise KeyError(module_sku)
