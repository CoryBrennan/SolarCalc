"""Validates as-built equipment/pile positions against design coordinates.

Feeds off Emlid Flow 360's CSV export (RTK GPS field survey) -- Name,
Easting, Northing, Elevation, Latitude, Longitude, Solution status, RMS,
etc. -- compared against a design point list. Design points can carry
either projected coordinates (easting/northing, e.g. from a DWG scan --
app/pvcase_dwg_scan.py -- in the same State Plane CRS as the field survey)
or geodetic lat/long (e.g. from PVCase's "Piling information" sheet --
app/pvcase_bom_import.py -- which reports WGS84 lat/long directly, so no
shared projected CRS needs confirming). A pair with only one of the two
coordinate systems on each side can't be compared -- see _lateral_offset_ft.

Design coordinates aren't a ProjectInput field anywhere in this app yet, so
this takes them as an explicit input rather than pulling from a project,
until that's decided.
"""

from __future__ import annotations

import csv
import io
import math

from pydantic import BaseModel

from app.pvcase_bom_import import PileRow, parse_pvcase_bom

# Emlid Flow only reports FIX when the rover has an RTK carrier-phase fixed
# solution (~cm-level, per the Lateral RMS column). SINGLE means no base
# correction reached the rover (autonomous GPS, meter-level) and FLOAT means
# an RTK solution that hasn't converged to integer ambiguity yet
# (decimeter-level). Neither should be trusted as as-built ground truth.
ACCEPTABLE_SOLUTION_STATUSES = {"FIX"}

# Flat-earth approximation (good to well under an inch over a site-sized
# area, a few miles across at most) rather than pulling in a full geodesy
# dependency for distances this short.
_METERS_PER_DEG_LAT = 111_320.0
_FT_PER_METER = 3.28084


class DesignPoint(BaseModel):
    tag: str
    easting_ft: float | None = None
    northing_ft: float | None = None
    elevation_ft: float | None = None
    latitude_deg: float | None = None
    longitude_deg: float | None = None


class AsBuiltPoint(BaseModel):
    name: str
    easting_ft: float | None = None
    northing_ft: float | None = None
    elevation_ft: float | None = None
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    solution_status: str = ""
    lateral_rms_ft: float | None = None


def _geodesic_offset_ft(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat_avg_rad = math.radians((lat1 + lat2) / 2)
    dlat_m = (lat2 - lat1) * _METERS_PER_DEG_LAT
    dlon_m = (lon2 - lon1) * _METERS_PER_DEG_LAT * math.cos(lat_avg_rad)
    return math.hypot(dlat_m, dlon_m) * _FT_PER_METER


def _lateral_offset_ft(design: DesignPoint, point: AsBuiltPoint) -> float | None:
    """Prefers projected easting/northing when both sides have it (an exact
    planar distance); falls back to geodetic lat/long. Returns None if
    neither side shares a coordinate system with the other."""
    if design.easting_ft is not None and design.northing_ft is not None and (
        point.easting_ft is not None and point.northing_ft is not None
    ):
        return math.hypot(point.easting_ft - design.easting_ft, point.northing_ft - design.northing_ft)
    if design.latitude_deg is not None and design.longitude_deg is not None and (
        point.latitude_deg is not None and point.longitude_deg is not None
    ):
        return _geodesic_offset_ft(design.latitude_deg, design.longitude_deg, point.latitude_deg, point.longitude_deg)
    return None


def piles_to_design_points(piles: list[PileRow]) -> list[DesignPoint]:
    """Converts parsed PVCase "Piling information" rows to DesignPoints,
    using each pile's lat/long (not its local project X/Y, which isn't in a
    known CRS on its own). `tag` is PVCase's own frame+pile identity (e.g.
    "12A") -- confirm it actually matches your field crew's point-naming
    convention (e.g. an Emlid Flow "TCU12") before trusting the match rate;
    this module has no way to verify that mapping itself."""
    return [
        DesignPoint(
            tag=p.tag,
            latitude_deg=p.latitude,
            longitude_deg=p.longitude,
            elevation_ft=p.z_terrain_ft,
        )
        for p in piles
    ]


class GpsValidationTolerances(BaseModel):
    lateral_tolerance_ft: float = 0.5
    elevation_tolerance_ft: float = 0.5
    max_lateral_rms_ft: float = 0.15


class GpsValidationRequest(BaseModel):
    design_points: list[DesignPoint]
    emlid_csv: str
    tolerances: GpsValidationTolerances = GpsValidationTolerances()


class PileValidationRequest(BaseModel):
    """Path-based like PvcaseValidateRequest (app/pvcase_validate.py) -- the
    BOM lives in the engineer's Dropbox-synced project folder on this same
    machine, so pointing at a path avoids re-uploading it through the
    browser."""

    bom_path: str
    emlid_csv: str
    tolerances: GpsValidationTolerances = GpsValidationTolerances()


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)


