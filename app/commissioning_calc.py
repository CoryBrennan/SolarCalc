"""Pass/fail + rollup logic for field commissioning QC — app/commissioning_routes.py's
calculation layer, kept separate from the route handlers the same way
wire_cost_calc.py sits under its own routes.

The panel splits into two groups, and this module's rollup follows the same
split:
  - Visual & Mechanical Inspection: TorquePoint + InspectionItem
  - Electrical Inspection: WireInspectionItem + ElectricalReading

DEFAULT_TORQUE_CHECKLIST / DEFAULT_VISUAL_MECHANICAL_CHECKLIST are checklist
*labels* only, never values — see db_models.TorquePoint's docstring for why
this app doesn't ship invented manufacturer torque numbers. A visual/
mechanical item has no numeric spec to invent in the first place; a
technician just marks it pass/fail directly.
"""

from __future__ import annotations

from typing import Literal

from app.models import RacewayRun

Result = Literal["pending", "pass", "fail"]

DEFAULT_TORQUE_CHECKLIST: dict[str, list[str]] = {
    "inverter": ["DC input terminals", "AC output lugs", "Ground lug", "Disconnect switch lugs"],
    "switchboard": ["Main breaker lugs", "Branch breaker lugs", "Bus splice bolts", "Neutral bar", "Ground bar"],
    "load_center": ["Main breaker lugs", "Branch breaker lugs", "Neutral bar", "Ground bar"],
}

DEFAULT_VISUAL_MECHANICAL_CHECKLIST: dict[str, list[str]] = {
    "inverter": [
        "Enclosure condition / no physical damage",
        "Nameplate present & legible",
        "Conduit entries sealed",
        "Mounting/anchoring secure",
        "Required labels & placards installed",
        "Working clearances per NEC 110.26",
    ],
    "switchboard": [
        "Enclosure condition / no physical damage",
        "Nameplate present & legible",
        "Bus connections visually intact, no discoloration",
        "Mounting/anchoring secure",
        "Required labels & placards installed",
        "Working clearances per NEC 110.26",
    ],
    "load_center": [
        "Enclosure condition / no physical damage",
        "Nameplate present & legible",
        "Conduit entries sealed",
        "Mounting/anchoring secure",
        "Required labels & placards installed",
    ],
}

# Common low-voltage insulation-resistance acceptance floor (rule-of-thumb,
# not a code-mandated figure) — exposed as an overridable default rather than
# a hardcoded gate, since the real minimum depends on conductor voltage
# rating and site standard.
DEFAULT_MIN_INSULATION_RESISTANCE_MEGOHM = 1.0

# ANSI C84.1 Range A-style service-voltage tolerance, used as the default
# design band width when auto-populating AC voltage readings from
# ProjectInput.inverter.nominal_ac_voltage_v — overridable per call, not a
# hardcoded gate any more than the IR floor above is.
DEFAULT_AC_VOLTAGE_TOLERANCE_PCT = 5.0


def score_measurement_band(design_min: float | None, design_max: float | None, measured: float | None) -> Result:
    """A measurement (torque or a voltage reading) can only be graded once
    both its design band and a measured reading exist — a missing design
    spec isn't a failure, it's an open item."""
    if design_min is None or design_max is None or measured is None:
        return "pending"
    return "pass" if design_min <= measured <= design_max else "fail"


# TorquePoint's own docstring and commissioning_routes reference this name
# specifically for torque grading; kept as an alias of the shared band-check
# rather than a second implementation.
score_torque_point = score_measurement_band


def score_wire_item(
    *,
    design_conductor: str,
    as_built_conductor: str | None,
    termination_ok: bool | None,
    labeling_ok: bool | None,
    continuity_ok: bool | None,
    insulation_resistance_megohm: float | None,
    min_insulation_resistance_megohm: float = DEFAULT_MIN_INSULATION_RESISTANCE_MEGOHM,
) -> Result:
    """Fails on the first explicit failure found among the boolean checks, a
    conductor mismatch against design intent, or an insulation-resistance
    reading below the floor. Only reaches "pass" once every field that was
    actually supplied has cleared *and* every check has been recorded —
    a technician who only fills in half the checklist gets "pending", not a
    false pass."""
    bool_checks = (termination_ok, labeling_ok, continuity_ok)

    if all(c is None for c in bool_checks) and as_built_conductor is None and insulation_resistance_megohm is None:
        return "pending"

    if any(c is False for c in bool_checks):
        return "fail"
    if as_built_conductor is not None and as_built_conductor.strip().lower() != design_conductor.strip().lower():
        return "fail"
    if insulation_resistance_megohm is not None and insulation_resistance_megohm < min_insulation_resistance_megohm:
        return "fail"

    if any(c is None for c in bool_checks) or as_built_conductor is None:
        return "pending"
    return "pass"


