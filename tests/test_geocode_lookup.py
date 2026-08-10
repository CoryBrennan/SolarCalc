"""Geocode lookup: each of the three chained lookups (Nominatim, USGS
elevation, timezonefinder) degrades independently on failure (mocks the
HTTP client entirely -- no network access in tests; timezonefinder itself
runs for real since it's an offline library)."""

from __future__ import annotations

import httpx
import pytest

from app.geocode_lookup import geocode_address
from app.models import SiteAddress

STL_ADDRESS = SiteAddress(street="1 Metropolitan Sq", city="St. Louis", state="MO", zip="63102")


def _mock_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})


def _nominatim_hit(lat="38.6273", lon="-90.1902"):
    return httpx.Response(200, json=[{"lat": lat, "lon": lon}])


def _usgs_hit(value="141.7"):
    return httpx.Response(200, json={"value": value})


def test_full_resolution_succeeds(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in str(request.url):
            return _nominatim_hit()
        return _usgs_hit()

    monkeypatch.setattr("app.geocode_lookup._client", _mock_client(handler))

    result = geocode_address(STL_ADDRESS)

    assert result["resolved"] is True
    assert result["latitude"] == pytest.approx(38.6273)
    assert result["longitude"] == pytest.approx(-90.1902)
    assert result["elevation_m"] == pytest.approx(141.7)
    assert result["timezone"] == "America/Chicago"
    assert result["warnings"] == []


def test_empty_address_short_circuits_with_no_network_call(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not make a network call for an empty address")

    monkeypatch.setattr("app.geocode_lookup._client", _mock_client(handler))

    result = geocode_address(SiteAddress(state=""))

    assert result["resolved"] is False
    assert "no address provided" in result["warnings"][0]


def test_no_geocoding_match(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    monkeypatch.setattr("app.geocode_lookup._client", _mock_client(handler))

    result = geocode_address(STL_ADDRESS)

    assert result["resolved"] is False
    assert result["latitude"] is None
    assert "no geocoding match" in result["warnings"][0]


def test_geocoding_service_error_is_caught(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr("app.geocode_lookup._client", _mock_client(handler))

    result = geocode_address(STL_ADDRESS)

    assert result["resolved"] is False
    assert "geocoding failed" in result["warnings"][0]


def test_elevation_failure_does_not_block_lat_long_or_timezone(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in str(request.url):
            return _nominatim_hit()
        return httpx.Response(500)

    monkeypatch.setattr("app.geocode_lookup._client", _mock_client(handler))

    result = geocode_address(STL_ADDRESS)

    assert result["resolved"] is True
    assert result["latitude"] is not None
    assert result["elevation_m"] is None
    assert result["timezone"] == "America/Chicago"
    assert "elevation lookup failed" in result["warnings"][0]
