"""Generator input contract for the inverter AC block — the AC-side one-line
representation of an inverter: its output breaker, the feeder riser up to the
switchboard bus, the AC terminal, a detail callout, and the nameplate data box.

This is the AC half of the AC/DC split described in the inverter DC block
spec; the two are separate blocks linked by a shared TAG1.

Geometry is fixed (unlike the switchboard/DC blocks, nothing about the layout
varies with project data — only the printed values do), so these emit
"attribute_update" changesets rather than "regenerate", same as the
transformer and MV devices.

One config per physical inverter: INV-1 .. INV-N, matching the position tags
switchboard_block already generates for the same inverters.
"""

from __future__ import annotations

from app import module_catalog, ocpd_calc
from app.models import ProjectInput


def format_phase_config(phases: int, wires: int, equipment_ground: bool) -> str:
    """e.g. (3, 3, True) -> "3Ø 3W + PE".

    Ø is U+00D8 (Latin-1), not the Greek phi — it's the phase glyph used on US
    electrical drawings and, being inside Latin-1, it renders in AutoCAD's
    stock SHX fonts where a Greek phi would not.
    """
    base = f"{phases}Ø {wires}W"
    return f"{base} + PE" if equipment_ground else base


def per_inverter_dc_capacity_w(project: ProjectInput) -> float:
    """Nameplate DC behind one inverter.

    The project models the array fleet-wide (a total module count) rather than
    per-inverter, so this is the even split: total DC / inverter count. That is
    exact when the array is balanced across inverters and approximate when it
    isn't — the project has no per-inverter module assignment to do better.
    """
    if project.inverter.quantity <= 0:
        return 0.0
    total_dc_w = module_catalog.resolve_module_spec(project.module.sku, project.module).pmax * project.module.quantity
    return total_dc_w / project.inverter.quantity


def build_inverter_ac_configs(project: ProjectInput) -> list[dict]:
    inverter = project.inverter

    # Sized from the inverter's own max output current, deliberately NOT from
    # the shared compute_combiner_ocpd_switchboard() result: that one follows
    # the user's OcpdInput.circuit selector, which may be pointed at a DC
    # circuit. The AC block's breaker is always the inverter output breaker.
    ocpd = ocpd_calc.size_ocpd(
        continuous_current_a=inverter.max_output_current_a,
        circuit="inverter_output",
        manufacturer_max_ocpd_a=inverter.manufacturer_max_ocpd_a,
    )
    ocpd_rating = f"{ocpd['standard_size_a']:g} A/{inverter.phases}"

    kwac = f"{inverter.ac_rating_w / 1000:.3f} KWAC"
    kwdc = f"{per_inverter_dc_capacity_w(project) / 1000:.3f} KWDC"
    vac = f"{inverter.nominal_ac_voltage_v:g} VAC"
    phase_config = format_phase_config(inverter.phases, inverter.ac_wires, inverter.ac_equipment_ground)

    configs = []
    for i in range(1, inverter.quantity + 1):
        tag = f"INV-{i}"
        configs.append(
            {
                "tag": tag,
                "ocpd_rating": ocpd_rating,
                "ocpd_within_mfr_max": ocpd["manufacturer_check_ok"],
                "detail_ref": inverter.ac_detail_ref,
                "attributes": {
                    "TAG1": tag,
                    "MFG": inverter.manufacturer.upper(),
                    "CAT": inverter.catalog_number.upper(),
                    "KWAC": kwac,
                    "KWDC": kwdc,
                    "VAC": vac,
                    "PHASE_CONFIG": phase_config,
                },
            }
        )
    return configs
