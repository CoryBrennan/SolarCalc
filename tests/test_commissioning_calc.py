from __future__ import annotations

from app import commissioning_calc
from app.models import RacewayRun


def test_measurement_band_pending_without_design_or_measured():
    assert commissioning_calc.score_measurement_band(None, None, None) == "pending"
    assert commissioning_calc.score_measurement_band(20.0, 25.0, None) == "pending"
    assert commissioning_calc.score_measurement_band(None, 25.0, 22.0) == "pending"


def test_measurement_band_pass_within_band():
    assert commissioning_calc.score_measurement_band(20.0, 25.0, 22.5) == "pass"
    assert commissioning_calc.score_measurement_band(20.0, 25.0, 20.0) == "pass"
    assert commissioning_calc.score_measurement_band(20.0, 25.0, 25.0) == "pass"


def test_measurement_band_fail_outside_band():
    assert commissioning_calc.score_measurement_band(20.0, 25.0, 19.9) == "fail"
    assert commissioning_calc.score_measurement_band(20.0, 25.0, 25.1) == "fail"


def test_score_torque_point_is_the_same_function():
    assert commissioning_calc.score_torque_point is commissioning_calc.score_measurement_band


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


def test_match_raceway_runs_for_tag_substring_case_insensitive():
    runs = [
        RacewayRun(tag="inv-04-ac"),
        RacewayRun(tag="INV-04 DC Home Run"),
        RacewayRun(tag="SWBD-1"),
    ]
    matched = commissioning_calc.match_raceway_runs_for_tag(runs, "INV-04")
    assert [r.tag for r in matched] == ["inv-04-ac", "INV-04 DC Home Run"]


def test_match_raceway_runs_for_tag_no_match():
    runs = [RacewayRun(tag="SWBD-1")]
    assert commissioning_calc.match_raceway_runs_for_tag(runs, "INV-04") == []


def test_derive_ac_voltage_readings_three_phase():
    readings = commissioning_calc.derive_ac_voltage_readings(800.0, 3, tolerance_pct=5.0)
    assert [r["label"] for r in readings] == ["AC output L1-L2", "AC output L2-L3", "AC output L1-L3"]
    for r in readings:
        assert r["design_min"] == 760.0
        assert r["design_max"] == 840.0
        assert r["unit"] == "VAC"


def test_derive_ac_voltage_readings_single_phase():
    readings = commissioning_calc.derive_ac_voltage_readings(240.0, 1, tolerance_pct=5.0)
    assert [r["label"] for r in readings] == ["AC output L1-L2"]


def test_summarize_unit_not_started_with_no_items():
    summary = commissioning_calc.summarize_unit([], [], [], [])
    assert summary["overall"] == "not_started"


def test_summarize_unit_in_progress_with_open_items():
    summary = commissioning_calc.summarize_unit(["pending", "pass"], [], ["pending"], [])
    assert summary["overall"] == "in_progress"


def test_summarize_unit_complete_when_all_graded_pass():
    summary = commissioning_calc.summarize_unit(["pass", "pass"], ["pass"], ["pass"], ["pass"])
    assert summary["overall"] == "complete"
    assert summary["visual_mechanical"] == {"total": 3, "pass": 3, "fail": 0, "pending": 0}
    assert summary["electrical"] == {"total": 2, "pass": 2, "fail": 0, "pending": 0}


def test_summarize_unit_needs_attention_on_any_fail_even_if_others_pending():
    summary = commissioning_calc.summarize_unit(["pass", "fail"], [], ["pending"], [])
    assert summary["overall"] == "needs_attention"


def test_summarize_unit_inspection_item_fail_also_triggers_needs_attention():
    summary = commissioning_calc.summarize_unit([], ["fail"], [], [])
    assert summary["overall"] == "needs_attention"


def test_summarize_unit_electrical_reading_fail_counts_toward_electrical_group():
    summary = commissioning_calc.summarize_unit([], [], [], ["fail"])
    assert summary["electrical"] == {"total": 1, "pass": 0, "fail": 1, "pending": 0}
    assert summary["overall"] == "needs_attention"
