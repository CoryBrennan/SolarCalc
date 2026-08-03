from app.models import ASHRAESiteData, InverterSpec, ModuleSpec
from app.string_design_calc import compute_string_length_range


def test_default_project_matches_browser_verified_range():
    """Browser-verified against the HMI's recomputeStringLength(): module SKU
    720 (Voc 49.40 V, Vmp 41.30 V), ASHRAE -10.0/28.0 C, MPPT 500-1500 V,
    max system voltage 1500 V -> min 13, max 28 modules/string."""
    result = compute_string_length_range(
        module=ModuleSpec(sku="720"),
        inverter=InverterSpec(),
        ashrae=ASHRAESiteData(),
    )
    assert result["min_string_length"] == 13
    assert result["max_string_length"] == 28
    assert result["recommended_string_length"] == 28
    assert result["voc_per_module_cold_v"] == 53.5496
    assert result["vmp_per_module_hot_v"] == 41.0026


def test_no_valid_range_when_mppt_min_too_high():
    result = compute_string_length_range(
        module=ModuleSpec(sku="720"),
        inverter=InverterSpec(mppt_v_min=1490),
        ashrae=ASHRAESiteData(),
    )
    assert result["max_string_length"] < result["min_string_length"]
    assert result["recommended_string_length"] is None
