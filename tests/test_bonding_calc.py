"""Regression baseline: 500 kVA delta/grounded-wye transformer verified live
in the HMI draft's browser console this session (SDS=Yes, secondary
conductor sized to 10 AWG).
"""

from app.bonding_calc import is_separately_derived_system, size_bonding_and_grounding
from app.models import TransformerConfig


def test_delta_primary_grounded_wye_secondary_is_sds():
    assert is_separately_derived_system("delta", "grounded_wye") is True


def test_grounded_wye_primary_is_not_sds():
    assert is_separately_derived_system("grounded_wye", "grounded_wye") is False


def test_default_project_matches_browser_verified():
    result = size_bonding_and_grounding(TransformerConfig(kva=500, secondary_v=34500, primary_winding="delta", secondary_winding="grounded_wye"))
    assert result["separately_derived_system"] is True
    assert result["secondary_flc_a"] == 8.37
    assert result["secondary_conductor"] == "10 AWG"
    assert result["system_bonding_jumper"] == "8 AWG Cu"
    assert result["grounding_electrode_conductor"] == "8 AWG Cu"


def test_non_sds_skips_sizing():
    result = size_bonding_and_grounding(TransformerConfig(primary_winding="grounded_wye", secondary_winding="grounded_wye"))
    assert result["separately_derived_system"] is False
    assert result["secondary_conductor"] is None
