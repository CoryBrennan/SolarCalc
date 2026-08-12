"""Pass/fail + rollup logic for field commissioning QC (torque checks, wire
inspections) — app/commissioning_routes.py's calculation layer, kept separate
from the route handlers the same way wire_cost_calc.py sits under its own
routes.

DEFAULT_TORQUE_CHECKLIST is connection *labels* only, not torque values —
see db_models.TorquePoint's docstring for why this app doesn't ship invented
manufacturer torque numbers. It just saves a field engineer from typing
"AC output lugs" from scratch on every unit of a given equipment type.
"""

from __future__ import annotations

from typing import Literal

Result = Literal["pending", "pass", "fail"]

DEFAULT_TORQUE_CHECKLIST: dict[str, list[str]] = {
    "inverter": ["DC input terminals", "AC output lugs", "Ground lug", "Disconnect switch lugs"],
    "switchboard": ["Main breaker lugs", "Branch breaker lugs", "Bus splice bolts", "Neutral bar", "Ground bar"],
    "load_center": ["Main breaker lugs", "Branch breaker lugs", "Neutral bar", "Ground bar"],
}

# Common low-voltage insulation-resistance acceptance floor (rule-of-thumb,
# not a code-mandated figure) — exposed as an overridable default rather than
# a hardcoded gate, since the real minimum depends on conductor voltage
# rating and site standard.
DEFAULT_MIN_INSULATION_RESISTANCE_MEGOHM = 1.0


def score_torque_point(design_min: float | None, design_max: float | None, measured: float | None) -> Result:
    """A connection can only be graded once both its design band and a
    measured reading exist — a missing design spec isn't a failure, it's an
    open item (see TorquePoint's docstring on why min/max start out None)."""
    if design_min is None or design_max is None or measured is None:
        return "pending"
    return "pass" if design_min <= measured <= design_max else "fail"


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


def _tally(results: list[str]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r == "pass")
    failed = sum(1 for r in results if r == "fail")
    return {"total": total, "pass": passed, "fail": failed, "pending": total - passed - failed}


def summarize_unit(torque_results: list[str], wire_results: list[str]) -> dict:
    """Rolls a unit's torque-point and wire-item results up into the
    CommissioningUnit.status this app persists: "needs_attention" beats
    everything else (a single failed connection means the unit isn't
    airworthy regardless of what else passed), then "complete" only once
    every logged item — torque and wire alike — has a pass/fail result and
    at least one item exists, otherwise "in_progress"/"not_started"."""
    torque = _tally(torque_results)
    wire = _tally(wire_results)
    total_items = torque["total"] + wire["total"]
    any_fail = torque["fail"] > 0 or wire["fail"] > 0
    all_graded = total_items > 0 and torque["pending"] == 0 and wire["pending"] == 0

    if any_fail:
        overall = "needs_attention"
    elif all_graded:
        overall = "complete"
    elif total_items > 0:
        overall = "in_progress"
    else:
        overall = "not_started"

    return {"torque": torque, "wire": wire, "overall": overall}
