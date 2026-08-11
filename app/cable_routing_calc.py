"""Routing-condition-aware ampacity + voltage-drop sizing for a single
circuit run, integrated with app/raceway_calc.py for real physical raceway
sizing on top of the derate/conductor decision.

Closes the gap flagged in memory/pvcase_integration_gaps.md: PVCase's BOM
export gives one length number per circuit (module-connector-to-endpoint)
with no way to say how much of that run is above-ground (cable tray/hanger,
no raceway-fill penalty) vs. in conduit (Table 310.15(C)(1) fill adjustment
applies). A flat "length from PVCase" fed straight into ampacity_calc as one
uniform installation condition understates the real derating whenever any
part of the run is actually in conduit.

A circuit is described as a bag of `RoutingLeg`s (installation method +
length + per-leg conductor count + optional ambient override) -- order
doesn't matter, since NEC sizing cares about the *worst* leg, not the
sequence: a single continuous conductor has to be sized for the most
restrictive point along its run, so the governing leg is whichever one
demands the highest ampacity, and that conductor is used for the whole
circuit. Voltage drop is computed leg-by-leg and summed (not one flat
length calc) because a leg's own installation method can add its own
penalty -- an AC circuit through steel conduit gets raceway_calc's
reactance multiplier, PVC/tray/free-air doesn't -- so two runs of the same
total length can have different real voltage drop depending on how much of
each is in steel conduit.

`RoutingLegTemplate` is the reusable, per-circuit-*type* version of a leg,
used with apply_routing_template() to turn one PVCase-derived total length
into concrete legs -- e.g. "15 ft free-air near the equipment, then conduit
for whatever's left" applies the same way to a 55 ft run and a 596 ft run.

Integration with app/raceway_calc.py: once the governing leg and final
(post-voltage-drop) conductor are known, each conduit leg gets a real trade
size via raceway_calc.size_conduit(), and each free-air leg explicitly
marked `size_as_tray=True` gets a real tray width via
raceway_calc.size_cable_tray() -- both sized against the *final* conductor,
since it's one continuous conductor and every leg's raceway has to fit it,
not just whatever that leg's own ampacity-only pick would have been. Every
leg's raceway/voltage-drop math reuses raceway_calc's own constants
(METAL_CONDUIT_MATERIALS, VD_STEEL_CONDUIT_AC_MULTIPLIER) and functions
rather than re-deriving them, so the two modules can't drift apart. A true
free-air leg (size_as_tray=False, e.g. a whip strung along rack framing)
gets no physical raceway sizing at all -- there's nothing to size.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

from app.ampacity_calc import fill_adjustment_factor, temp_correction_factor
from app.conductor_tables import CIRCULAR_MILS, CONDUCTOR_ORDER, select_conductor
from app.raceway_calc import (
    METAL_CONDUIT_MATERIALS,
    VD_STEEL_CONDUIT_AC_MULTIPLIER,
    size_cable_tray,
    size_conduit,
)
from app.voltage_drop_calc import K_COPPER_OHM_CMIL_PER_FT

InstallationMethod = Literal["free_air_or_tray", "conduit_above_grade", "conduit_below_grade"]
ConduitMaterial = Literal["EMT", "IMC", "RMC", "PVC_SCH40", "PVC_SCH80"]
TrayType = Literal["ladder", "ventilated_trough", "solid_bottom"]
ConductorInsulation = Literal["THHN_THWN2", "USE2_RHW2"]

# Cable tray / free-air runs don't get the Table 310.15(C)(1) raceway-fill
# penalty (that table applies to conductors bundled in a raceway or cable);
# both conduit methods do. Ambient stays ASHRAE-based for every method by
# default -- a buried conduit's real ambient is soil temperature, not air
# temperature, but this app has no verified soil-temp reference data, so
# that difference is left to the engineer's ambient_c_override rather than
# guessed at.
_RACEWAY_METHODS: frozenset[str] = frozenset({"conduit_above_grade", "conduit_below_grade"})

_LEG_PASSTHROUGH_FIELDS = (
    "ambient_c_override", "conductor_count", "conductor_insulation",
    "conduit_material", "is_nipple", "size_as_tray", "tray_type", "tray_width_in",
)


class RoutingLeg(BaseModel):
    installation_method: InstallationMethod = "free_air_or_tray"
    length_ft: float = 0.0
    ambient_c_override: float | None = None
    conductor_count: int = 2
    conductor_insulation: ConductorInsulation = "USE2_RHW2"

    # Conduit legs (installation_method is one of the two conduit_* values)
    # always get a real trade-size spec. free_air_or_tray legs only get
    # physically sized as a cable tray when size_as_tray=True -- true free
    # air (e.g. a whip strung along rack framing) has nothing to size.
    conduit_material: ConduitMaterial = "PVC_SCH40"
    is_nipple: bool = False
    size_as_tray: bool = False
    tray_type: TrayType = "ladder"
    tray_width_in: float = 12.0


class RoutingLegTemplate(BaseModel):
    """`fixed_length_ft = None` marks the one "remainder" leg that absorbs
    whatever length is left after the fixed legs -- see apply_routing_template()."""

    installation_method: InstallationMethod = "conduit_below_grade"
    fixed_length_ft: float | None = None
    ambient_c_override: float | None = None
    conductor_count: int = 2
    conductor_insulation: ConductorInsulation = "USE2_RHW2"
    conduit_material: ConduitMaterial = "PVC_SCH40"
    is_nipple: bool = False
    size_as_tray: bool = False
    tray_type: TrayType = "ladder"
    tray_width_in: float = 12.0


def _leg_from_template(template: RoutingLegTemplate, length_ft: float) -> RoutingLeg:
    kwargs = {field: getattr(template, field) for field in _LEG_PASSTHROUGH_FIELDS}
    return RoutingLeg(installation_method=template.installation_method, length_ft=length_ft, **kwargs)


def apply_routing_template(total_length_ft: float, templates: list[RoutingLegTemplate]) -> tuple[list[RoutingLeg], list[str]]:
    warnings: list[str] = []
    fixed = [t for t in templates if t.fixed_length_ft is not None]
    remainder_templates = [t for t in templates if t.fixed_length_ft is None]

    if len(remainder_templates) > 1:
        warnings.append(
            f"{len(remainder_templates)} remainder legs in template (only one is allowed) -- "
            "all but the first were treated as 0 ft."
        )

    fixed_total = sum(t.fixed_length_ft for t in fixed)
    remainder_ft = total_length_ft - fixed_total

    legs: list[RoutingLeg] = [_leg_from_template(t, t.fixed_length_ft) for t in fixed]

    if remainder_templates:
        first = remainder_templates[0]
        if remainder_ft < 0:
            warnings.append(
                f"Fixed legs total {fixed_total:.1f} ft, longer than this segment's actual "
                f"{total_length_ft:.1f} ft -- remainder leg set to 0 ft rather than a negative length."
            )
        legs.append(_leg_from_template(first, max(remainder_ft, 0.0)))
        for extra in remainder_templates[1:]:
            legs.append(_leg_from_template(extra, 0.0))
    elif abs(remainder_ft) > 0.01:
        warnings.append(
            f"Template has no remainder leg and its fixed legs total {fixed_total:.1f} ft, "
            f"which doesn't match this segment's actual {total_length_ft:.1f} ft "
            f"({'short by' if remainder_ft > 0 else 'over by'} {abs(remainder_ft):.1f} ft)."
        )

    return legs, warnings


def leg_derate_factor(leg: RoutingLeg, default_ambient_c: float, insulation_rating: int) -> dict:
    ambient_c = leg.ambient_c_override if leg.ambient_c_override is not None else default_ambient_c
    temp_factor = temp_correction_factor(ambient_c, insulation_rating)
    fill_factor = fill_adjustment_factor(leg.conductor_count) if leg.installation_method in _RACEWAY_METHODS else 1.0
    return {"ambient_c": ambient_c, "temp_factor": temp_factor, "fill_factor": fill_factor, "combined": temp_factor * fill_factor}


def _leg_vd_multiplier(leg: RoutingLeg, circuit_type: Literal["ac", "dc"]) -> float:
    """Steel (magnetic) conduit adds inductive reactance an AC circuit sees
    but PVC/tray/free-air doesn't -- reuses raceway_calc's own adder so this
    can't drift from what that module reports for the same leg."""
    if circuit_type == "ac" and leg.installation_method in _RACEWAY_METHODS and leg.conduit_material in METAL_CONDUIT_MATERIALS:
        return VD_STEEL_CONDUIT_AC_MULTIPLIER
    return 1.0


