"""Regression baseline for the AC-side inverter block. The default project is
15 x 350kW Chint inverters at 800V, 253A max output, behind 9465 x 720W
modules — so 350.000 KWAC and 6,814,800 / 15 = 454.320 KWDC per inverter, with
a 253 x 1.25 = 316.25A -> 350A output breaker.
"""

from app.inverter_ac_block import (
    build_inverter_ac_configs,
    format_phase_config,
    per_inverter_dc_capacity_w,
)
from app.models import InverterSpec, ProjectInput


def test_default_project_nameplate_matches_project_data():
    configs = build_inverter_ac_configs(ProjectInput())

    assert len(configs) == 15  # one block per physical inverter
    first = configs[0]
    assert first["tag"] == "INV-1"

    attrs = first["attributes"]
    assert attrs["TAG1"] == "INV-1"
    assert attrs["MFG"] == "CHINT POWER SYSTEMS"
    assert attrs["CAT"] == "CPS SCH350KTL-DO/US-800"
    assert attrs["KWAC"] == "350.000 KWAC"
    assert attrs["KWDC"] == "454.320 KWDC"
    assert attrs["VAC"] == "800 VAC"
    assert attrs["PHASE_CONFIG"] == "3Ø 3W + PE"


def test_tags_run_one_per_inverter_and_match_switchboard_positions():
    configs = build_inverter_ac_configs(ProjectInput(inverter=InverterSpec(quantity=3)))
    assert [c["tag"] for c in configs] == ["INV-1", "INV-2", "INV-3"]


def test_ocpd_is_the_inverter_output_breaker_with_pole_count():
    config = build_inverter_ac_configs(ProjectInput())[0]
    # 253A x 1.25 = 316.25A -> next standard size 350A, 3 poles.
    assert config["ocpd_rating"] == "350 A/3"
    assert config["ocpd_within_mfr_max"] is True


def test_ocpd_ignores_the_ocpd_circuit_selector():
    """The AC block's breaker is always the inverter output breaker, even when
    the project's OCPD panel is pointed at a DC circuit."""
    from app.models import OcpdInput

    project = ProjectInput(ocpd=OcpdInput(circuit="pv_source"))
    assert build_inverter_ac_configs(project)[0]["ocpd_rating"] == "350 A/3"


def test_ocpd_over_manufacturer_max_is_flagged_not_silently_capped():
    project = ProjectInput(inverter=InverterSpec(max_output_current_a=253, manufacturer_max_ocpd_a=300))
    config = build_inverter_ac_configs(project)[0]
    assert config["ocpd_rating"] == "350 A/3"
    assert config["ocpd_within_mfr_max"] is False


def test_snip_reference_values_reproduce_the_sungrow_nameplate():
    """The SG200HX-US block this was drawn from: 200kW AC / 279.3kW DC at
    600V. Confirms the formatting produces that nameplate exactly."""
    project = ProjectInput(
        inverter=InverterSpec(
            manufacturer="Sungrow",
            catalog_number="SG200HX-US",
            ac_rating_w=200_000,
            nominal_ac_voltage_v=600.0,
            quantity=1,
        ),
        # 279.3 kWDC behind the single inverter: 388 x 720W = 279,360W.
        module=ProjectInput().module.model_copy(update={"quantity": 388}),
    )
    attrs = build_inverter_ac_configs(project)[0]["attributes"]
    assert attrs["MFG"] == "SUNGROW"
    assert attrs["CAT"] == "SG200HX-US"
    assert attrs["KWAC"] == "200.000 KWAC"
    assert attrs["KWDC"] == "279.360 KWDC"
    assert attrs["VAC"] == "600 VAC"
    assert attrs["PHASE_CONFIG"] == "3Ø 3W + PE"


def test_per_inverter_dc_splits_the_array_evenly():
    project = ProjectInput()
    assert per_inverter_dc_capacity_w(project) == 720 * 9465 / 15


def test_per_inverter_dc_is_zero_rather_than_dividing_by_zero():
    project = ProjectInput(inverter=InverterSpec(quantity=0))
    assert per_inverter_dc_capacity_w(project) == 0.0
    assert build_inverter_ac_configs(project) == []


def test_phase_config_formatting_variants():
    assert format_phase_config(3, 3, True) == "3Ø 3W + PE"
    assert format_phase_config(3, 4, True) == "3Ø 4W + PE"
    assert format_phase_config(1, 2, False) == "1Ø 2W"


def test_detail_ref_defaults_empty_and_passes_through_when_set():
    assert build_inverter_ac_configs(ProjectInput())[0]["detail_ref"] == ""

    project = ProjectInput(inverter=InverterSpec(ac_detail_ref="C"))
    assert build_inverter_ac_configs(project)[0]["detail_ref"] == "C"
