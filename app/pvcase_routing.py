"""Applies a per-circuit-type routing template (app/cable_routing_calc.py)
across every real segment in a parsed PVCase BOM (app/pvcase_bom_import.py),
finding the governing (worst-case) segment per circuit type -- the concrete
answer to memory/pvcase_integration_gaps.md's routing-condition gap, sized
against real per-tag lengths rather than a single sitewide placeholder.

Current/voltage per circuit type, simplifications flagged rather than hidden:

- transformer_to_inverter (AC): current = InverterSpec.max_output_current_a,
  voltage = InverterSpec.nominal_ac_voltage_v -- the same source /calculate
  already uses for its own inverter-to-switchboard voltage-drop check.
- inverter_to_combiner (DC): current = combiner_calc's max_output_ampacity_a
  (the worst combiner row on the project), voltage = InverterSpec.max_dc_voltage_v.
  Only meaningful when dc_topology == "combiner" -- "direct" MPPT topology
  has no DC-combiner segments at all, so this circuit is skipped for it.
- combiner_to_string (DC): current = one string's Isc x 1.25 (matches
  ampacity_calc's own dc_source formula), voltage = InverterSpec.max_dc_voltage_v.

Using max_dc_voltage_v (not each string's actual cold/hot Voc/Vmp) as the DC
voltage-drop denominator for both DC circuit types is a deliberate
simplification, same spirit as EtapAssumptions' sitewide conductor/length
defaults elsewhere in this app -- not a claim every string's real operating
voltage is identical.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.cable_routing_calc import RoutingLegTemplate, apply_routing_template, size_cable_with_routing
from app import module_catalog
from app.combiner_calc import size_combiners
from app.conductor_tables import CONDUCTOR_ORDER
from app.models import ProjectInput
from app.pvcase_bom_import import CableSegment, PvcaseBomData

_CIRCUIT_TYPES = ["transformer_to_inverter", "inverter_to_combiner", "combiner_to_string"]
_MAX_WARNINGS_PER_CIRCUIT = 10

# transformer_to_inverter is always the AC run; the other two are always DC
# -- not something an assumption needs to configure, since it's fixed by
# what the circuit physically is. Drives raceway_calc's steel-conduit
# voltage-drop reactance adder, which only applies to AC.
_CIRCUIT_AC_DC: dict[str, Literal["ac", "dc"]] = {
    "transformer_to_inverter": "ac",
    "inverter_to_combiner": "dc",
    "combiner_to_string": "dc",
}


class CircuitRoutingAssumption(BaseModel):
    insulation_rating: Literal[75, 90] = 90
    voltage_drop_limit_pct: float = 2.0
    legs: list[RoutingLegTemplate] = Field(default_factory=lambda: [RoutingLegTemplate()])


class PvcaseRoutingAssumptions(BaseModel):
    transformer_to_inverter: CircuitRoutingAssumption = Field(
        default_factory=lambda: CircuitRoutingAssumption(
            legs=[RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None)]
        )
    )
    inverter_to_combiner: CircuitRoutingAssumption = Field(
        default_factory=lambda: CircuitRoutingAssumption(
            legs=[RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None)]
        )
    )
    combiner_to_string: CircuitRoutingAssumption = Field(
        default_factory=lambda: CircuitRoutingAssumption(
            legs=[RoutingLegTemplate(installation_method="free_air_or_tray", fixed_length_ft=None)]
        )
    )


class PvcaseRoutingRequest(BaseModel):
    project: ProjectInput
    bom_path: str
    assumptions: PvcaseRoutingAssumptions = Field(default_factory=PvcaseRoutingAssumptions)


def _circuit_current_voltage(circuit: str, project: ProjectInput) -> tuple[float, float] | None:
    if circuit == "transformer_to_inverter":
        return project.inverter.max_output_current_a, project.inverter.nominal_ac_voltage_v
    if circuit == "inverter_to_combiner":
        if project.inverter.dc_topology != "combiner":
            return None
        combiner_result = size_combiners(project.combiner_rows, project.module.max_series_fuse_rating_a, project.module)
        return combiner_result["max_output_ampacity_a"], project.inverter.max_dc_voltage_v
    if circuit == "combiner_to_string":
        module = module_catalog.resolve_module_spec(project.module.sku, project.module)
        return module.isc * 1.25, project.inverter.max_dc_voltage_v
    raise ValueError(f"Unknown circuit type: {circuit!r}")


def _segments_for_circuit(bom: PvcaseBomData, circuit: str) -> list[CableSegment]:
    return getattr(bom, circuit)


def compute_circuit_routing_report(
    circuit: str,
    segments: list[CableSegment],
    current_a: float,
    voltage_v: float,
    assumption: CircuitRoutingAssumption,
    default_ambient_c: float,
    circuit_type: Literal["ac", "dc"] = "dc",
) -> dict:
    if not segments:
        return {
            "circuit": circuit,
            "segment_count": 0,
            "governing_segment": None,
            "selected_conductor": None,
            "final_conductor": None,
            "voltage_drop": None,
            "warnings": ["No segments of this circuit type in the BOM."],
        }

    worst_segment: CableSegment | None = None
    worst_result: dict | None = None
    worst_rank = -1
    warnings: list[str] = []
    total_warning_count = 0

    for seg in segments:
        legs, leg_warnings = apply_routing_template(seg.length_ft, assumption.legs)
        for w in leg_warnings:
            total_warning_count += 1
            if len(warnings) < _MAX_WARNINGS_PER_CIRCUIT:
                warnings.append(f"{seg.from_tag} -> {seg.to_tag}: {w}")

        result = size_cable_with_routing(
            current_a=current_a,
            voltage_v=voltage_v,
            insulation_rating=assumption.insulation_rating,
            voltage_drop_limit_pct=assumption.voltage_drop_limit_pct,
            legs=legs,
            default_ambient_c=default_ambient_c,
            circuit_type=circuit_type,
        )
        # Ampacity's required-current figure alone can't tell segments apart
        # -- it only depends on current/derate, not length -- so "worst" has
        # to be judged by the actual conductor this segment ends up needing
        # once voltage-drop's length-driven upsize is applied. A conductor
        # of None (nothing in the table clears it) is the ultimate worst case.
        conductor = result["final_conductor"]
        rank = len(CONDUCTOR_ORDER) if conductor is None else CONDUCTOR_ORDER.index(conductor)
        if rank > worst_rank:
            worst_rank = rank
            worst_segment, worst_result = seg, result

    if total_warning_count > len(warnings):
        warnings.append(f"...and {total_warning_count - len(warnings)} more segment(s) with template-fit warnings.")

    return {
        "circuit": circuit,
        "segment_count": len(segments),
        "governing_segment": {
            "from_tag": worst_segment.from_tag,
            "to_tag": worst_segment.to_tag,
            "length_ft": round(worst_segment.length_ft, 1),
        },
        **worst_result,
        "warnings": warnings,
    }


def compute_routing_report(project: ProjectInput, bom: PvcaseBomData, assumptions: PvcaseRoutingAssumptions) -> dict:
    default_ambient_c = project.ashrae.max_design_temp_c
    circuits = []
    for circuit in _CIRCUIT_TYPES:
        cv = _circuit_current_voltage(circuit, project)
        assumption = getattr(assumptions, circuit)
        if cv is None:
            circuits.append({
                "circuit": circuit,
                "segment_count": 0,
                "governing_segment": None,
                "selected_conductor": None,
                "final_conductor": None,
                "voltage_drop": None,
                "warnings": [f"Skipped -- {circuit} doesn't apply (dc_topology={project.inverter.dc_topology!r})."],
            })
            continue
        current_a, voltage_v = cv
        segments = _segments_for_circuit(bom, circuit)
        circuits.append(
            compute_circuit_routing_report(
                circuit, segments, current_a, voltage_v, assumption, default_ambient_c,
                circuit_type=_CIRCUIT_AC_DC[circuit],
            )
        )
    return {"circuits": circuits}
