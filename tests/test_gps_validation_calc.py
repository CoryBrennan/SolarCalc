"""Fixture rows below are copied verbatim (same Easting/Northing/Elevation/
Solution status/Lateral RMS) from a real Emlid Flow 360 export for the
Ameren Peoria site -- Illinois West (ftUS) State Plane, NAD83(2011) +
NAVD88(GEOID18) height. `base point` is the RTK base station's own
autonomous fix (SINGLE, no correction) while every TCU point is RTK FIX."""

from __future__ import annotations

import pytest

from app.gps_validation_calc import (
    AsBuiltPoint,
    DesignPoint,
    GpsValidationTolerances,
    PileValidationRequest,
    parse_emlid_csv,
    piles_to_design_points,
    validate_as_built,
    validate_piles_request,
)
from app.pvcase_bom_import import PileRow
from tests.test_pvcase_bom_import import _build_xlsx, _minimal_sheets

EMLID_CSV_HEADER = (
    "Name,Code,Code description,Easting,Northing,Elevation,Description,Longitude,Latitude,"
    "Ellipsoidal height,Origin,Tilt angle,Easting RMS,Northing RMS,Elevation RMS,Lateral RMS,"
    "Antenna height,Antenna height units,Solution status,Correction type,Averaging start,"
    "Averaging end,Samples,PDOP,GDOP,Base easting,Base northing,Base elevation,Base longitude,"
    "Base latitude,Base ellipsoidal height,Baseline,Mount point,CS name,GPS Satellites,"
    "GLONASS Satellites,Galileo Satellites,BeiDou Satellites,QZSS Satellites,Device type,"
    "Device serial number,Author"
)

BASE_POINT_ROW = (
    "base point,,,2434825.516,1475311.472,571.803,,-89.66795146,40.71660662,463.588,Global,,"
    "0.407,0.439,0.863,0.599,6.812,ft,SINGLE,RTK,2026-05-20 10:09:54.4 UTC-05:00,"
    "2026-05-20 10:11:54.4 UTC-05:00,601,1.1,1.2,,,,,,,,,"
    "NAD83(2011) / Illinois West (ftUS) + NAVD88(GEOID18) height (ftUS),,,,,,Reach RS4,"
    "8243887558cfc833,monitoring@azimuth.energy"
)

TCU1_ROW = (
    "TCU1,,,2434191.346,1475350.261,570.143,,-89.67023838,40.71672295,461.923,Global,1.7,"
    "0.039,0.038,0.038,0.054,6.812,ft,FIX,RTK,2026-05-20 10:27:09.4 UTC-05:00,"
    "2026-05-20 10:27:14.4 UTC-05:00,26,1.2,1.4,2434825.305,1475312.412,577.548,-89.66795220,"
    "40.71660920,469.332,634.972,,NAD83(2011) / Illinois West (ftUS) + NAVD88(GEOID18) height (ftUS),"
    "8,6,6,0,0,Reach RS4,824369cc34c044b2,monitoring@azimuth.energy"
)

TCU2_ROW = (
    "TCU2,,,2434209.425,1475350.554,570.712,,-89.67017316,40.71672347,462.492,Global,1.2,"
    "0.039,0.040,0.035,0.055,6.812,ft,FIX,RTK,2026-05-20 10:27:30.0 UTC-05:00,"
    "2026-05-20 10:27:35.0 UTC-05:00,26,1.2,1.4,2434825.305,1475312.412,577.548,-89.66795220,"
    "40.71660920,469.332,616.981,,NAD83(2011) / Illinois West (ftUS) + NAVD88(GEOID18) height (ftUS),"
    "8,6,6,0,0,Reach RS4,824369cc34c044b2,monitoring@azimuth.energy"
)


def test_parse_emlid_csv_reads_real_export_columns():
    csv_text = "\n".join([EMLID_CSV_HEADER, BASE_POINT_ROW, TCU1_ROW])

    points = parse_emlid_csv(csv_text)

    assert [p.name for p in points] == ["base point", "TCU1"]
    base_point, tcu1 = points
    assert base_point.solution_status == "SINGLE"
    assert base_point.easting_ft == 2434825.516
    assert tcu1.solution_status == "FIX"
    assert tcu1.northing_ft == 1475350.261
    assert tcu1.lateral_rms_ft == 0.054


def test_parse_emlid_csv_skips_blank_trailing_row():
    csv_text = "\n".join([EMLID_CSV_HEADER, TCU1_ROW, ""])

    points = parse_emlid_csv(csv_text)

    assert len(points) == 1


def test_validate_as_built_passes_point_within_tolerance():
    design = [DesignPoint(tag="TCU1", easting_ft=2434191.346, northing_ft=1475350.261, elevation_ft=570.143)]
    as_built = parse_emlid_csv("\n".join([EMLID_CSV_HEADER, TCU1_ROW]))

    result = validate_as_built(design, as_built, GpsValidationTolerances())

    assert result["summary"]["passed"] == 1
    assert result["results"][0]["lateral_offset_ft"] == 0.0
    assert result["results"][0]["passes"] is True


def test_validate_as_built_fails_point_out_of_tolerance():
    # Design pile 2 ft east of where TCU2 was actually staked -- outside the
    # default 0.5 ft lateral tolerance.
    design = [DesignPoint(tag="TCU2", easting_ft=2434211.425, northing_ft=1475350.554, elevation_ft=570.712)]
    as_built = parse_emlid_csv("\n".join([EMLID_CSV_HEADER, TCU2_ROW]))

    result = validate_as_built(design, as_built, GpsValidationTolerances())

    assert result["summary"]["failed"] == 1
    assert result["results"][0]["passes"] is False
    assert result["results"][0]["lateral_offset_ft"] > 1.9


