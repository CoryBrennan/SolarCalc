"""Regression baseline: the default project (15 inverters, 350A OCPD each,
1200A busbar, 800A main, 5250A backfeed) produces the exact contract shape
ac-switchboard-addin's SwitchboardConfig class expects.
"""

from app.switchboard_block import build_switchboard_config


def test_default_project_matches_verified_calc_numbers():
    config = build_switchboard_config(
        tag="SWBD-1",
        inverter_phases=3,
        busbar_rating_a=1200,
        main_rating_a=800,
        num_inverters=15,
        inverter_ocpd_standard_size_a=350,
        backfeed_total_a=5250.0,
    )

    assert config["tag"] == "SWBD-1"
    assert config["phase_config"] == "3PH"
    assert config["main_breaker"]["rating"] == "800A"
    assert config["bus_rating_amps"] == 1200
    assert config["backfeed_total_amps"] == 5250
    assert len(config["positions"]) == 15

    first = config["positions"][0]
    assert first == {
        "position_number": 1,
        "type": "inverter",
        "tag": "INV-1",
        "breaker_rating": "350A",
        "phase": ["A", "B", "C"],
    }
    assert config["positions"][-1]["position_number"] == 15
    assert config["positions"][-1]["tag"] == "INV-15"


def test_single_phase_uses_two_pole_labels():
    config = build_switchboard_config(
        tag="SWBD-2", inverter_phases=1, busbar_rating_a=400, main_rating_a=200,
        num_inverters=2, inverter_ocpd_standard_size_a=100, backfeed_total_a=200,
    )
    assert config["phase_config"] == "1PH"
    assert config["positions"][0]["phase"] == ["A", "B"]
