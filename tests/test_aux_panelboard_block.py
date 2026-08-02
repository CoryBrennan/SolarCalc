from app.aux_panelboard_block import build_aux_panelboard_config
from app.models import AuxLoadCircuit, AuxPanelboardConfig


def test_default_config_matches_spec_example():
    config = build_aux_panelboard_config(AuxPanelboardConfig())
    assert config["tag"] == "AUX-1"
    assert config["main_breaker_rating"] == "100A"
    assert config["voltage"] == "120/240V"
    assert config["phase"] == "1PH"
    assert config["positions"] == [
        {"position": 1, "circuit_tag": "CKT-1", "breaker_rating": "20A", "description": "Lighting"},
        {"position": 2, "circuit_tag": "CKT-2", "breaker_rating": "20A", "description": "Receptacles"},
    ]


def test_custom_circuits_pass_through():
    config = AuxPanelboardConfig(
        tag="AUX-2",
        circuits=[
            AuxLoadCircuit(position=1, circuit_tag="CKT-1", breaker_rating_a=30, description="SCADA panel"),
        ],
    )
    result = build_aux_panelboard_config(config)
    assert len(result["positions"]) == 1
    assert result["positions"][0]["breaker_rating"] == "30A"
