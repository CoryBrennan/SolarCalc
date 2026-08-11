from app.models import RacewayRun
from app.raceway_calc import (
    compute_raceway_run,
    conductor_area_in2,
    fill_percent_allowed,
    size_cable_tray,
    size_conduit,
    size_messenger_cable,
)


def test_fill_percent_allowed_table1():
    assert fill_percent_allowed(1, is_nipple=False) == 53.0
    assert fill_percent_allowed(2, is_nipple=False) == 31.0
    assert fill_percent_allowed(3, is_nipple=False) == 40.0
    assert fill_percent_allowed(9, is_nipple=False) == 40.0
    assert fill_percent_allowed(9, is_nipple=True) == 60.0


def test_conductor_area_use2_is_larger_than_thhn():
    thhn = conductor_area_in2("4/0 AWG", "THHN_THWN2")
    use2 = conductor_area_in2("4/0 AWG", "USE2_RHW2")
    assert thhn == 0.3237
    assert use2 > thhn
    assert use2 == round(thhn * 1.28, 4)


def test_size_conduit_three_10awg_pvc40():
    # 3 conductors -> 40% fill. 10 AWG THHN area 0.0211 in2 x 3 = 0.0633 in2.
    # 1/2" PVC Sch 40: 0.285 in2 x 0.40 = 0.114 in2 >= 0.0633 -> fits at 1/2".
    result = size_conduit("10 AWG", "THHN_THWN2", 3, "PVC_SCH40", is_nipple=False)
    assert result["allowed_fill_pct"] == 40.0
    assert result["total_conductor_area_in2"] == 0.0633
    assert result["selected_trade_size_in"] == "1/2"
    assert result["fits"] is True


def test_size_conduit_needs_larger_trade_size_for_more_conductors():
    small = size_conduit("4/0 AWG", "USE2_RHW2", 2, "PVC_SCH40", is_nipple=False)
    large = size_conduit("4/0 AWG", "USE2_RHW2", 9, "PVC_SCH40", is_nipple=False)
    small_idx = ["1/2", "3/4", "1", "1-1/4", "1-1/2", "2", "2-1/2", "3", "3-1/2", "4"].index(small["selected_trade_size_in"])
    large_idx = ["1/2", "3/4", "1", "1-1/4", "1-1/2", "2", "2-1/2", "3", "3-1/2", "4"].index(large["selected_trade_size_in"])
    assert large_idx > small_idx


def test_size_conduit_metal_flag():
    assert size_conduit("6 AWG", "THHN_THWN2", 3, "EMT", is_nipple=False)["is_metal"] is True
    assert size_conduit("6 AWG", "THHN_THWN2", 3, "PVC_SCH80", is_nipple=False)["is_metal"] is False


def test_size_cable_tray_below_1_0_awg_flags_ladder_note():
    result = size_cable_tray("6 AWG", "USE2_RHW2", 20, "ladder", tray_width_in=12)
    assert result["ladder_rung_spacing_note"] is True
    result_trough = size_cable_tray("6 AWG", "USE2_RHW2", 20, "ventilated_trough", tray_width_in=12)
    assert result_trough["ladder_rung_spacing_note"] is False


def test_size_cable_tray_fill_check():
    # 4/0 AWG USE-2 area = 0.3237*1.28 = 0.4143 in2. 6 conductors = 2.4858 in2.
    # 12" tray, sub-1/0 ratio doesn't apply (4/0 >= 1/0 but < 250kcmil so still
    # sub-250kcmil ratio 2/3): max area = 12 * 2/3 = 8.0 in2.
    result = size_cable_tray("4/0 AWG", "USE2_RHW2", 6, "ladder", tray_width_in=12)
    assert result["max_allowed_area_in2"] == 8.0
    assert result["passes"] is True


def test_size_cable_tray_overfilled_reports_required_width():
    result = size_cable_tray("4/0 AWG", "USE2_RHW2", 40, "ladder", tray_width_in=6)
    assert result["passes"] is False
    assert result["min_required_width_in"] > 6


def test_size_messenger_cable_selects_adequate_strand():
    result = size_messenger_cable(
        conductor="4/0 AWG", insulation="USE2_RHW2", conductor_count=3,
        span_ft=150, ice_thickness_in=0.0, sag_ratio=0.03, safety_factor=2.0,
    )
    assert result["fits"] is True
    assert result["selected_breaking_strength_lb"] >= result["required_breaking_strength_lb"]


def test_size_messenger_cable_ice_increases_required_strength():
    no_ice = size_messenger_cable(
        conductor="4/0 AWG", insulation="USE2_RHW2", conductor_count=3,
        span_ft=150, ice_thickness_in=0.0, sag_ratio=0.03, safety_factor=2.0,
    )
    with_ice = size_messenger_cable(
        conductor="4/0 AWG", insulation="USE2_RHW2", conductor_count=3,
        span_ft=150, ice_thickness_in=0.5, sag_ratio=0.03, safety_factor=2.0,
    )
    assert with_ice["required_breaking_strength_lb"] > no_ice["required_breaking_strength_lb"]


def test_compute_raceway_run_conduit():
    run = RacewayRun(
        tag="RW-TEST", raceway_type="conduit", circuit_type="dc", current_a=30,
        conductor_count=2, insulation_rating=90, conductor_insulation="USE2_RHW2",
        length_ft=200, voltage_v=600, vd_limit_pct=2.0, conduit_material="PVC_SCH40",
    )
    result = compute_raceway_run(run, ambient_c=35.0)
    assert result["selected_conductor"] is not None
    assert result["raceway"]["selected_trade_size_in"] is not None
    assert result["voltage_drop"] is not None


def test_compute_raceway_run_ac_in_steel_conduit_upsizes_vd_vs_pvc():
    steel_run = RacewayRun(
        raceway_type="conduit", circuit_type="ac", current_a=100, conductor_count=3,
        length_ft=300, voltage_v=480, conduit_material="RMC",
    )
    pvc_run = RacewayRun(
        raceway_type="conduit", circuit_type="ac", current_a=100, conductor_count=3,
        length_ft=300, voltage_v=480, conduit_material="PVC_SCH40",
    )
    steel_result = compute_raceway_run(steel_run, ambient_c=30.0)
    pvc_result = compute_raceway_run(pvc_run, ambient_c=30.0)
    assert steel_result["voltage_drop"]["voltage_drop_pct"] > pvc_result["voltage_drop"]["voltage_drop_pct"]


def test_compute_raceway_run_dc_ignores_conduit_material_for_vd():
    steel_run = RacewayRun(
        raceway_type="conduit", circuit_type="dc", current_a=30, conductor_count=2,
        length_ft=200, voltage_v=600, conduit_material="RMC",
    )
    pvc_run = RacewayRun(
        raceway_type="conduit", circuit_type="dc", current_a=30, conductor_count=2,
        length_ft=200, voltage_v=600, conduit_material="PVC_SCH40",
    )
    steel_result = compute_raceway_run(steel_run, ambient_c=30.0)
    pvc_result = compute_raceway_run(pvc_run, ambient_c=30.0)
    assert steel_result["voltage_drop"]["voltage_drop_pct"] == pvc_result["voltage_drop"]["voltage_drop_pct"]


def test_compute_raceway_run_messenger():
    run = RacewayRun(
        raceway_type="messenger", circuit_type="ac", current_a=50, conductor_count=3,
        length_ft=150, voltage_v=480, span_ft=150, ice_thickness_in=0.25,
    )
    result = compute_raceway_run(run, ambient_c=30.0)
    assert result["raceway"]["selected_messenger"] is not None
