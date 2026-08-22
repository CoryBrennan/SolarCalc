"""End-to-end: POSTing the default project (every field at its default, which
mirrors the HMI draft's sample "REE ESTL Landfill" project exactly) should
reproduce every number verified live in the browser this session, in one
integrated response.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_serves_hmi_html():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Solar Calc Engine" in response.text
    assert "syncWithBackend" in response.text


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


def test_site_geocode_passes_address_through_to_lookup_and_returns_result(monkeypatch):
    captured = {}

    def fake_geocode_address(address):
        captured["street"] = address.street
        return {
            "latitude": 38.6273, "longitude": -90.1902, "elevation_m": 141.7,
            "timezone": "America/Chicago", "resolved": True, "warnings": [],
        }

    monkeypatch.setattr("app.main.geocode_lookup.geocode_address", fake_geocode_address)

    response = client.post("/site/geocode", json={"street": "1 Metropolitan Sq", "city": "St. Louis"})

    assert response.status_code == 200
    assert response.json()["timezone"] == "America/Chicago"
    assert captured["street"] == "1 Metropolitan Sq"


def test_ashrae_nearby_stations_passes_coordinates_through_to_lookup(monkeypatch):
    captured = {}

    def fake_nearby_stations(request):
        captured["lat"] = request.lat
        captured["limit"] = request.limit
        return {"stations": [{"wmo": "724395", "place": "ST LOUIS REGIONAL AP, IL, USA", "distance_mi": 0.0}], "resolved": True, "warnings": []}

    monkeypatch.setattr("app.main.ashrae_lookup.nearby_stations", fake_nearby_stations)

    response = client.post("/ashrae/nearby-stations", json={"lat": 38.883, "lon": -90.050, "limit": 3})

    assert response.status_code == 200
    assert response.json()["stations"][0]["wmo"] == "724395"
    assert captured["lat"] == 38.883
    assert captured["limit"] == 3


def test_ashrae_station_data_passes_wmo_through_to_lookup(monkeypatch):
    captured = {}

    def fake_station_design_data(request):
        captured["wmo"] = request.wmo
        return {"resolved": True, "warnings": [], "name": "ST LOUIS REGIONAL AP, IL, USA", "cool_db_2": 31.6}

    monkeypatch.setattr("app.main.ashrae_lookup.station_design_data", fake_station_design_data)

    response = client.post("/ashrae/station-data", json={"wmo": "724395"})

    assert response.status_code == 200
    assert response.json()["cool_db_2"] == 31.6
    assert captured["wmo"] == "724395"


def test_site_gps_validate_end_to_end_with_real_emlid_export_row():
    # TCU1 row verbatim from a real Emlid Flow 360 export (Ameren Peoria,
    # Illinois West ftUS State Plane) -- FIX solution, matched to a design
    # point at the exact same coordinates.
    emlid_csv = (
        "Name,Code,Code description,Easting,Northing,Elevation,Description,Longitude,Latitude,"
        "Ellipsoidal height,Origin,Tilt angle,Easting RMS,Northing RMS,Elevation RMS,Lateral RMS,"
        "Antenna height,Antenna height units,Solution status,Correction type\n"
        "TCU1,,,2434191.346,1475350.261,570.143,,-89.67023838,40.71672295,461.923,Global,1.7,"
        "0.039,0.038,0.038,0.054,6.812,ft,FIX,RTK\n"
    )

    response = client.post(
        "/site/gps-validate",
        json={
            "design_points": [
                {"tag": "TCU1", "easting_ft": 2434191.346, "northing_ft": 1475350.261, "elevation_ft": 570.143}
            ],
            "emlid_csv": emlid_csv,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["passed"] == 1
    assert body["results"][0]["lateral_offset_ft"] == 0.0


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
    assert body["document_header"]["header"]["site_address"] == "IL"
    assert "business_name" in body["document_header"]["missing_fields"]

    assert body["site"]["num_modules"] == 9465
    assert body["site"]["target_ac_capacity_w"] == 5_000_000
    assert body["site"]["target_dc_capacity_w"] == 6_500_000
    assert body["site"]["calculated_dc_ac_ratio"] == 1.3
    assert body["site"]["actual_dc_capacity_w"] == 6_814_800
    assert body["site"]["actual_ac_capacity_w"] == 5_250_000
    assert body["site"]["storage_target_wh"] == 0
    assert body["site"]["other_generation_target_w"] == 0


def test_calculate_passes_storage_and_other_generation_targets_through():
    response = client.post(
        "/calculate",
        json={"site": {"storage_target_wh": 2_000_000, "other_generation_target_w": 500_000}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["site"]["storage_target_wh"] == 2_000_000
    assert body["site"]["other_generation_target_w"] == 500_000


def test_calculate_accepts_der_number_and_ahj_by_discipline():
    response = client.post(
        "/calculate",
        json={
            "jurisdiction": {
                "der_number": "DER-000123",
                "ahj_by_discipline": {
                    "electrical": {
                        "office": "St. Clair County Electrical Inspections",
                        "name": "Jane Doe",
                        "phone": "618-555-0100",
                        "email": "jane@example.com",
                    },
                },
            }
        },
    )
    assert response.status_code == 200


def test_calculate_rejects_unknown_module_sku():
    response = client.post("/calculate", json={"module": {"sku": "999"}})
    assert response.status_code == 422


def test_calculate_accepts_a_custom_module_sku_with_inline_electricals():
    """A module the HMI's Module Spec panel added by hand or approved from a
    datasheet has a SKU never in module_catalog.MODULE_SKUS -- as long as the
    request supplies real electricals inline, it should resolve instead of
    422ing (module_catalog.resolve_module_spec)."""
    response = client.post(
        "/calculate",
        json={
            "module": {
                "sku": "doc-42", "manufacturer": "TestCo", "quantity": 100,
                "pmax": 500, "voc": 45.0, "vmp": 38.0, "isc": 14.0, "imp": 13.2,
                "bifacial_pmax": 550, "temp_coeff_voc_pct_per_c": -0.3, "temp_coeff_isc_pct_per_c": 0.05,
            },
            "combiner_rows": [{"inputs": 2, "bus_rating_a": 200, "module_sku": "doc-42"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["site"]["actual_dc_capacity_w"] == 500 * 100
    assert body["combiners"]["rows"][0]["module_sku"] == "doc-42"


def test_calculate_rejects_combiner_row_referencing_a_different_unknown_sku():
    """The fallback only ever substitutes the *main* module's electricals --
    a combiner row pointing at some other, genuinely unknown SKU must still
    422 rather than silently borrowing the wrong module's data."""
    response = client.post(
        "/calculate",
        json={
            "module": {
                "sku": "doc-42", "pmax": 500, "voc": 45.0, "vmp": 38.0, "isc": 14.0, "imp": 13.2,
            },
            "combiner_rows": [{"inputs": 2, "bus_rating_a": 200, "module_sku": "doc-99"}],
        },
    )
    assert response.status_code == 422


def test_calculate_resolves_a_combiner_row_on_a_second_custom_module_via_custom_modules():
    """A combiner row can now reference a genuinely different non-catalog
    module than the project's main one, as long as its full electricals are
    supplied under custom_modules -- the HMI's Array schedule allows a
    different module per inverter, so a combiner fed by that inverter's
    strings needs to resolve its own module, not just the main one."""
    response = client.post(
        "/calculate",
        json={
            "module": {
                "sku": "doc-42", "pmax": 500, "voc": 45.0, "vmp": 38.0, "isc": 14.0, "imp": 13.2,
            },
            "custom_modules": {
                "doc-99": {"sku": "doc-99", "pmax": 600, "voc": 48.0, "vmp": 40.0, "isc": 15.0, "imp": 14.1},
            },
            "combiner_rows": [{"inputs": 2, "bus_rating_a": 200, "module_sku": "doc-99"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["combiners"]["rows"][0]["module_sku"] == "doc-99"
    # Isc 15 x 1.25 = 18.75 -> next standard fuse size above that.
    assert body["combiners"]["rows"][0]["input_min_fuse_a"] == 15.0 * 1.25


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
