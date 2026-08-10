"""Resolves a SiteAddress to lat/long, elevation, and IANA timezone — three
free, keyless public services chained together (Nominatim, USGS EPQS,
timezonefinder). Each step degrades independently: a failure in one doesn't
block the others, since the eventual consumer (PVsyst .SIT creation via
PVsystCLI's create-site) can proceed with partial coordinates, and whatever
didn't resolve just stays None for the engineer to fill in by hand.

USGS EPQS is US-elevation-only, matching this app's current site coverage;
a non-US site would resolve lat/long/timezone but not elevation.
"""

from __future__ import annotations

import httpx
from timezonefinder import TimezoneFinder

from app.models import SiteAddress

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USGS_ELEVATION_URL = "https://epqs.nationalmap.gov/v1/json"
USER_AGENT = "azimuth-solar-calc/1.0 (engineering@azimuth.energy)"

_tf = TimezoneFinder()


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0)


def geocode_address(address: SiteAddress) -> dict:
    result: dict = {
        "latitude": None,
        "longitude": None,
        "elevation_m": None,
        "timezone": None,
        "resolved": False,
        "warnings": [],
    }

    query = address.full_address()
    if not query:
        result["warnings"].append("no address provided")
        return result

    with _client() as client:
        try:
            resp = client.get(NOMINATIM_URL, params={"q": query, "format": "json", "limit": 1})
            resp.raise_for_status()
            hits = resp.json()
            if not hits:
                result["warnings"].append(f"no geocoding match for '{query}'")
                return result
            result["latitude"] = float(hits[0]["lat"])
            result["longitude"] = float(hits[0]["lon"])
            result["resolved"] = True
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
            result["warnings"].append(f"geocoding failed: {exc}")
            return result

        try:
            resp = client.get(
                USGS_ELEVATION_URL,
                params={"x": result["longitude"], "y": result["latitude"], "units": "Meters"},
            )
            resp.raise_for_status()
            result["elevation_m"] = round(float(resp.json()["value"]), 1)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            result["warnings"].append(f"elevation lookup failed: {exc}")

    try:
        tz = _tf.timezone_at(lat=result["latitude"], lng=result["longitude"])
        if tz:
            result["timezone"] = tz
        else:
            result["warnings"].append("timezone lookup returned no match")
    except Exception as exc:  # defensive — timezonefinder shouldn't raise on valid coords
        result["warnings"].append(f"timezone lookup failed: {exc}")

    return result
