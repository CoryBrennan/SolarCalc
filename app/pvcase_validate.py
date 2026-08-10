"""Closes the App Planning -> PVCase/AutoCAD implementation -> App validation
loop: compares a project's planning brief (app/pvcase_plan.py) against a
parsed PVCase BOM export (app/pvcase_bom_import.py) and/or a parsed CAD
Release DWG scan (app/pvcase_dwg_scan.py).

Either source can be supplied alone -- a BOM-only or DWG-only check just
skips comparisons that need the missing side. When both are supplied, the
BOM-vs-DWG comparison is worth as much as either against the plan: BOM and
DWG come from the same PVCase export session and should always agree with
each other, so a mismatch there usually means one of the two files is stale
(e.g. a Dropbox sync-conflict copy of the DWG, or a BOM re-exported after a
later CAD edit) rather than a real design problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field as PydanticField

from app.models import ProjectInput
from app.pvcase_bom_import import PvcaseBomData
from app.pvcase_dwg_scan import DwgDeviceTag
from app.pvcase_plan import PvcaseNamingConvention, PvcasePlanInput, build_pvcase_plan

_EQUIPMENT_TYPES = ["inverters", "dc_combiners", "transformers"]


class PvcaseValidateRequest(BaseModel):
    """Path-based rather than upload-based -- the BOM and DWG live in the
    engineer's Dropbox-synced project folder on this same machine, so
    pointing at a path avoids re-uploading a 30+ MB DWG through the browser.
    DWG scanning also needs a local AutoCAD install regardless (see
    pvcase_dwg_scan.find_accoreconsole), so it's only meaningful when this
    backend runs on the engineer's own machine, not a cloud deployment."""

    project: ProjectInput
    plan: PvcasePlanInput = PydanticField(default_factory=PvcasePlanInput)
    bom_path: str | None = None
    dwg_path: str | None = None


@dataclass
class TagSetDiff:
    expected_only: list[str]  # in the first set, not the second -- "missing"
    actual_only: list[str]  # in the second set, not the first -- "unexpected"
    matched_count: int


@dataclass
class TagComparison:
    equipment: str
    plan_vs_bom: TagSetDiff | None = None
    plan_vs_dwg: TagSetDiff | None = None
    bom_vs_dwg: TagSetDiff | None = None


@dataclass
class LengthStats:
    segment: str
    count: int
    min_ft: float
    max_ft: float
    avg_ft: float


@dataclass
class PvcaseValidationReport:
    plan: dict
    bom_present: bool
    dwg_present: bool
    comparisons: list[TagComparison] = field(default_factory=list)
    length_stats: list[LengthStats] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        for c in self.comparisons:
            for diff in (c.plan_vs_bom, c.plan_vs_dwg, c.bom_vs_dwg):
                if diff and (diff.expected_only or diff.actual_only):
                    return False
        return True


def _prefix_for(naming: PvcaseNamingConvention, equipment: str) -> str:
    return {
        "inverters": naming.inverter_prefix,
        "dc_combiners": naming.combiner_prefix,
        "transformers": naming.transformer_prefix,
    }[equipment]


def _tags_with_prefix(tags: set[str], prefix: str, separator: str) -> set[str]:
    marker = prefix + separator
    return {t for t in tags if t == prefix or t.startswith(marker)}


def _diff(expected: list[str] | set[str], actual: set[str]) -> TagSetDiff:
    expected_set = set(expected)
    return TagSetDiff(
        expected_only=sorted(expected_set - actual),
        actual_only=sorted(actual - expected_set),
        matched_count=len(expected_set & actual),
    )


def _length_stats(segment_name: str, segments: list) -> LengthStats | None:
    if not segments:
        return None
    lengths = [s.length_ft for s in segments]
    return LengthStats(
        segment=segment_name,
        count=len(lengths),
        min_ft=round(min(lengths), 1),
        max_ft=round(max(lengths), 1),
        avg_ft=round(sum(lengths) / len(lengths), 1),
    )


def validate_pvcase_design(
    project: ProjectInput,
    plan_input: PvcasePlanInput,
    bom: PvcaseBomData | None = None,
    dwg_tags: list[DwgDeviceTag] | None = None,
) -> PvcaseValidationReport:
    plan = build_pvcase_plan(project, plan_input)
    naming = plan_input.naming

    bom_all = bom.all_tags() if bom is not None else None
    dwg_all = {t.tag for t in dwg_tags} if dwg_tags is not None else None

    comparisons: list[TagComparison] = []
    for equipment in _EQUIPMENT_TYPES:
        prefix = _prefix_for(naming, equipment)
        expected_tags = plan["expected_tags"][equipment]

        bom_tags = _tags_with_prefix(bom_all, prefix, naming.separator) if bom_all is not None else None
        dwg_tags_set = _tags_with_prefix(dwg_all, prefix, naming.separator) if dwg_all is not None else None

        comparisons.append(
            TagComparison(
                equipment=equipment,
                plan_vs_bom=_diff(expected_tags, bom_tags) if bom_tags is not None else None,
                plan_vs_dwg=_diff(expected_tags, dwg_tags_set) if dwg_tags_set is not None else None,
                bom_vs_dwg=_diff(bom_tags, dwg_tags_set) if (bom_tags is not None and dwg_tags_set is not None) else None,
            )
        )

    length_stats: list[LengthStats] = []
    if bom is not None:
        for name, segs in (
            ("Transformer to Inverter (AC)", bom.transformer_to_inverter),
            ("Inverter to DC Combiner (DC)", bom.inverter_to_combiner),
            ("DC Combiner to String (DC)", bom.combiner_to_string),
        ):
            stat = _length_stats(name, segs)
            if stat:
                length_stats.append(stat)

    warnings: list[str] = []
    if bom is None and dwg_tags is None:
        warnings.append("Neither a BOM export nor a DWG scan was supplied -- nothing to validate against the plan.")
    if bom is not None and not bom.transformer_to_inverter and not bom.inverter_to_combiner:
        warnings.append("BOM parsed but contains no circuit-length rows -- check it's the right export.")

    return PvcaseValidationReport(
        plan=plan,
        bom_present=bom is not None,
        dwg_present=dwg_tags is not None,
        comparisons=comparisons,
        length_stats=length_stats,
        warnings=warnings,
    )
