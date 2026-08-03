from app.models import ASHRAESiteData, ClientVoltageDropLimits, InverterSpec, ModuleSpec
from app.string_design_calc import compute_string_length_range, mppt_life_years


def _range(**overrides):
    kwargs = {
        "module": ModuleSpec(sku="720"),
        "inverter": InverterSpec(),
        "ashrae": ASHRAESiteData(),
        "voltage_drop_limits": ClientVoltageDropLimits(),
    }
    kwargs.update(overrides)
    return compute_string_length_range(**kwargs)


def test_default_project_matches_browser_verified_range():
    """Browser-verified against the HMI's recomputeStringLength(): module SKU
    720 (Voc 49.40 V, Vmp 41.30 V), ASHRAE -10.0/28.0 C, MPPT 500-1500 V,
    max system voltage 1500 V -> min 13, max 28 modules/string."""
    result = _range()
    assert result["min_string_length"] == 13
    assert result["max_string_length"] == 28
    assert result["recommended_string_length"] == 28
    assert result["voc_per_module_cold_v"] == 53.5496
    assert result["vmp_per_module_hot_v"] == 41.0026


def test_no_valid_range_when_mppt_min_too_high():
    result = _range(inverter=InverterSpec(mppt_v_min=1490))
    assert result["max_string_length"] < result["min_string_length"]
    assert result["recommended_string_length"] is None


def test_short_string_fails_the_15_year_mppt_life_target():
    """A string barely clearing the MPPT minimum has almost no headroom, so
    degradation walks it out of the window well before 15 years."""
    life = mppt_life_years(
        modules_per_string=13,
        vmp_per_module_hot=41.0026,
        mppt_v_min=500.0,
        v_drop_frac=0.02,
        first_year_deg_frac=0.01,
        annual_deg_frac=0.004,
    )
    assert 0 < life < 15


def test_recommendation_skips_lengths_under_the_life_target():
    """With the MPPT floor raised so only the very longest strings survive 15
    years, the recommendation must drop below max_string_length rather than
    blindly returning it."""
    result = _range(inverter=InverterSpec(mppt_v_min=1050))
    rec = result["recommended_string_length"]
    assert rec is not None
    assert result["recommended_mppt_life_years"] >= 15
    life_one_shorter = mppt_life_years(
        modules_per_string=rec - 1,
        vmp_per_module_hot=result["vmp_per_module_hot_v"],
        mppt_v_min=1050,
        v_drop_frac=0.02,
        first_year_deg_frac=0.01,
        annual_deg_frac=0.004,
    )
    assert life_one_shorter < 15
