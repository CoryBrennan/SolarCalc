"""Regression baseline: inverter->switchboard segment (253A, 200ft, 800V,
4/0 AWG, 1.0% limit) verified live in the HMI draft's browser console this
session — 0.67%, passes without upsizing.
"""

import pytest

from app.voltage_drop_calc import check_segment


def test_aluminum_has_more_voltage_drop_than_copper_same_size():
    cu = check_segment(current_a=253, length_ft=200, voltage_v=800, limit_pct=100, conductor="4/0 AWG", material="CU")
    al = check_segment(current_a=253, length_ft=200, voltage_v=800, limit_pct=100, conductor="4/0 AWG", material="AL")
    assert al["voltage_drop_pct"] > cu["voltage_drop_pct"]


def test_default_project_matches_browser_verified():
    result = check_segment(current_a=253, length_ft=200, voltage_v=800, limit_pct=1.0, conductor="4/0 AWG")
    assert result["voltage_drop_pct"] == 0.67
    assert result["passes"] is True
    assert result["upsized"] is False
    assert result["final_conductor"] == "4/0 AWG"


def test_upsizes_when_over_limit():
    result = check_segment(current_a=253, length_ft=800, voltage_v=800, limit_pct=1.0, conductor="4/0 AWG")
    assert result["passes"] is True
    assert result["upsized"] is True
    assert result["final_conductor"] != "4/0 AWG"


def test_fails_when_no_size_clears_limit():
    result = check_segment(current_a=253, length_ft=100000, voltage_v=800, limit_pct=0.1, conductor="10 AWG")
    assert result["passes"] is False
    # Matches the original JS: when nothing clears the limit, the reported
    # conductor/numbers stay at the *starting* size, not the largest tried.
    assert result["final_conductor"] == "10 AWG"


def test_unknown_conductor_falls_back_to_4_0():
    result = check_segment(current_a=50, length_ft=100, voltage_v=480, limit_pct=2.0, conductor="not a real size")
    assert result["starting_conductor"] == "4/0 AWG"


def test_zero_voltage_raises_value_error_instead_of_zero_division():
    with pytest.raises(ValueError, match="voltage_v"):
        check_segment(current_a=253, length_ft=200, voltage_v=0, limit_pct=1.0, conductor="4/0 AWG")
