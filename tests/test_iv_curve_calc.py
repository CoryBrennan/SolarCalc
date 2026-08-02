"""Regression baseline: expected Voc verified live in the HMI draft's browser
console this session (720W module, 850 W/m2, 42C cell temp, 28 modules/string
-> 1326.77 V). Isc/Vmp/Imp derived from the same formula, hand-checked.

Note: Vmp uses the same temp-coefficient constant as Voc (TEMP_COEFF_VOC),
not a separate Vmp coefficient — that's a match to the original JS, not an
oversight here.
"""

from app.iv_curve_calc import expected_iv_point, validate_reading


def test_expected_point_matches_browser_verified():
    result = expected_iv_point(module_sku="720", irradiance_w_m2=850, cell_temp_c=42, modules_per_string=28)
    assert result["voc"] == 1326.77
    assert result["isc"] == 15.82
    assert result["vmp"] == 1109.22
    assert result["imp"] == 14.92


def test_validate_reading_within_tolerance_passes():
    expected = expected_iv_point(module_sku="720", irradiance_w_m2=850, cell_temp_c=42, modules_per_string=28)
    measured = {"voc": 1330.0, "isc": 15.90, "vmp": 1104.6, "imp": 14.85}
    result = validate_reading(expected, measured, tolerance_pct=5.0)
    assert result["all_pass"] is True


def test_validate_reading_outside_tolerance_fails():
    expected = expected_iv_point(module_sku="720", irradiance_w_m2=850, cell_temp_c=42, modules_per_string=28)
    measured = {"voc": 1000.0, "isc": 15.90, "vmp": 1104.6, "imp": 14.85}
    result = validate_reading(expected, measured, tolerance_pct=5.0)
    assert result["all_pass"] is False
    voc_check = next(c for c in result["checks"] if c["parameter"] == "voc")
    assert voc_check["passes"] is False
