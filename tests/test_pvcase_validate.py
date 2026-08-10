from app.models import InverterSpec, ModuleSpec, ProjectInput
from app.pvcase_bom_import import CableSegment, PvcaseBomData
from app.pvcase_dwg_scan import DwgDeviceTag
from app.pvcase_plan import PvcasePlanInput, SwitchboardGroup
from app.pvcase_validate import validate_pvcase_design


def _project(inverter_count=2):
    return ProjectInput(
        module=ModuleSpec(sku="720", quantity=100),
        inverter=InverterSpec(quantity=inverter_count, dc_topology="combiner"),
    )


def _plan_input(inverter_count=2):
    return PvcasePlanInput(switchboards=[SwitchboardGroup(tag="SWBD-1", inverter_count=inverter_count, transformer_tag="XFMR-1")])


def _matching_bom():
    return PvcaseBomData(
        project_name="Test",
        overview={},
        transformer_to_inverter=[
            CableSegment(from_tag="XFMR-1", to_tag="INV-1-1", length_ft=10.0),
            CableSegment(from_tag="XFMR-1", to_tag="INV-1-2", length_ft=12.0),
        ],
        inverter_to_combiner=[
            CableSegment(from_tag="INV-1-1", to_tag="DCC-1-1", length_ft=5.0),
            CableSegment(from_tag="INV-1-2", to_tag="DCC-1-2", length_ft=6.0),
        ],
        combiner_to_string=[
            CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR1", length_ft=20.0),
        ],
    )


def _matching_dwg_tags():
    return [
        DwgDeviceTag(tag="INV-1-1", x=0, y=0),
        DwgDeviceTag(tag="INV-1-2", x=1, y=0),
        DwgDeviceTag(tag="DCC-1-1", x=0, y=1),
        DwgDeviceTag(tag="DCC-1-2", x=1, y=1),
        DwgDeviceTag(tag="XFMR-1", x=5, y=5),
    ]


def test_matching_bom_and_dwg_report_ok():
    report = validate_pvcase_design(_project(), _plan_input(), bom=_matching_bom(), dwg_tags=_matching_dwg_tags())
    assert report.ok()
    assert report.bom_present and report.dwg_present


def test_missing_inverter_in_bom_is_flagged():
    bom = _matching_bom()
    # Remove INV-1-2 everywhere it appears -- all_tags() scans every sheet,
    # so a real "missing from PVCase" case has to be missing from all of them.
    bom.transformer_to_inverter = [s for s in bom.transformer_to_inverter if s.to_tag != "INV-1-2"]
    bom.inverter_to_combiner = [s for s in bom.inverter_to_combiner if s.from_tag != "INV-1-2"]
    report = validate_pvcase_design(_project(), _plan_input(), bom=bom)
    inv_comp = next(c for c in report.comparisons if c.equipment == "inverters")
    assert "INV-1-2" in inv_comp.plan_vs_bom.expected_only
    assert not report.ok()


def test_unexpected_extra_tag_in_dwg_is_flagged():
    tags = _matching_dwg_tags() + [DwgDeviceTag(tag="INV-1-3", x=9, y=9)]
    report = validate_pvcase_design(_project(), _plan_input(), dwg_tags=tags)
    inv_comp = next(c for c in report.comparisons if c.equipment == "inverters")
    assert "INV-1-3" in inv_comp.plan_vs_dwg.actual_only
    assert not report.ok()


def test_bom_and_dwg_disagree_even_if_neither_matches_plan_exactly():
    """A stale DWG (e.g. Dropbox conflicted copy) can still agree with the
    plan count-wise while genuinely disagreeing with the BOM -- that
    cross-check is independent of the plan comparison."""
    bom = _matching_bom()
    dwg_tags = _matching_dwg_tags()
    dwg_tags[0] = DwgDeviceTag(tag="INV-1-9", x=0, y=0)  # swap INV-1-1 for a bogus tag
    report = validate_pvcase_design(_project(), _plan_input(), bom=bom, dwg_tags=dwg_tags)
    inv_comp = next(c for c in report.comparisons if c.equipment == "inverters")
    assert "INV-1-1" in inv_comp.bom_vs_dwg.expected_only
    assert "INV-1-9" in inv_comp.bom_vs_dwg.actual_only


def test_direct_topology_skips_combiner_comparison_cleanly():
    project = _project()
    project.inverter.dc_topology = "direct"
    bom = _matching_bom()
    bom.inverter_to_combiner = []
    bom.combiner_to_string = []
    report = validate_pvcase_design(project, _plan_input(), bom=bom)
    combiner_comp = next(c for c in report.comparisons if c.equipment == "dc_combiners")
    assert combiner_comp.plan_vs_bom.expected_only == []
    assert combiner_comp.plan_vs_bom.actual_only == []


def test_no_sources_supplied_warns_and_is_ok_by_default():
    report = validate_pvcase_design(_project(), _plan_input())
    assert not report.bom_present and not report.dwg_present
    assert any("Neither a BOM export nor a DWG scan" in w for w in report.warnings)
    assert report.ok()  # no data to disagree with the plan


def test_length_stats_summarize_each_circuit_sheet():
    report = validate_pvcase_design(_project(), _plan_input(), bom=_matching_bom())
    segments = {s.segment: s for s in report.length_stats}
    assert segments["Transformer to Inverter (AC)"].count == 2
    assert segments["Transformer to Inverter (AC)"].min_ft == 10.0
    assert segments["Transformer to Inverter (AC)"].max_ft == 12.0
    assert "DC Combiner to String (DC)" in segments
