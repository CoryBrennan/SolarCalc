"""Regression baseline: the default two-combiner schedule (2 and 3 inputs,
both 720W modules, 35A max series fuse) verified live in the HMI draft's
browser console this session.
"""

from app.combiner_calc import compute_combiner_row, size_combiners
from app.models import CombinerRow


def test_default_schedule_matches_browser_verified():
    rows = [
        CombinerRow(inputs=2, bus_rating_a=200, module_sku="720"),
        CombinerRow(inputs=3, bus_rating_a=200, module_sku="720"),
    ]
    result = size_combiners(rows, max_series_fuse_rating_a=35)

    assert result["combiner_count"] == 2
    assert result["total_strings"] == 5
    assert result["max_output_ampacity_a"] == 75.00

    row1, row2 = result["rows"]
    assert row1["input_fuse_a"] == 25
    assert row1["output_ampacity_a"] == 50.00
    assert row1["output_conductor"] == "8 AWG"
    assert row1["bus_passes"] is True

    assert row2["input_fuse_a"] == 25
    assert row2["output_ampacity_a"] == 75.00
    assert row2["output_conductor"] == "6 AWG"
    assert row2["bus_passes"] is True


def test_undersized_busbar_fails():
    row = CombinerRow(inputs=12, bus_rating_a=200, module_sku="720")
    result = compute_combiner_row(row, max_series_fuse_rating_a=35)
    assert result["output_ampacity_a"] == 300.00
    assert result["output_conductor"] == "300 kcmil"
    assert result["bus_passes"] is False


def test_different_modules_per_row_size_independently():
    row_700 = CombinerRow(inputs=2, bus_rating_a=200, module_sku="700")
    row_720 = CombinerRow(inputs=2, bus_rating_a=200, module_sku="720")
    result_700 = compute_combiner_row(row_700, max_series_fuse_rating_a=35)
    result_720 = compute_combiner_row(row_720, max_series_fuse_rating_a=35)
    # Both round to the same standard fuse size here (close Isc values) but are
    # computed independently off each row's own module.
    assert result_700["input_min_fuse_a"] != result_720["input_min_fuse_a"]


def test_fuse_cap_flags_when_below_125_percent_minimum():
    row = CombinerRow(inputs=1, bus_rating_a=200, module_sku="720")
    result = compute_combiner_row(row, max_series_fuse_rating_a=15)
    assert result["input_fuse_a"] == 15
    assert result["input_fuse_under_minimum"] is True
