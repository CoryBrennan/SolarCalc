"""Regression baseline: values verified live in the HMI draft's browser
console this session (720W module, 90C insulation, 6 conductors, 35.0C
ambient — the default project's DC source circuit case).
"""

from app.ampacity_calc import fill_adjustment_factor, size_conductor, temp_correction_factor
from app.models import AmpacityInput, ASHRAESiteData, InverterSpec


def test_dc_source_matches_browser_verified_default_project():
    result = size_conductor(
        module_sku="720",
        inverter=InverterSpec(),
        ashrae=ASHRAESiteData(max_design_temp_c=35.0),
        ampacity_input=AmpacityInput(circuit_type="dc_source", insulation_rating=90, conductor_count=6),
    )
    assert result["base_continuous_current_a"] == 23.11
    assert result["temp_correction_factor"] == 0.96
    assert result["fill_adjustment_factor"] == 0.80
    assert result["required_ampacity_a"] == 30.09
    assert result["selected_conductor"] == "10 AWG"


def test_ac_output_uses_inverter_current():
    result = size_conductor(
        module_sku="720",
        inverter=InverterSpec(max_output_current_a=253),
        ashrae=ASHRAESiteData(max_design_temp_c=30.0),
        ampacity_input=AmpacityInput(circuit_type="ac_output", insulation_rating=90, conductor_count=3),
    )
    assert result["base_continuous_current_a"] == 316.25
    assert result["temp_correction_factor"] == 1.00
    assert result["fill_adjustment_factor"] == 1.00


def test_dc_output_multiplies_by_parallel_strings():
    result = size_conductor(
        module_sku="720",
        inverter=InverterSpec(),
        ashrae=ASHRAESiteData(max_design_temp_c=30.0),
        ampacity_input=AmpacityInput(circuit_type="dc_output", parallel_strings=4, insulation_rating=90, conductor_count=3),
    )
    assert result["base_continuous_current_a"] == round(18.49 * 1.25 * 4, 2)


def test_temp_correction_factor_bands():
    assert temp_correction_factor(20, 90) == 1.04
    assert temp_correction_factor(30, 90) == 1.00
    assert temp_correction_factor(76, 90) == 0.50
    assert temp_correction_factor(100, 90) == 0.50  # clamps to last band


def test_fill_adjustment_factor_bands():
    assert fill_adjustment_factor(1) == 1.00
    assert fill_adjustment_factor(6) == 0.80
    assert fill_adjustment_factor(9) == 0.70
    assert fill_adjustment_factor(20) == 0.50
    assert fill_adjustment_factor(41) == 0.35


def test_conductor_none_when_ampacity_exceeds_table():
    result = size_conductor(
        module_sku="720",
        inverter=InverterSpec(max_output_current_a=100000),
        ashrae=ASHRAESiteData(max_design_temp_c=76),
        ampacity_input=AmpacityInput(circuit_type="ac_output", insulation_rating=90, conductor_count=41),
    )
    assert result["selected_conductor"] is None
