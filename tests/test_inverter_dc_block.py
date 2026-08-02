"""Regression baseline: the mppt variant uses the default project's 720W
module (Isc=18.49A) across 15 MPPTs x 2 strings; the combiner variant is
derived from the same default combiner schedule verified elsewhere
(2 and 3 inputs, both 720W, 35A max fuse -> 25A input fuse both rows).
"""

from app.inverter_dc_block import build_inverter_dc_combiner_configs, build_inverter_dc_mppt_config
from app.models import CombinerRow, ProjectInput


def test_mppt_variant_matches_default_project():
    project = ProjectInput()
    config = build_inverter_dc_mppt_config(project)

    assert config["tag"] == "INV-1"
    assert config["block_variant"] == "mppt"
    assert config["disconnect_placement"] == "per_mppt"
    assert config["max_dc_voltage"] == "1500V"
    assert len(config["mppt_groups"]) == 15

    first_group = config["mppt_groups"][0]
    assert first_group["mppt_index"] == 1
    assert len(first_group["strings"]) == 2
    # Isc=18.49A x 1.25 = 23.11A -> next standard size 25A.
    assert first_group["strings"][0]["ocpd_rating"] == "25A"
    assert first_group["strings"][0]["string_id"] == 1

    # disconnect covers 2 strings: 2 x 18.49 = 36.98A x 1.25 = 46.225A -> 50A.
    assert config["disconnect_rating"] == "50A"
    assert config["max_isc_per_mppt"] == "36.98A"


def test_combiner_variant_matches_default_combiner_schedule():
    project = ProjectInput()
    configs = build_inverter_dc_combiner_configs(project)

    assert len(configs) == 2
    dcc1, dcc2 = configs

    assert dcc1["tag"] == "DCC-1"
    assert dcc1["block_variant"] == "combiner"
    assert dcc1["disconnect_placement"] == "single"
    assert len(dcc1["mppt_groups"][0]["strings"]) == 2  # row 1 has 2 inputs
    assert dcc1["mppt_groups"][0]["strings"][0]["ocpd_rating"] == "25A"
    # output ampacity 50A -> disconnect rounds to 50A (already a standard size)
    assert dcc1["disconnect_rating"] == "50A"

    assert dcc2["tag"] == "DCC-2"
    assert len(dcc2["mppt_groups"][0]["strings"]) == 3  # row 2 has 3 inputs
    # output ampacity 75A -> 75 isn't a standard size, next one up is 80A.
    assert dcc2["disconnect_rating"] == "80A"


def test_combiner_variant_scales_with_custom_rows():
    project = ProjectInput(combiner_rows=[CombinerRow(inputs=5, bus_rating_a=400, module_sku="720")])
    configs = build_inverter_dc_combiner_configs(project)
    assert len(configs) == 1
    assert configs[0]["tag"] == "DCC-1"
    assert len(configs[0]["mppt_groups"][0]["strings"]) == 5
