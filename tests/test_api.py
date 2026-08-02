"""End-to-end: POSTing the default project (every field at its default, which
mirrors the HMI draft's sample "REE ESTL Landfill" project exactly) should
reproduce every number verified live in the browser this session, in one
integrated response.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_cors_allows_cross_origin_calls():
    response = client.post(
        "/calculate",
        json={},
        headers={"Origin": "http://127.0.0.1:8791"},
    )
    assert response.headers["access-control-allow-origin"] == "*"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_calculate_default_project_matches_browser_verified_numbers():
    response = client.post("/calculate", json={})
    assert response.status_code == 200
    body = response.json()

    assert body["site"]["num_inverters"] == 15

    assert body["jurisdiction"]["resolved"] is False
    assert body["jurisdiction"]["nec_edition"] == "UNKNOWN — confirm with AHJ"

    assert body["combiners"]["combiner_count"] == 2
    assert body["combiners"]["total_strings"] == 5
    assert body["combiners"]["rows"][0]["output_conductor"] == "8 AWG"
    assert body["combiners"]["rows"][1]["output_conductor"] == "6 AWG"

    assert body["ocpd"]["standard_size_a"] == 350
    assert body["ocpd"]["manufacturer_check_ok"] is True

    assert body["switchboard"]["actual_backfed_a"] == 5250.0
    assert body["switchboard"]["passes"] is False

    assert body["ampacity"]["selected_conductor"] == "10 AWG"
    assert body["ampacity"]["required_ampacity_a"] == 30.09

    assert body["bonding"]["separately_derived_system"] is True
    assert body["bonding"]["secondary_conductor"] == "10 AWG"

    assert body["voltage_drop"]["voltage_drop_pct"] == 0.67
    assert body["voltage_drop"]["passes"] is True

    assert body["placarding"]["estimated_total"] == 396.95

    assert len(body["etap"]["rows"]) == 16
    assert body["etap"]["total_source_mva_at_poi"] == 5.25

    assert body["iv_curve"]["expected"]["voc"] == 1326.77

    assert body["document_header"]["header"]["project_name"] == "REE ESTL Landfill"
    assert "e911_address" in body["document_header"]["missing_fields"]


def test_calculate_rejects_unknown_module_sku():
    response = client.post("/calculate", json={"module": {"sku": "999"}})
    assert response.status_code == 422


def test_calculate_switchboard_passes_when_topology_is_direct():
    """When the inverter takes PV source circuits directly (no combiner), the
    combiner-derived backfed load shouldn't apply — this project's switchboard
    should be evaluated on its own configured busbar/main regardless."""
    response = client.post("/calculate", json={"inverter": {"dc_topology": "direct"}})
    assert response.status_code == 200
    body = response.json()
    assert body["combiners"]["combiner_count"] == 0


def test_generate_switchboard_config_matches_calculate_endpoint():
    """The dedicated CAD-generator endpoint should agree with /calculate's own
    ocpd/switchboard numbers for the same default project — same underlying
    calc, just reshaped for ac-switchboard-addin."""
    calc_body = client.post("/calculate", json={}).json()
    config = client.post("/generate/switchboard-config", json={}).json()

    assert config["tag"] == "SWBD-1"
    assert config["phase_config"] == "3PH"
    assert config["bus_rating_amps"] == 1200
    assert config["backfeed_total_amps"] == int(calc_body["switchboard"]["actual_backfed_a"])
    assert len(config["positions"]) == calc_body["site"]["num_inverters"] == 15
    assert config["positions"][0]["breaker_rating"] == f"{calc_body['ocpd']['standard_size_a']}A"
    assert config["main_breaker"]["rating"] == "800A"


def test_generate_switchboard_config_rejects_unknown_sku():
    response = client.post("/generate/switchboard-config", json={"module": {"sku": "999"}})
    assert response.status_code == 422
