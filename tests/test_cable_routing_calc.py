import pytest

from app.cable_routing_calc import (
    RoutingLeg,
    RoutingLegTemplate,
    apply_routing_template,
    leg_derate_factor,
    size_cable_with_routing,
)


def test_apply_routing_template_fixed_plus_remainder():
    templates = [
        RoutingLegTemplate(installation_method="free_air_or_tray", fixed_length_ft=15.0),
        RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None),
    ]
    legs, warnings = apply_routing_template(100.0, templates)
    assert warnings == []
    assert legs[0].installation_method == "free_air_or_tray"
    assert legs[0].length_ft == 15.0
    assert legs[1].installation_method == "conduit_below_grade"
    assert legs[1].length_ft == 85.0


def test_apply_routing_template_carries_raceway_fields_through():
    templates = [
        RoutingLegTemplate(
            installation_method="conduit_below_grade", fixed_length_ft=None,
            conductor_count=12, conduit_material="RMC", is_nipple=True,
        ),
    ]
    legs, _ = apply_routing_template(80.0, templates)
    assert legs[0].conductor_count == 12
    assert legs[0].conduit_material == "RMC"
    assert legs[0].is_nipple is True


def test_apply_routing_template_fixed_legs_exceed_short_segment():
    """Real-world case: a 15 ft fixed lead-in template applied to a segment
    shorter than 15 ft (this app's own real BOM data has DC combiner-to-
    inverter runs as short as 55.6 ft, so a template tuned for a long run
    can legitimately be too big for a short one)."""
    templates = [
        RoutingLegTemplate(installation_method="free_air_or_tray", fixed_length_ft=15.0),
        RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None),
    ]
    legs, warnings = apply_routing_template(10.0, templates)
    assert legs[1].length_ft == 0.0  # remainder clamped, not negative
    assert any("longer than this segment's actual" in w for w in warnings)


def test_apply_routing_template_no_remainder_leg_mismatch_warns():
    templates = [RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=50.0)]
    legs, warnings = apply_routing_template(80.0, templates)
    assert len(legs) == 1
    assert any("doesn't match this segment's actual" in w for w in warnings)


def test_apply_routing_template_no_remainder_leg_exact_match_no_warning():
    templates = [RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=80.0)]
    legs, warnings = apply_routing_template(80.0, templates)
    assert warnings == []


def test_apply_routing_template_multiple_remainders_warns_and_only_first_absorbs():
    templates = [
        RoutingLegTemplate(installation_method="free_air_or_tray", fixed_length_ft=None),
        RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None),
    ]
    legs, warnings = apply_routing_template(100.0, templates)
    assert any("remainder legs in template" in w for w in warnings)
    assert legs[0].length_ft == 100.0
    assert legs[1].length_ft == 0.0


def test_leg_derate_factor_free_air_skips_fill_penalty():
    leg = RoutingLeg(installation_method="free_air_or_tray", length_ft=50, conductor_count=12)
    result = leg_derate_factor(leg, default_ambient_c=30, insulation_rating=90)
    assert result["fill_factor"] == 1.0


def test_leg_derate_factor_conduit_applies_fill_penalty():
    leg = RoutingLeg(installation_method="conduit_below_grade", length_ft=50, conductor_count=12)
    result = leg_derate_factor(leg, default_ambient_c=30, insulation_rating=90)
    assert result["fill_factor"] == 0.50  # Table 310.15(C)(1) band for 10-20 conductors


def test_leg_derate_factor_ambient_override_wins_over_default():
    leg = RoutingLeg(installation_method="free_air_or_tray", length_ft=50, ambient_c_override=45)
    result = leg_derate_factor(leg, default_ambient_c=30, insulation_rating=90)
    assert result["ambient_c"] == 45


def test_size_cable_with_routing_picks_worst_leg_as_governing():
    """A short conduit leg with many bundled conductors should govern over a
    much longer free-air leg, since NEC sizes to the most restrictive point."""
    legs = [
        RoutingLeg(installation_method="free_air_or_tray", length_ft=500, conductor_count=20),
        RoutingLeg(installation_method="conduit_below_grade", length_ft=10, conductor_count=20),
    ]
    result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90,
        voltage_drop_limit_pct=2.0, legs=legs, default_ambient_c=30,
    )
    assert result["governing_leg_index"] == 1
    assert result["total_length_ft"] == 510.0


def test_size_cable_with_routing_all_free_air_matches_flat_ampacity_calc():
    """Sanity check against the existing (non-routing-aware) ampacity_calc:
    a single free-air leg with the same inputs should reproduce the same
    required ampacity as ampacity_calc.size_conductor's fill_factor=1.0 path."""
    from app.ampacity_calc import temp_correction_factor

    current_a = 253.0
    legs = [RoutingLeg(installation_method="free_air_or_tray", length_ft=200, conductor_count=3)]
    result = size_cable_with_routing(
        current_a=current_a, voltage_v=800, insulation_rating=90,
        voltage_drop_limit_pct=1.5, legs=legs, default_ambient_c=35,
    )
    expected_required = current_a / temp_correction_factor(35, 90)
    assert result["governing_required_ampacity_a"] == pytest.approx(round(expected_required, 2))


def test_size_cable_with_routing_returns_none_conductor_when_no_size_fits():
    legs = [RoutingLeg(installation_method="conduit_below_grade", length_ft=10, conductor_count=2)]
    result = size_cable_with_routing(
        current_a=10000, voltage_v=800, insulation_rating=90,
        voltage_drop_limit_pct=2.0, legs=legs, default_ambient_c=30,
    )
    assert result["selected_conductor"] is None
    assert result["final_conductor"] is None
    assert result["voltage_drop"] is None
    assert result["legs"][0]["raceway"] is None