def _leg_vd_volts(leg: RoutingLeg, conductor: str, current_a: float, circuit_type: Literal["ac", "dc"]) -> float:
    cm = CIRCULAR_MILS[conductor]
    multiplier = _leg_vd_multiplier(leg, circuit_type)
    return (math.sqrt(3) * K_COPPER_OHM_CMIL_PER_FT * current_a * leg.length_ft * multiplier) / cm


def _total_voltage_drop(legs: list[RoutingLeg], conductor: str, current_a: float, voltage_v: float, circuit_type: Literal["ac", "dc"]) -> tuple[float, float]:
    volts = sum(_leg_vd_volts(leg, conductor, current_a, circuit_type) for leg in legs)
    pct = (volts / voltage_v) * 100 if voltage_v else 0.0
    return volts, pct


def _check_and_upsize_multileg(
    legs: list[RoutingLeg],
    current_a: float,
    voltage_v: float,
    limit_pct: float,
    starting_conductor: str,
    circuit_type: Literal["ac", "dc"],
) -> dict:
    """Same shape and upsize algorithm as voltage_drop_calc.check_segment(),
    but sums leg-by-leg volts (so a per-leg steel-conduit multiplier can
    apply) instead of one flat length x one multiplier calc."""
    volts, pct = _total_voltage_drop(legs, starting_conductor, current_a, voltage_v, circuit_type)
    final_conductor = starting_conductor
    upsized = False

    if pct > limit_pct:
        idx = CONDUCTOR_ORDER.index(starting_conductor)
        for candidate in CONDUCTOR_ORDER[idx + 1:]:
            candidate_volts, candidate_pct = _total_voltage_drop(legs, candidate, current_a, voltage_v, circuit_type)
            if candidate_pct <= limit_pct:
                volts, pct, final_conductor, upsized = candidate_volts, candidate_pct, candidate, True
                break
        # If no candidate clears the limit, volts/pct/final_conductor stay at
        # the *starting* conductor's values -- matches check_segment's own
        # documented behavior, not an oversight here either.

    return {
        "starting_conductor": starting_conductor,
        "final_conductor": final_conductor,
        "upsized": upsized,
        "voltage_drop_v": round(volts, 2),
        "voltage_drop_pct": round(pct, 2),
        "passes": pct <= limit_pct,
    }


