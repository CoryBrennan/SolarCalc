"""PVCase planning brief -- what an engineer needs to key into PVCase (target
module count, modules-per-string, and the same PVCase-style equipment tags
the Inverter Design panel already generates) before building the site layout,
computed from this project's own module/inverter/string-length calcs so
PVCase and the app start from one set of numbers instead of a second manual
translation.

Tag generation assumes one DC combiner per inverter when dc_topology ==
"combiner", matching every real Azimuth PVCase export seen so far (Encore
Brighton: 36 inverters, 36 DC combiners, one XFMR per switchboard).
app/combiner_calc.py's CombinerRow models something different -- a small
catalog of combiner *types* for sizing, not a site-wide per-inverter count --
and is untouched by this module.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import ProjectInput
from app.string_design_calc import compute_string_length_range


class SwitchboardGroup(BaseModel):
    tag: str = "SWBD-1"
    inverter_count: int = 15
    transformer_tag: str = "XFMR-1"


class PvcaseNamingConvention(BaseModel):
    """Mirrors the HMI's Inverter Design naming controls (prefix / separator /
    start / zero-pad / sequential-vs-per-switchboard, see static/index.html
    namingOptions()/autoInverterTags()) so generated tags match whatever the
    engineer already set there."""

    inverter_prefix: str = "INV"
    combiner_prefix: str = "DCC"
    transformer_prefix: str = "XFMR"
    separator: str = "-"
    start: int = 1
    zero_pad: int = 0
    per_switchboard: bool = True


class PvcasePlanInput(BaseModel):
    switchboards: list[SwitchboardGroup] = Field(default_factory=lambda: [SwitchboardGroup()])
    naming: PvcaseNamingConvention = Field(default_factory=PvcaseNamingConvention)


class PvcasePlanRequest(BaseModel):
    project: ProjectInput
    plan: PvcasePlanInput = Field(default_factory=PvcasePlanInput)


def _pad(n: int, width: int) -> str:
    s = str(n)
    return s.zfill(width) if width > 0 else s


def generate_tags(switchboards: list[SwitchboardGroup], naming: PvcaseNamingConvention, prefix: str) -> list[str]:
    """Same numbering rule as the HMI's autoInverterTags(): per-switchboard
    mode restarts the counter at `start` for each board; sequential mode
    counts up once across every board in order."""
    tags: list[str] = []
    seq = naming.start
    for board_idx, board in enumerate(switchboards, start=1):
        counter = naming.start
        for _ in range(board.inverter_count):
            if naming.per_switchboard:
                tags.append(f"{prefix}{naming.separator}{board_idx}{naming.separator}{_pad(counter, naming.zero_pad)}")
                counter += 1
            else:
                tags.append(f"{prefix}{naming.separator}{_pad(seq, naming.zero_pad)}")
                seq += 1
    return tags


def build_pvcase_plan(project: ProjectInput, plan: PvcasePlanInput) -> dict:
    total_planned_inverters = sum(b.inverter_count for b in plan.switchboards)

    string_result = compute_string_length_range(
        module=project.module,
        inverter=project.inverter,
        ashrae=project.ashrae,
        voltage_drop_limits=project.voltage_drop_limits,
    )
    modules_per_string = string_result["recommended_string_length"] or string_result["max_string_length"] or None
    estimated_string_count = round(project.module.quantity / modules_per_string) if modules_per_string else None

    inverter_tags = generate_tags(plan.switchboards, plan.naming, plan.naming.inverter_prefix)
    combiner_tags = (
        generate_tags(plan.switchboards, plan.naming, plan.naming.combiner_prefix)
        if project.inverter.dc_topology == "combiner"
        else []
    )
    transformer_tags = [b.transformer_tag for b in plan.switchboards]

    setup_notes = [
        f"Set the Symmetrical Tree Builder's naming toggle to numerical, "
        f"{modules_per_string if modules_per_string else '(no valid string length)'} modules per string.",
        (
            f"Inverter tags: {plan.naming.inverter_prefix}{plan.naming.separator}"
            f"(board){plan.naming.separator}(n) per switchboard."
            if plan.naming.per_switchboard
            else f"Inverter tags: {plan.naming.inverter_prefix}{plan.naming.separator}(n) sequential across site."
        ),
        f"Total planned inverters: {total_planned_inverters} across {len(plan.switchboards)} switchboard(s)/transformer(s).",
    ]
    if total_planned_inverters != project.inverter.quantity:
        setup_notes.append(
            f"Switchboard plan totals {total_planned_inverters} inverters but "
            f"InverterSpec.quantity is {project.inverter.quantity} -- reconcile before building in PVCase."
        )

    return {
        "module": {
            "sku": project.module.sku,
            "manufacturer": project.module.manufacturer,
            "target_quantity": project.module.quantity,
        },
        "string_design": string_result,
        "modules_per_string_to_use_in_pvcase": modules_per_string,
        "estimated_string_count": estimated_string_count,
        "inverter": {
            "model": project.inverter.model,
            "ac_rating_w": project.inverter.ac_rating_w,
            "target_quantity": project.inverter.quantity,
            "planned_quantity": total_planned_inverters,
            "quantity_mismatch": total_planned_inverters != project.inverter.quantity,
        },
        "dc_topology": project.inverter.dc_topology,
        "naming_convention": plan.naming.model_dump(),
        "switchboards": [b.model_dump() for b in plan.switchboards],
        "expected_tags": {
            "inverters": inverter_tags,
            "dc_combiners": combiner_tags,
            "transformers": transformer_tags,
        },
        "pvcase_setup_notes": setup_notes,
    }