def test_validate_as_built_rejects_non_fix_solution():
    design = [DesignPoint(tag="base point", easting_ft=2434825.516, northing_ft=1475311.472)]
    as_built = parse_emlid_csv("\n".join([EMLID_CSV_HEADER, BASE_POINT_ROW]))

    result = validate_as_built(design, as_built, GpsValidationTolerances())

    assert result["summary"]["matched"] == 0
    assert result["summary"]["rejected_low_quality"] == 1
    assert "SINGLE" in result["rejected"][0]["reason"]


def test_validate_as_built_rejects_point_with_high_lateral_rms():
    design = [DesignPoint(tag="TCU-RMS", easting_ft=100.0, northing_ft=200.0)]
    as_built = [
        AsBuiltPoint(
            name="TCU-RMS", easting_ft=100.0, northing_ft=200.0,
            solution_status="FIX", lateral_rms_ft=0.5,
        )
    ]

    result = validate_as_built(design, as_built, GpsValidationTolerances(max_lateral_rms_ft=0.15))

    assert result["summary"]["rejected_low_quality"] == 1
    assert "RMS" in result["rejected"][0]["reason"]


def test_validate_as_built_reports_unmatched_points_both_directions():
    design = [DesignPoint(tag="TCU99", easting_ft=0, northing_ft=0)]
    as_built = parse_emlid_csv("\n".join([EMLID_CSV_HEADER, TCU1_ROW]))

    result = validate_as_built(design, as_built, GpsValidationTolerances())

    assert result["unmatched_design"] == ["TCU99"]
    assert result["unmatched_as_built"] == ["TCU1"]


def test_validate_as_built_falls_back_to_lat_long_when_no_easting_northing():
    # Design point has only lat/long (e.g. from a PVCase pile row) --
    # TCU1's real lat/long, so the offset should be ~0.
    design = [DesignPoint(tag="TCU1", latitude_deg=40.71672295, longitude_deg=-89.67023838)]
    as_built = parse_emlid_csv("\n".join([EMLID_CSV_HEADER, TCU1_ROW]))

    result = validate_as_built(design, as_built, GpsValidationTolerances())

    assert result["summary"]["passed"] == 1
    assert result["results"][0]["lateral_offset_ft"] < 0.1


def test_validate_as_built_rejects_point_with_no_shared_coordinate_system():
    # Design point only has easting/northing; as-built row's Easting/Northing
    # columns are wiped so only lat/long survives -- no common system.
    design = [DesignPoint(tag="TCU1", easting_ft=2434191.346, northing_ft=1475350.261)]
    as_built = [
        AsBuiltPoint(
            name="TCU1", latitude_deg=40.71672295, longitude_deg=-89.67023838,
            solution_status="FIX",
        )
    ]

    result = validate_as_built(design, as_built, GpsValidationTolerances())

    assert result["summary"]["matched"] == 0
    assert result["summary"]["no_shared_crs"] == 1
    assert "no shared coordinate system" in result["no_shared_crs"][0]["reason"]


def test_piles_to_design_points_uses_lat_long_and_frame_pile_tag():
    piles = [
        PileRow(
            frame="1", pile="1A", preset_type="Polar 2Px14 Jinko 650W",
            x_ft=-34316.16, y_ft=-38066.825,
            latitude=23.392813429123287, longitude=-75.476268883581398,
            z_frame_attach_ft=142.06807, z_terrain_ft=72.29679,
        )
    ]

    design_points = piles_to_design_points(piles)

    assert len(design_points) == 1
    dp = design_points[0]
    assert dp.tag == "1-1A"
    assert dp.latitude_deg == pytest.approx(23.392813429123287)
    assert dp.longitude_deg == pytest.approx(-75.476268883581398)
    assert dp.elevation_ft == pytest.approx(72.29679)
    assert dp.easting_ft is None


def test_validate_piles_request_reads_bom_and_matches_by_frame_pile_tag(tmp_path):
    bom_path = tmp_path / "bom.xlsx"
    sheets = _minimal_sheets(**{
        "Piling information": [
            ["PV area #1 (Existing grade)"],
            ["Frame", "Preset type", "Pile", "X", "Y", "Lat", "Long", "Z frame attach",
             "Z terrain enter", "Pile reveal length, in", "Frame slope (S/W +, N/E -), deg", "Frame fault"],
            # Reuses TCU1's real lat/long so it lines up with an as-built
            # point tagged to match -- PVCase's own frame+pile tag ("1-1A")
            # wouldn't naturally match an Emlid point named "TCU1" in
            # practice; that naming reconciliation is a site-specific
            # convention, not something this test asserts.
            ["1", "Polar 2Px14 Jinko 650W", "1A", "-34316.16", "-38066.825",
             "40.71672295", "-89.67023838", "142.06807", "72.29679", "69.77128", "0.34", "None"],
        ],
    })
    _build_xlsx(bom_path, sheets)
    emlid_csv = "\n".join([EMLID_CSV_HEADER, TCU1_ROW.replace("TCU1", "1-1A", 1)])

    # Z terrain here is the Bahamas fixture's real value, unrelated to
    # TCU1's real Illinois elevation -- an artifact of splicing two
    # different real datasets together for this test, not something a real
    # BOM+survey pairing would exhibit. Widen elevation tolerance so the
    # test isolates the lateral (lat/long) match this test is actually about.
    tolerances = GpsValidationTolerances(elevation_tolerance_ft=1000)
    result = validate_piles_request(
        PileValidationRequest(bom_path=str(bom_path), emlid_csv=emlid_csv, tolerances=tolerances)
    )

    assert result["pile_count_in_bom"] == 1
    assert result["summary"]["passed"] == 1
