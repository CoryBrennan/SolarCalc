"""Module electrical catalog — ReneSola RS9-700~720NBG-E1 2384+1303+33.pdf.

Ported from the HMI draft's MODULE_SKUS table (five wattage bins of the same
bifacial module family). Temp coefficients are shared across the family
(same datasheet Characteristics table).
"""

from __future__ import annotations

from pydantic import BaseModel


class ModuleElectricalSpec(BaseModel):
    pmax: float
    voc: float
    vmp: float
    isc: float
    imp: float
    bifacial_pmax: float


MODULE_SKUS: dict[str, ModuleElectricalSpec] = {
    "700": ModuleElectricalSpec(pmax=700, voc=48.60, vmp=40.50, isc=18.32, imp=17.29, bifacial_pmax=770),
    "705": ModuleElectricalSpec(pmax=705, voc=48.80, vmp=40.70, isc=18.36, imp=17.33, bifacial_pmax=776),
    "710": ModuleElectricalSpec(pmax=710, voc=49.00, vmp=40.90, isc=18.40, imp=17.36, bifacial_pmax=781),
    "715": ModuleElectricalSpec(pmax=715, voc=49.20, vmp=41.10, isc=18.44, imp=17.40, bifacial_pmax=787),
    "720": ModuleElectricalSpec(pmax=720, voc=49.40, vmp=41.30, isc=18.49, imp=17.44, bifacial_pmax=792),
}

TEMP_COEFF_VOC_PCT_PER_C = -0.24
TEMP_COEFF_ISC_PCT_PER_C = 0.04