def match_raceway_runs_for_tag(raceway_runs: list[RacewayRun], unit_tag: str) -> list[RacewayRun]:
    """Raceway runs whose own tag contains the unit's equipment tag as a
    substring (case-insensitive) — e.g. unit tag "INV-04" matches raceway
    run tags "INV-04", "INV-04-AC", "INV-04 DC Home Run". Nothing stronger
    links a RacewayRun to a piece of equipment in this app's data model
    today (RacewayRun.tag is free text the engineer sets on the Raceway (24)
    panel), so this is the only honest match rule available — runs that
    don't follow this naming convention need a manually-added wire item
    instead (still available in the same panel, right next to the sync
    button this feeds)."""
    tag_upper = unit_tag.upper()
    return [run for run in raceway_runs if tag_upper in run.tag.upper()]


def derive_ac_voltage_readings(
    nominal_ac_voltage_v: float,
    phases: int,
    tolerance_pct: float = DEFAULT_AC_VOLTAGE_TOLERANCE_PCT,
) -> list[dict]:
    """AC line-to-line voltage checkpoints derived from the project's own
    nominal_ac_voltage_v/phases (ProjectInput.inverter) — design_min/max is
    the nominal voltage +/- tolerance_pct, an ANSI C84.1 Range A-style band,
    not a fabricated equipment-specific spec (contrast a torque design band,
    which has no such project-level source and must always be entered by
    hand). Three line-line pairs for a 3-phase system, one reading for
    anything else — this app doesn't model split-phase L-N pairs
    separately yet."""
    half_band = nominal_ac_voltage_v * tolerance_pct / 100.0
    design_min = round(nominal_ac_voltage_v - half_band, 1)
    design_max = round(nominal_ac_voltage_v + half_band, 1)
    labels = ["AC output L1-L2", "AC output L2-L3", "AC output L1-L3"] if phases == 3 else ["AC output L1-L2"]
    return [
        {"label": label, "reading_type": "ac_voltage", "design_min": design_min, "design_max": design_max, "unit": "VAC"}
        for label in labels
    ]


def _tally(results: list[str]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r == "pass")
    failed = sum(1 for r in results if r == "fail")
    return {"total": total, "pass": passed, "fail": failed, "pending": total - passed - failed}


def summarize_unit(
    torque_results: list[str],
    inspection_results: list[str],
    wire_results: list[str],
    electrical_results: list[str],
) -> dict:
    """Rolls a unit's four child-item result lists up into the two groups
    the HMI panel shows (Visual & Mechanical = torque + InspectionItem,
    Electrical = wire + ElectricalReading) plus the overall
    CommissioningUnit.status this app persists: "needs_attention" beats
    everything else (a single failed item means the unit isn't airworthy
    regardless of what else passed), then "complete" only once every logged
    item across both groups has a pass/fail result and at least one item
    exists, otherwise "in_progress"/"not_started"."""
    visual_mechanical = _tally(torque_results + inspection_results)
    electrical = _tally(wire_results + electrical_results)
    total_items = visual_mechanical["total"] + electrical["total"]
    any_fail = visual_mechanical["fail"] > 0 or electrical["fail"] > 0
    all_graded = total_items > 0 and visual_mechanical["pending"] == 0 and electrical["pending"] == 0

    if any_fail:
        overall = "needs_attention"
    elif all_graded:
        overall = "complete"
    elif total_items > 0:
        overall = "in_progress"
    else:
        overall = "not_started"

    return {"visual_mechanical": visual_mechanical, "electrical": electrical, "overall": overall}
