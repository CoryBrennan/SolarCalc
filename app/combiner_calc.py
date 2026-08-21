"""DC combiner sizing — any number of combiners per inverter, each with its own
module, input (string) count, and busbar rating. Output circuit ampacity is
the sum of that row's input fuse ratings (NEC 690.9), not raw Isc x 1.25 x
inputs — matching how a combiner output is conventionally sized once each
input is individually fused.
"""

from __future__ import annotations

from app import module_catalog
from app.conductor_tables import next_standard_size, select_conductor
from app.models import CombinerRow, ModuleSpec


def compute_combiner_row(
    row: CombinerRow, max_series_fuse_rating_a: float, module_fallback: ModuleSpec | None = None
) -> dict:
    module = module_catalog.resolve_module_spec(row.module_sku, module_fallback)
    input_min_fuse_a = module.isc * 1.25
    input_fuse_a = min(next_standard_size(input_min_fuse_a), max_series_fuse_rating_a)
    output_ampacity_a = input_fuse_a * row.inputs
    output_conductor = select_conductor(output_ampacity_a, 90)
    bus_passes = row.bus_rating_a >= output_ampacity_a

    return {
        "module_sku": row.module_sku,
        "inputs": row.inputs,
        "input_min_fuse_a": round(input_min_fuse_a, 2),
        "input_fuse_a": input_fuse_a,
        "input_fuse_under_minimum": input_fuse_a < input_min_fuse_a,
        "output_ampacity_a": round(output_ampacity_a, 2),
        "output_conductor": output_conductor,
        "bus_rating_a": row.bus_rating_a,
        "bus_passes": bus_passes,
    }


def size_combiners(
    rows: list[CombinerRow], max_series_fuse_rating_a: float, module_fallback: ModuleSpec | None = None
) -> dict:
    computed_rows = [compute_combiner_row(row, max_series_fuse_rating_a, module_fallback) for row in rows]
    total_strings = sum(row.inputs for row in rows)
    max_output_ampacity_a = max((r["output_ampacity_a"] for r in computed_rows), default=0)

    return {
        "rows": computed_rows,
        "combiner_count": len(rows),
        "total_strings": total_strings,
        "max_output_ampacity_a": round(max_output_ampacity_a, 2),
    }
