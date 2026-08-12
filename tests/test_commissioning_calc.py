from __future__ import annotations

from app import commissioning_calc


def test_torque_point_pending_without_design_or_measured():
    assert commissioning_calc.score_torque_point(None, None, None) == "pending"
    assert commissioning_calc.score_torque_point(20.0, 25.0, None) == "pending"
    assert commissioning_calc.score_torque_point(None, 25.0, 22.0) == "pending"


def test_torque_point_pass_within_band():
    assert commissioning_calc.score_torque_point(20.0, 25.0, 22.5) == "pass"
    assert commissioning_calc.score_torque_point(20.0, 25.0, 20.0) == "pass"
    assert commissioning_calc.score_torque_point(20.0, 25.0, 25.0) == "pass"


def test_torque_point_fail_outside_band():
    assert commissioning_calc.score_torque_point(20.0, 25.0, 19.9) == "fail"
    assert commissioning_calc.score_torque_point(20.0, 25.0, 25.1) == "fail"


def test_wire_item_pending_when_nothing_recorded():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor=None,
        termination_ok=None,
        labeling_ok=None,
        continuity_ok=None,
        insulation_resistance_megohm=None,
    )
    assert result == "pending"


def test_wire_item_pending_when_partially_recorded():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor="500 kcmil CU",
        termination_ok=True,
        labeling_ok=None,
        continuity_ok=True,
        insulation_resistance_megohm=5.0,
    )
    assert result == "pending"


def test_wire_item_pass_when_everything_checks_out():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor="500 KCMIL cu",
        termination_ok=True,
        labeling_ok=True,
        continuity_ok=True,
        insulation_resistance_megohm=5.0,
    )
    assert result == "pass"


def test_wire_item_fails_on_boolean_check():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor="500 kcmil CU",
        termination_ok=False,
        labeling_ok=True,
        continuity_ok=True,
        insulation_resistance_megohm=5.0,
    )
    assert result == "fail"


def test_wire_item_fails_on_conductor_mismatch():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor="4/0 AWG CU",
        termination_ok=True,
        labeling_ok=True,
        continuity_ok=True,
        insulation_resistance_megohm=5.0,
    )
    assert result == "fail"


def test_wire_item_fails_below_insulation_resistance_floor():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor="500 kcmil CU",
        termination_ok=True,
        labeling_ok=True,
        continuity_ok=True,
        insulation_resistance_megohm=0.5,
    )
    assert result == "fail"


def test_wire_item_respects_custom_insulation_floor():
    result = commissioning_calc.score_wire_item(
        design_conductor="500 kcmil CU",
        as_built_conductor="500 kcmil CU",
        termination_ok=True,
        labeling_ok=True,
        continuity_ok=True,
        insulation_resistance_megohm=0.5,
        min_insulation_resistance_megohm=0.1,
    )
    assert result == "pass"


def test_summarize_unit_not_started_with_no_items():
    summary = commissioning_calc.summarize_unit([], [])
    assert summary["overall"] == "not_started"


def test_summarize_unit_in_progress_with_open_items():
    summary = commissioning_calc.summarize_unit(["pending", "pass"], ["pending"])
    assert summary["overall"] == "in_progress"


def test_summarize_unit_complete_when_all_graded_pass():
    summary = commissioning_calc.summarize_unit(["pass", "pass"], ["pass"])
    assert summary["overall"] == "complete"
    assert summary["torque"] == {"total": 2, "pass": 2, "fail": 0, "pending": 0}
    assert summary["wire"] == {"total": 1, "pass": 1, "fail": 0, "pending": 0}


def test_summarize_unit_needs_attention_on_any_fail_even_if_others_pending():
    summary = commissioning_calc.summarize_unit(["pass", "fail"], ["pending"])
    assert summary["overall"] == "needs_attention"
