"""Regression baseline: inverter output circuit at 253A verified live in the
HMI draft's browser console this session (350A standard size, within the
400A manufacturer max).
"""

from app.ocpd_calc import size_ocpd


def test_inverter_output_matches_browser_verified_default_project():
    result = size_ocpd(continuous_current_a=253, circuit="inverter_output", manufacturer_max_ocpd_a=400)
    assert result["min_rating_a"] == 316.25
    assert result["standard_size_a"] == 350
    assert result["manufacturer_check_ok"] is True
    assert "OK" in result["manufacturer_check"]


def test_exceeds_manufacturer_max():
    result = size_ocpd(continuous_current_a=253, circuit="inverter_output", manufacturer_max_ocpd_a=300)
    assert result["standard_size_a"] == 350
    assert result["manufacturer_check_ok"] is False
    assert "Exceeds" in result["manufacturer_check"]


def test_pv_source_circuit_has_no_manufacturer_check():
    result = size_ocpd(continuous_current_a=18.49, circuit="pv_source", manufacturer_max_ocpd_a=400)
    assert result["manufacturer_check_ok"] is True
    assert "n/a" in result["manufacturer_check"]


def test_combiner_output_matches_verified_combiner_row():
    # Row 2 of the default combiner schedule: 3 x 25A fused inputs = 75A.
    result = size_ocpd(continuous_current_a=75.0, circuit="dc_combiner_output")
    assert result["min_rating_a"] == 93.75
    assert result["standard_size_a"] == 100
