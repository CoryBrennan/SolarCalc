"""Placarding requirements — NEC 690/705 markings for a ground-mount array with
no O&M building in the DC path, matched to PVLabels.com SKUs. Quantities are
derived from num_inverters (the HMI draft's JS version hardcoded these at
qty=15/16 for the one sample project; this generalizes it — verified to
reproduce the exact same $396.95 total at num_inverters=15).

Rapid shutdown (690.12) placards are omitted — that requirement only applies
to PV circuits on or in a building. DC-conduit marking (690.31(D)(2)) is
omitted until a home-run routing length is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlacardRequirement:
    ref: str
    purpose: str
    location: str
    sku: str
    description: str
    unit_price: float
    url: str


_REQUIREMENTS: list[PlacardRequirement] = [
    PlacardRequirement(
        "NEC 705.10", "Point of interconnection utility disconnect marking",
        "Main utility/POI disconnect switch", "04-428",
        "ONSITE GENERATION, UTILITY DISCONNECT, SWITCH PLACARD", 4.95,
        "https://www.pvlabels.com/on-site-generation-utility-disconnect-switch-placard-04-428/",
    ),
    PlacardRequirement(
        "NEC 690.13(B)", "PV system disconnect identification",
        "Each inverter's integrated DC disconnect", "03-327",
        "PHOTOVOLTAIC SYSTEM DISCONNECT LABEL", 0.70,
        "https://www.pvlabels.com/pv-system-disconnect-label-03-327/",
    ),
    PlacardRequirement(
        "NEC 690.13(B)", "PV system disconnect rated voltage/current marking",
        "Each inverter's integrated DC disconnect", "03-110",
        "PV SYSTEM DC DISCONNECT OPERATING VOLTAGE/CURRENT LABEL (write-in)", 1.10,
        "https://www.pvlabels.com/dc-disconnect-pv-system-voltage-vdc-current-amps-max-short-03-110/",
    ),
    PlacardRequirement(
        "NEC 690.53", "Inverter DC input (rated MPP current/voltage) marking",
        "Each inverter nameplate", "04-677",
        "INVERTER INPUT RATINGS RATED MPP CURRENT/VOLTAGE PLACARD (custom)", 7.70,
        "https://www.pvlabels.com/inverter-input-ratings-mpp-current-adc-voltage-vdc-04-677/",
    ),
    PlacardRequirement(
        "NEC 705.10", "Inverter AC output (rated current/voltage) marking",
        "Each inverter nameplate", "04-671",
        "INVERTER OUTPUT CONNECTION RATED CURRENT/VOLTAGE PLACARD (custom)", 6.60,
        "https://www.pvlabels.com/inverter-output-connection-current-amps-voltage-volts-custom-04-671/",
    ),
    PlacardRequirement(
        "Site convention", "Inverter sequence numbering for O&M/service",
        "Each inverter enclosure", "04-710",
        "INVERTER #_ SEQUENTIAL IDENTIFICATION PLACARD (custom)", 5.50,
        "https://www.pvlabels.com/inverter-number-1-16-inverters-custom-placard-04-710/",
    ),
]

_ARC_FLASH = PlacardRequirement(
    "NEC 110.16 / 70E", "Arc-flash hazard warning (fill in from the arc-flash study)",
    "POI switchgear + each inverter AC compartment", "05-545",
    "DANGER ARC FLASH HAZARD LABEL (12 write-in fields)", 4.25,
    "https://www.pvlabels.com/arc-flash-danger-12-input-fields-data-shock-risks-05-545/",
)


def determine_placard_requirements(num_inverters: int) -> dict:
    line_items = []

    # First requirement (POI utility disconnect) is always qty 1, regardless of
    # inverter count; the rest of the fixed list scales one-per-inverter.
    for i, req in enumerate(_REQUIREMENTS):
        qty = 1 if i == 0 else num_inverters
        line = qty * req.unit_price
        line_items.append(
            {
                "ref": req.ref, "purpose": req.purpose, "location": req.location,
                "sku": req.sku, "description": req.description, "qty": qty,
                "unit_price": req.unit_price, "line_total": round(line, 2), "url": req.url,
            }
        )

    arc_flash_qty = num_inverters + 1  # POI switchgear + each inverter AC compartment
    line_items.append(
        {
            "ref": _ARC_FLASH.ref, "purpose": _ARC_FLASH.purpose, "location": _ARC_FLASH.location,
            "sku": _ARC_FLASH.sku, "description": _ARC_FLASH.description, "qty": arc_flash_qty,
            "unit_price": _ARC_FLASH.unit_price, "line_total": round(arc_flash_qty * _ARC_FLASH.unit_price, 2),
            "url": _ARC_FLASH.url,
        }
    )

    total = round(sum(item["line_total"] for item in line_items), 2)
    return {"line_items": line_items, "estimated_total": total}
