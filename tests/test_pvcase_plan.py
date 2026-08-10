from app.models import InverterSpec, ModuleSpec, ProjectInput
from app.pvcase_plan import PvcaseNamingConvention, PvcasePlanInput, SwitchboardGroup, build_pvcase_plan, generate_tags


def test_generate_tags_per_switchboard_restarts_counter_each_board():
    boards = [SwitchboardGroup(tag="SWBD-1", inverter_count=3), SwitchboardGroup(tag="SWBD-2", inverter_count=2)]
    naming = PvcaseNamingConvention(per_switchboard=True, start=1, zero_pad=0)
    tags = generate_tags(boards, naming, "INV")
    assert tags == ["INV-1-1", "INV-1-2", "INV-1-3", "INV-2-1", "INV-2-2"]


def test_generate_tags_sequential_counts_once_across_boards():
    boards = [SwitchboardGroup(tag="SWBD-1", inverter_count=2), SwitchboardGroup(tag="SWBD-2", inverter_count=2)]
    naming = PvcaseNamingConvention(per_switchboard=False, start=1, zero_pad=0)
    tags = generate_tags(boards, naming, "INV")
    assert tags == ["INV-1", "INV-2", "INV-3", "INV-4"]


def test_generate_tags_zero_pad():
    boards = [SwitchboardGroup(tag="SWBD-1", inverter_count=11)]
    naming = PvcaseNamingConvention(per_switchboard=True, start=1, zero_pad=2)
    tags = generate_tags(boards, naming, "DCC")
    assert tags[8] == "DCC-1-09"
    assert tags[9] == "DCC-1-10"


def test_plan_flags_inverter_quantity_mismatch():
    project = ProjectInput(module=ModuleSpec(sku="720", quantity=100), inverter=InverterSpec(quantity=5))
    plan_input = PvcasePlanInput(switchboards=[SwitchboardGroup(tag="SWBD-1", inverter_count=3)])
    plan = build_pvcase_plan(project, plan_input)
    assert plan["inverter"]["planned_quantity"] == 3
    assert plan["inverter"]["target_quantity"] == 5
    assert plan["inverter"]["quantity_mismatch"] is True
    assert any("reconcile" in note for note in plan["pvcase_setup_notes"])


def test_plan_expected_tags_match_dc_topology():
    project = ProjectInput(inverter=InverterSpec(quantity=2, dc_topology="direct"))
    plan_input = PvcasePlanInput(switchboards=[SwitchboardGroup(tag="SWBD-1", inverter_count=2)])
    plan = build_pvcase_plan(project, plan_input)
    assert plan["expected_tags"]["inverters"] == ["INV-1-1", "INV-1-2"]
    assert plan["expected_tags"]["dc_combiners"] == []  # "direct" topology has no DC combiners
    assert plan["expected_tags"]["transformers"] == ["XFMR-1"]


def test_plan_modules_per_string_uses_recommended_length():
    project = ProjectInput(module=ModuleSpec(sku="720", quantity=1000))
    plan_input = PvcasePlanInput(switchboards=[SwitchboardGroup(tag="SWBD-1", inverter_count=1)])
    plan = build_pvcase_plan(project, plan_input)
    assert plan["modules_per_string_to_use_in_pvcase"] == plan["string_design"]["recommended_string_length"]
    assert plan["estimated_string_count"] == round(1000 / plan["modules_per_string_to_use_in_pvcase"])
