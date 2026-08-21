"""Live lookups against ashrae-meteo.info's own (undocumented) endpoints —
the same two POSTs its own map makes when a location is right-clicked or a
station marker is opened. Found by reading its JavaScript (ol_map.js and
index.php's inline script), not a published API — ashrae-meteo.info has
none. If it ever changes or blocks this, the ASHRAE Site Data panel's
manual paste-and-verify workflow still works on its own; this module is a
convenience layer in front of it, not a replacement for the engineer
checking the result.
"""

from __future__ import annotations

import math

import httpx
from pydantic import BaseModel

BASE_URL = "https://ashrae-meteo.info/v3.0"
USER_AGENT = "azimuth-solar-calc/1.0 (engineering@azimuth.energy; single-station lookup, same as manual map use)"


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class AshraeNearbyRequest(BaseModel):
    lat: float
    lon: float
    ashrae_version: str = "2025"
    limit: int = 3


def nearby_stations(request: AshraeNearbyRequest) -> dict:
    """ashrae-meteo.info's own nearest-station search. Its server 500s on a
    small `number` (tested live: 3 fails, 10 succeeds), so this always asks
    for 10 and keeps the closest `limit` — using our own haversine distance
    against the exact input coordinates rather than its opaque `tt` field.
    """
    result: dict = {"stations": [], "resolved": False, "warnings": []}
    try:
        with _client() as client:
            resp = client.post(
                f"{BASE_URL}/request_places.php",
                data={
                    "lat": f"{request.lat:.3f}",
                    "long": f"{request.lon:.3f}",
                    "number": 10,
                    "ashrae_version": request.ashrae_version,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        result["warnings"].append(f"nearby-station lookup failed: {exc}")
        return result

    stations = []
    for s in data.get("meteo_stations", []):
        try:
            s_lat, s_lon = float(s["lat"]), float(s["long"])
        except (KeyError, TypeError, ValueError):
            continue
        elev = s.get("elev")
        stations.append({
            "wmo": s.get("wmo", ""),
            "place": s.get("place", ""),
            "lat": s_lat,
            "lon": s_lon,
            "elev_m": float(elev) if elev not in (None, "") else None,
            "distance_mi": round(_haversine_miles(request.lat, request.lon, s_lat, s_lon), 1),
        })
    stations.sort(key=lambda s: s["distance_mi"])
    result["stations"] = stations[: request.limit]
    result["resolved"] = bool(result["stations"])
    if not result["stations"]:
        result["warnings"].append("no stations returned")
    return result


class AshraeStationDataRequest(BaseModel):
    wmo: str
    ashrae_version: str = "2025"
    si_ip: str = "SI"


# Fields with one unambiguous ASHRAE meaning. The raw response carries
# hundreds of monthly/percentile/solar fields (the full Chapter 14 table) —
# only these map cleanly onto the HMI's design-critical values. "Average
# high DB" has no equivalent single field in this dataset (the closest
# candidates are monthly means, not a labeled "average high"), so it's
# deliberately left out here — guessing at it would be worse than leaving
# it a manual entry for an NEC-compliance input.
_FIELD_MAP = {
    "heat_db_996": "heating_DB_99.6",
    "heat_db_99": "heating_DB_99",
    "cool_db_04": "cooling_DB_MCWB_0.4_DB",
    "cool_db_1": "cooling_DB_MCWB_1_DB",
    "cool_db_2": "cooling_DB_MCWB_2_DB",
    "extreme_min": "extreme_annual_DB_mean_min",
    "extreme_max": "extreme_annual_DB_mean_max",
}


def station_design_data(request: AshraeStationDataRequest) -> dict:
    """The same per-station fetch ashrae-meteo.info's map makes when a
    station marker is opened."""
    result: dict = {"resolved": False, "warnings": []}
    try:
        with _client() as client:
            resp = client.post(
                f"{BASE_URL}/request_meteo_parametres.php",
                data={"wmo": request.wmo, "ashrae_version": request.ashrae_version, "si_ip": request.si_ip},
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        result["warnings"].append(f"station data lookup failed: {exc}")
        return result

    stations = data.get("meteo_stations", [])
    if not stations:
        result["warnings"].append(f"no station data returned for WMO {request.wmo}")
        return result
    raw = stations[0]

    def num(key: str) -> float | None:
        try:
            return float(raw[key])
        except (KeyError, TypeError, ValueError):
            return None

    result.update({
        "resolved": True,
        "name": raw.get("place", ""),
        "wmo": raw.get("wmo", request.wmo),
        "edition": request.ashrae_version,
        "lat": num("lat"),
        "lon": num("long"),
        "elev_m": num("elev"),
        "std_pressure_kpa": num("stdp"),
        "time_zone": raw.get("time_zone", ""),
        "period": raw.get("period", ""),
        "grade": raw.get("grade", ""),
    })
    for field, source_key in _FIELD_MAP.items():
        result[field] = num(source_key)
    return result