def parse_emlid_csv(csv_text: str) -> list[AsBuiltPoint]:
    """Parses an Emlid Flow 360 point-export CSV (the "Name, Code, Code
    description, Easting, Northing, Elevation, ... Solution status,
    Correction type, ..." column layout). Rows with no Name are skipped."""
    reader = csv.DictReader(io.StringIO(csv_text))
    points: list[AsBuiltPoint] = []
    for row in reader:
        name = (row.get("Name") or "").strip()
        if not name:
            continue
        points.append(
            AsBuiltPoint(
                name=name,
                easting_ft=float(row["Easting"]),
                northing_ft=float(row["Northing"]),
                elevation_ft=_parse_float(row.get("Elevation")),
                latitude_deg=_parse_float(row.get("Latitude")),
                longitude_deg=_parse_float(row.get("Longitude")),
                solution_status=(row.get("Solution status") or "").strip().upper(),
                lateral_rms_ft=_parse_float(row.get("Lateral RMS")),
            )
        )
    return points


def _match_key(name: str) -> str:
    return name.strip().upper()


def validate_request(request: GpsValidationRequest) -> dict:
    as_built_points = parse_emlid_csv(request.emlid_csv)
    return validate_as_built(request.design_points, as_built_points, request.tolerances)


def validate_piles_request(request: PileValidationRequest) -> dict:
    """Parses the PVCase BOM at `bom_path`, converts its "Piling information"
    rows to DesignPoints via lat/long, and validates against the Emlid CSV.
    Equipment (inverter/DC combiner/transformer) design points aren't part
    of this path -- those come from pvcase_dwg_scan, not the BOM."""
    bom = parse_pvcase_bom(request.bom_path)
    design_points = piles_to_design_points(bom.piles)
    as_built_points = parse_emlid_csv(request.emlid_csv)
    result = validate_as_built(design_points, as_built_points, request.tolerances)
    result["pile_count_in_bom"] = len(bom.piles)
    return result


def validate_as_built(
    design_points: list[DesignPoint],
    as_built_points: list[AsBuiltPoint],
    tolerances: GpsValidationTolerances,
) -> dict:
    design_by_tag = {_match_key(p.tag): p for p in design_points}
    matched_tags: set[str] = set()
    results = []
    rejected = []
    no_shared_crs = []

    for point in as_built_points:
        design = design_by_tag.get(_match_key(point.name))
        if design is None:
            continue
        matched_tags.add(_match_key(point.name))

        if point.solution_status not in ACCEPTABLE_SOLUTION_STATUSES:
            rejected.append({
                "name": point.name,
                "reason": f"solution status {point.solution_status or 'UNKNOWN'} is not RTK-fixed -- re-survey",
            })
            continue
        if point.lateral_rms_ft is not None and point.lateral_rms_ft > tolerances.max_lateral_rms_ft:
            rejected.append({
                "name": point.name,
                "reason": (
                    f"lateral RMS {point.lateral_rms_ft:.3f} ft exceeds "
                    f"{tolerances.max_lateral_rms_ft:.3f} ft -- re-survey"
                ),
            })
            continue

        lateral_offset_ft = _lateral_offset_ft(design, point)
        if lateral_offset_ft is None:
            no_shared_crs.append({
                "name": point.name,
                "reason": (
                    f"no shared coordinate system with design point {design.tag!r} "
                    "(need easting/northing or lat/long on both sides)"
                ),
            })
            continue
        elevation_offset_ft = (
            None
            if point.elevation_ft is None or design.elevation_ft is None
            else point.elevation_ft - design.elevation_ft
        )
        passes = lateral_offset_ft <= tolerances.lateral_tolerance_ft and (
            elevation_offset_ft is None or abs(elevation_offset_ft) <= tolerances.elevation_tolerance_ft
        )

        results.append({
            "tag": design.tag,
            "name": point.name,
            "lateral_offset_ft": round(lateral_offset_ft, 3),
            "elevation_offset_ft": None if elevation_offset_ft is None else round(elevation_offset_ft, 3),
            "passes": passes,
        })

    unmatched_design = [p.tag for p in design_points if _match_key(p.tag) not in matched_tags]
    unmatched_as_built = [p.name for p in as_built_points if _match_key(p.name) not in design_by_tag]

    return {
        "results": results,
        "rejected": rejected,
        "no_shared_crs": no_shared_crs,
        "unmatched_design": unmatched_design,
        "unmatched_as_built": unmatched_as_built,
        "summary": {
            "total_design_points": len(design_points),
            "matched": len(results),
            "passed": sum(1 for r in results if r["passes"]),
            "failed": sum(1 for r in results if not r["passes"]),
            "rejected_low_quality": len(rejected),
            "no_shared_crs": len(no_shared_crs),
            "unmatched_design": len(unmatched_design),
            "unmatched_as_built": len(unmatched_as_built),
        },
    }
