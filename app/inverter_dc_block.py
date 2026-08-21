"""Generator input contract for the inverter DC block (per-MPPT string
layout, used when the inverter takes PV source circuits directly) and the
DC combiner block (single string cluster per physical combiner) — the same
generator on the AutoCAD side handles both, distinguished by block_variant.

The combiner variant is derived straight from the real combiner schedule
(combiner_calc) already built for DC Combiner Sizing; the mppt variant uses
a representative mppt_count / strings-per-mppt since there's no per-MPPT
string breakdown modeled yet.
"""

from __future__ import annotations

from app import combiner_calc, module_catalog, ocpd_calc
from app.ampacity_calc import size_conductor
from app.conductor_tables import next_standard_size
from app.models import AmpacityInput, ProjectInput


def _string_conductor(project: ProjectInput, module_sku: str) -> str | None:
    ampacity_input = AmpacityInput(
        circuit_type="dc_source",
        insulation_rating=project.ampacity.insulation_rating,
        conductor_count=project.ampacity.conductor_count,
    )
    result = size_conductor(module_sku, project.inverter, project.ashrae, ampacity_input, project.module)
    return result["selected_conductor"]


def build_inverter_dc_mppt_config(project: ProjectInput) -> dict:
    module = module_catalog.resolve_module_spec(project.module.sku, project.module)
    strings_per_mppt = project.inverter.strings_per_mppt_direct
    per_string_ocpd = ocpd_calc.size_ocpd(continuous_current_a=module.isc, circuit="pv_source")["standard_size_a"]
    conductor = _string_conductor(project, project.module.sku)

    mppt_groups = [
        {
            "mppt_index": mppt_index,
            "strings": [
                {"string_id": i, "ocpd_rating": f"{per_string_ocpd}A", "conductor_size": conductor}
                for i in range(1, strings_per_mppt + 1)
            ],
        }
        for mppt_index in range(1, project.inverter.mppt_count + 1)
    ]

    disconnect_current = strings_per_mppt * module.isc
    disconnect_rating = ocpd_calc.size_ocpd(continuous_current_a=disconnect_current, circuit="pv_source")["standard_size_a"]
    max_isc_per_mppt = round(strings_per_mppt * module.isc, 2)

    return {
        "tag": "INV-1",
        "block_variant": "mppt",
        "disconnect_placement": "per_mppt",
        "disconnect_rating": f"{disconnect_rating}A",
        "mppt_groups": mppt_groups,
        "max_dc_voltage": f"{project.inverter.max_dc_voltage_v:g}V",
        "max_isc_per_mppt": f"{max_isc_per_mppt}A",
    }


def build_inverter_dc_combiner_configs(project: ProjectInput) -> list[dict]:
    configs = []
    for i, row in enumerate(project.combiner_rows, start=1):
        computed = combiner_calc.compute_combiner_row(row, project.module.max_series_fuse_rating_a, project.module)
        conductor = _string_conductor(project, row.module_sku)
        input_fuse = computed["input_fuse_a"]
        disconnect_rating = next_standard_size(computed["output_ampacity_a"])

        configs.append(
            {
                "tag": f"DCC-{i}",
                "block_variant": "combiner",
                "disconnect_placement": "single",
                "disconnect_rating": f"{disconnect_rating:g}A",
                "mppt_groups": [
                    {
                        "mppt_index": 1,
                        "strings": [
                            {"string_id": s, "ocpd_rating": f"{input_fuse:g}A", "conductor_size": conductor}
                            for s in range(1, row.inputs + 1)
                        ],
                    }
                ],
            }
        )
    return configs