def test_final_conductor_reflects_voltage_drop_upsize_not_just_ampacity():
    """A long, tightly-limited run can need a bigger conductor for voltage
    drop than ampacity alone would pick -- final_conductor has to reflect
    that upsize, not just the ampacity-only selected_conductor."""
    legs = [RoutingLeg(installation_method="conduit_below_grade", length_ft=600, conductor_count=2)]
    result = size_cable_with_routing(
        current_a=250, voltage_v=1500, insulation_rating=90,
        voltage_drop_limit_pct=0.5, legs=legs, default_ambient_c=35,
    )
    assert result["voltage_drop"]["upsized"] is True
    assert result["final_conductor"] == result["voltage_drop"]["final_conductor"]
    assert result["final_conductor"] != result["selected_conductor"]


def test_size_cable_with_routing_requires_at_least_one_leg():
    with pytest.raises(ValueError):
        size_cable_with_routing(
            current_a=10, voltage_v=800, insulation_rating=90,
            voltage_drop_limit_pct=2.0, legs=[], default_ambient_c=30,
        )


# ---------------------------------------------------------------------------
# Integration with app/raceway_calc.py
# ---------------------------------------------------------------------------

def test_conduit_leg_gets_a_real_trade_size():
    legs = [RoutingLeg(installation_method="conduit_below_grade", length_ft=200, conductor_count=2, conduit_material="PVC_SCH40")]
    result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90,
        voltage_drop_limit_pct=2.0, legs=legs, default_ambient_c=30,
    )
    raceway = result["legs"][0]["raceway"]
    assert raceway is not None
    assert raceway["material"] == "PVC_SCH40"
    assert raceway["fits"] is True
    assert raceway["selected_trade_size_in"] is not None


def test_free_air_leg_without_size_as_tray_gets_no_raceway_spec():
    legs = [RoutingLeg(installation_method="free_air_or_tray", length_ft=50, conductor_count=2)]
    result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90,
        voltage_drop_limit_pct=2.0, legs=legs, default_ambient_c=30,
    )
    assert result["legs"][0]["raceway"] is None


def test_free_air_leg_with_size_as_tray_gets_a_real_tray_spec():
    legs = [RoutingLeg(installation_method="free_air_or_tray", length_ft=50, conductor_count=6, size_as_tray=True, tray_width_in=12)]
    result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90,
        voltage_drop_limit_pct=2.0, legs=legs, default_ambient_c=30,
    )
    raceway = result["legs"][0]["raceway"]
    assert raceway is not None
    assert "actual_fill_pct" in raceway
    assert "min_required_width_in" in raceway


def test_raceway_sizing_uses_final_not_ampacity_only_conductor():
    """The governing leg forces a voltage-drop upsize; a non-governing
    conduit leg's raceway must still be sized for that larger final
    conductor, not its own smaller ampacity-only pick."""
    legs = [
        RoutingLeg(installation_method="conduit_below_grade", length_ft=600, conductor_count=2),  # forces upsize
        RoutingLeg(installation_method="conduit_below_grade", length_ft=5, conductor_count=2),  # tiny, non-governing
    ]
    result = size_cable_with_routing(
        current_a=250, voltage_v=1500, insulation_rating=90,
        voltage_drop_limit_pct=0.5, legs=legs, default_ambient_c=35,
    )
    assert result["voltage_drop"]["upsized"] is True
    small_leg_raceway = result["legs"][1]["raceway"]
    assert small_leg_raceway["area_per_conductor_in2"] == pytest.approx(
        result["legs"][0]["raceway"]["area_per_conductor_in2"]
    )  # same conductor size sized on both legs


def test_ac_steel_conduit_adds_voltage_drop_reactance_multiplier():
    """Same current/length/conductor, only circuit_type + conduit material
    differ -- the steel-conduit AC run must show strictly more voltage drop
    than the PVC one, via raceway_calc's own reactance adder."""
    steel_legs = [RoutingLeg(installation_method="conduit_above_grade", length_ft=300, conductor_count=2, conduit_material="EMT")]
    pvc_legs = [RoutingLeg(installation_method="conduit_above_grade", length_ft=300, conductor_count=2, conduit_material="PVC_SCH40")]

    steel_result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90, voltage_drop_limit_pct=100.0,
        legs=steel_legs, default_ambient_c=30, circuit_type="ac",
    )
    pvc_result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90, voltage_drop_limit_pct=100.0,
        legs=pvc_legs, default_ambient_c=30, circuit_type="ac",
    )
    assert steel_result["voltage_drop"]["voltage_drop_v"] > pvc_result["voltage_drop"]["voltage_drop_v"]


def test_dc_circuit_never_gets_steel_conduit_multiplier():
    steel_legs_dc = [RoutingLeg(installation_method="conduit_above_grade", length_ft=300, conductor_count=2, conduit_material="EMT")]
    pvc_legs_dc = [RoutingLeg(installation_method="conduit_above_grade", length_ft=300, conductor_count=2, conduit_material="PVC_SCH40")]

    steel_result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90, voltage_drop_limit_pct=100.0,
        legs=steel_legs_dc, default_ambient_c=30, circuit_type="dc",
    )
    pvc_result = size_cable_with_routing(
        current_a=100, voltage_v=800, insulation_rating=90, voltage_drop_limit_pct=100.0,
        legs=pvc_legs_dc, default_ambient_c=30, circuit_type="dc",
    )
    assert steel_result["voltage_drop"]["voltage_drop_v"] == pytest.approx(pvc_result["voltage_drop"]["voltage_drop_v"])
