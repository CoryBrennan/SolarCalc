"""Regression baseline: $396.95 at 15 inverters, verified live in the HMI
draft's browser console this session. The JS version hardcoded quantities
for that one sample project; this generalizes by num_inverters and is
verified to reproduce the exact same total.
"""

from app.placarding_calc import determine_placard_requirements


def test_fifteen_inverters_matches_browser_verified_total():
    result = determine_placard_requirements(num_inverters=15)
    assert result["estimated_total"] == 396.95
    assert len(result["line_items"]) == 7


def test_quantities_scale_correctly():
    result = determine_placard_requirements(num_inverters=15)
    poi_item, *per_inverter_items, arc_flash_item = result["line_items"]
    assert poi_item["qty"] == 1
    assert all(item["qty"] == 15 for item in per_inverter_items)
    assert arc_flash_item["qty"] == 16  # POI switchgear + 15 inverter compartments


def test_scales_with_different_inverter_count():
    result = determine_placard_requirements(num_inverters=3)
    poi_item, *per_inverter_items, arc_flash_item = result["line_items"]
    assert poi_item["qty"] == 1
    assert all(item["qty"] == 3 for item in per_inverter_items)
    assert arc_flash_item["qty"] == 4
