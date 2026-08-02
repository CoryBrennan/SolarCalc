"""Regression baseline: the default project's 500 kVA delta/grounded-wye
transformer is the exact same one verified as SDS=Yes in Bonding & Grounding
earlier — this proves the transformer block reuses that logic rather than
re-deriving it.
"""

from app.models import MvGoabConfig, MvMeterConfig, MvRecloserConfig, TransformerConfig
from app.static_device_block import (
    build_mv_goab_config,
    build_mv_meter_config,
    build_mv_recloser_config,
    build_transformer_config,
)


def test_transformer_sds_flag_matches_bonding_calc():
    config = build_transformer_config("XFMR-1", TransformerConfig())
    assert config["tag"] == "XFMR-1"
    assert config["device_type"] == "transformer"
    assert config["attributes"]["SDS_FLAG"] == "Yes"
    assert config["attributes"]["GROUNDING_TYPE"] == "Solidly grounded"
    assert config["attributes"]["KVA_RATING"] == "500"
    assert config["attributes"]["WINDING_CONFIG"] == "Delta-Grounded Wye"


def test_transformer_non_sds_grounding_type():
    transformer = TransformerConfig(primary_winding="grounded_wye", secondary_winding="grounded_wye")
    config = build_transformer_config("XFMR-2", transformer)
    assert config["attributes"]["SDS_FLAG"] == "No"


def test_mv_recloser_config():
    config = build_mv_recloser_config(MvRecloserConfig())
    assert config["device_type"] == "mv_recloser"
    assert config["attributes"]["VOLTAGE_CLASS"] == "38kV"
    assert config["attributes"]["INTERRUPTING_RATING"] == "12.5kA"


def test_mv_goab_config():
    config = build_mv_goab_config(MvGoabConfig())
    assert config["device_type"] == "mv_goab"
    assert config["attributes"]["LOAD_BREAK_RATING"] == "600A"


def test_mv_meter_config():
    config = build_mv_meter_config(MvMeterConfig())
    assert config["device_type"] == "mv_meter"
    assert config["attributes"]["CT_RATIO"] == "400:5"
    assert config["attributes"]["PT_RATIO"] == "200:1"
