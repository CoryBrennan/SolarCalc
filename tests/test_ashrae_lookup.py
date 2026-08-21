"""ASHRAE live lookup: mocks the HTTP client entirely (no real network
calls against ashrae-meteo.info in tests) -- same style as
test_geocode_lookup.py."""

from __future__ import annotations

import httpx
import pytest

from app.ashrae_lookup import (
    AshraeNearbyRequest,
    AshraeStationDataRequest,
    nearby_stations,
    station_design_data,
)

# Trimmed from a real response captured live against St. Louis (38.883, -90.050).
NEARBY_FIXTURE = {
    "meteo_stations": [
        {"wmo": "724395", "place": "ST LOUIS REGIONAL AP, IL, USA", "lat": "38.883", "long": "-90.050", "elev": "166"},
        {"wmo": "724340", "place": "ST LOUIS LAMBERT, MO, USA", "lat": "38.752", "long": "-90.373", "elev": "162"},
        {"wmo": "724347", "place": "ST CHARLES COUNTY AP, MO, USA", "lat": "38.930", "long": "-90.433", "elev": "132"},
        {"wmo": "725314", "place": "ST LOUIS DOWNTOWN, IL, USA", "lat": "38.564", "long": "-90.149", "elev": "123"},
    ]
}

STATION_DATA_FIXTURE = {
    "meteo_stations": [
        {
            "place": "ST LOUIS REGIONAL AP, IL, USA",
            "wmo": "724395",
            "lat": "38.883",
            "long": "-90.050",
            "elev": "166",
            "stdp": "99.35",
            "time_zone": "-6.00 (NAC)",
            "period": "2005-2023",
            "grade": "A",
            "heating_DB_99.6": "-14.6",
            "heating_DB_99": "-11.5",
            "cooling_DB_MCWB_0.4_DB": "34.4",
            "cooling_DB_MCWB_1_DB": "33.0",
            "cooling_DB_MCWB_2_DB": "31.6",
            "extreme_annual_DB_mean_min": "-18.0",
            "extreme_annual_DB_mean_max": "36.5",
        }
    ]
}


def _mock_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})


def test_nearby_stations_returns_closest_n_sorted_by_our_own_distance(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "request_places.php" in str(request.url)
        return httpx.Response(200, json=NEARBY_FIXTURE)

    monkeypatch.setattr("app.ashrae_lookup._client", _mock_client(handler))

    result = nearby_stations(AshraeNearbyRequest(lat=38.883, lon=-90.050, limit=3))

    assert result["resolved"] is True
    assert result["warnings"] == []
    assert len(result["stations"]) == 3
    assert result["stations"][0]["wmo"] == "724395"
    assert result["stations"][0]["distance_mi"] == 0.0
    # sorted ascending by distance
    distances = [s["distance_mi"] for s in result["stations"]]
    assert distances == sorted(distances)


def test_nearby_stations_service_error_is_caught(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr("app.ashrae_lookup._client", _mock_client(handler))

    result = nearby_stations(AshraeNearbyRequest(lat=38.883, lon=-90.050))

    assert result["resolved"] is False
    assert result["stations"] == []
    assert "nearby-station lookup failed" in result["warnings"][0]


def test_station_design_data_maps_only_unambiguous_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "request_meteo_parametres.php" in str(request.url)
        return httpx.Response(200, json=STATION_DATA_FIXTURE)

    monkeypatch.setattr("app.ashrae_lookup._client", _mock_client(handler))

    result = station_design_data(AshraeStationDataRequest(wmo="724395"))

    assert result["resolved"] is True
    assert result["name"] == "ST LOUIS REGIONAL AP, IL, USA"
    assert result["heat_db_996"] == pytest.approx(-14.6)
    assert result["heat_db_99"] == pytest.approx(-11.5)
    assert result["cool_db_04"] == pytest.approx(34.4)
    assert result["cool_db_1"] == pytest.approx(33.0)
    assert result["cool_db_2"] == pytest.approx(31.6)
    assert result["extreme_min"] == pytest.approx(-18.0)
    assert result["extreme_max"] == pytest.approx(36.5)
    # "average high" has no unambiguous source field -- deliberately absent.
    assert "avg_high" not in result


def test_station_design_data_unknown_wmo_is_caught(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"meteo_stations": []})

    monkeypatch.setattr("app.ashrae_lookup._client", _mock_client(handler))

    result = station_design_data(AshraeStationDataRequest(wmo="000000"))

    assert result["resolved"] is False
    assert "no station data returned" in result["warnings"][0]