def _leg_raceway_spec(leg: RoutingLeg, conductor: str) -> dict | None:
    if leg.installation_method in _RACEWAY_METHODS:
        return size_conduit(conductor, leg.conductor_insulation, leg.conductor_count, leg.conduit_material, leg.is_nipple)
    if leg.installation_method == "free_air_or_tray" and leg.size_as_tray:
        return size_cable_tray(conductor, leg.conductor_insulation, leg.conductor_count, leg.tray_type, leg.tray_width_in)
    return None


def size_cable_with_routing(
    current_a: float,
    voltage_v: float,
    insulation_rating: Literal[75, 90],
    voltage_drop_limit_pct: float,
    legs: list[RoutingLeg],
    default_ambient_c: float,
    circuit_type: Literal["ac", "dc"] = "dc",
) -> dict:
    if not legs:
        raise ValueError("size_cable_with_routing requires at least one leg")

    per_leg = []
    governing_idx = 0
    governing_required_a = -1.0
    for i, leg in enumerate(legs):
        derate = leg_derate_factor(leg, default_ambient_c, insulation_rating)
        required_a = current_a / derate["combined"] if derate["combined"] > 0 else float("inf")
        per_leg.append({
            "installation_method": leg.installation_method,
            "length_ft": leg.length_ft,
            "conductor_count": leg.conductor_count,
            **derate,
            "required_ampacity_a": round(required_a, 2),
        })
        if required_a > governing_required_a:
            governing_required_a = required_a
            governing_idx = i

    conductor = select_conductor(governing_required_a, insulation_rating)
    total_length_ft = sum(leg.length_ft for leg in legs)

    # select_conductor returns None if even the largest table entry (750
    # kcmil) can't clear the governing leg's required ampacity -- matches
    # ampacity_calc.size_conductor's own convention of surfacing that as
    # None rather than silently substituting an undersized conductor.
    vd_result = (
        _check_and_upsize_multileg(legs, current_a, voltage_v, voltage_drop_limit_pct, conductor, circuit_type)
        if conductor is not None
        else None
    )

    # The conductor that actually satisfies both checks -- ampacity alone
    # (`selected_conductor`) can be undersized once voltage-drop's
    # length-driven upsize is applied, so callers that want "the real answer"
    # should read this, not selected_conductor.
    final_conductor = vd_result["final_conductor"] if vd_result is not None else conductor

    # Physical raceway sizing per leg, against the FINAL whole-run conductor
    # (raceway_calc.size_conduit/size_cable_tray) -- every leg carries the
    # same continuous conductor, so a leg's raceway has to fit that wire,
    # not whatever that leg's own (possibly smaller) ampacity-only pick was.
    if final_conductor is not None:
        for leg, leg_dict in zip(legs, per_leg):
            leg_dict["raceway"] = _leg_raceway_spec(leg, final_conductor)
    else:
        for leg_dict in per_leg:
            leg_dict["raceway"] = None

    return {
        "legs": per_leg,
        "governing_leg_index": governing_idx,
        "governing_required_ampacity_a": round(governing_required_a, 2),
        "total_length_ft": round(total_length_ft, 1),
        "selected_conductor": conductor,
        "final_conductor": final_conductor,
        "voltage_drop": vd_result,
    }
