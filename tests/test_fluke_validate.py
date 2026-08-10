from __future__ import annotations

from app.fluke_export_import import FlukeReading
from app.fluke_validate import (
    build_validation_report,
    check_coverage,
    check_design_intent_divergence,
    validate_readings,
)
from app.iv_curve_calc import expected_iv_point
from app.pvcase_bom_import import CableSegment, PvcaseBomData


def _reading(**overrides):
    defaults = dict(
        switchboard="SWBD1", inverter="Inv-1-1", combiner="DCC-1-1", string_id="STR1",
        irradiance_w_m2=850.0, temp_c=42.0,
    )
    defaults.update(overrides)
    return FlukeReading(**defaults)


def test_validate_readings_prefers_vendor_modeled_when_present():
    reading = _reading(
        isc_measured_a=12.0, isc_modeled_a=11.8, isc_deviation_vs_modeled_pct=1.7,
        voc_measured_v=1270.0, voc_modeled_v=1265.0, voc_deviation_vs_modeled_pct=0.4,
    )
    results = validate_readings([reading], tolerance_pct=5.0)
    assert all(r.passes for r in results)
    assert all(r.source == "vendor_modeled" for r in results)


def test_validate_readings_fails_outside_tolerance():
    reading = _reading(voc_measured_v=200.0, voc_modeled_v=1270.0, voc_deviation_vs_modeled_pct=84.3)
    results = validate_readings([reading], tolerance_pct=5.0)
    voc_check = next(r for r in results if r.parameter == "voc_v")
    assert voc_check.passes is False


def test_validate_readings_falls_back_to_app_translation_without_vendor_modeled():
    expected = expected_iv_point(module_sku="720", irradiance_w_m2=850, cell_temp_c=42, modules_per_string=29)
    reading = _reading(
        isc_measured_a=expected["isc"], voc_measured_v=expected["voc"],
        imp_measured_a=expected["imp"], vmp_measured_v=expected["vmp"],
    )
    results = validate_readings([reading], module_sku="720", modules_per_string=29)
    assert len(results) == 4
    assert all(r.passes for r in results)
    assert all(r.source == "app_translation" for r in results)


def test_validate_readings_skips_parameter_with_no_vendor_value_and_no_fallback():
    reading = _reading(isc_measured_a=12.0)  # no isc_modeled_a, no module_sku given
    assert validate_readings([reading]) == []


def test_check_design_intent_divergence_flags_wrong_module_isc():
    reading = _reading(isc_modeled_a=6.0)  # vendor's own Modeled reflects a totally different module
    results = check_design_intent_divergence([reading], module_sku="ZXM7-UHLDD144", modules_per_string=26)
    isc_result = next(r for r in results if r.parameter == "isc_a")
    assert isc_result.flagged is True


def test_check_design_intent_divergence_passes_when_modeled_matches_design():
    design_point = expected_iv_point(module_sku="ZXM7-UHLDD144", irradiance_w_m2=850, cell_temp_c=27, modules_per_string=26)
    reading = _reading(temp_c=27.0, isc_modeled_a=design_point["isc"], voc_modeled_v=design_point["voc"])
    results = check_design_intent_divergence([reading], module_sku="ZXM7-UHLDD144", modules_per_string=26)
    assert all(not r.flagged for r in results)


def test_check_design_intent_divergence_unknown_sku_raises():
    reading = _reading(isc_modeled_a=6.0)
    try:
        check_design_intent_divergence([reading], module_sku="NOT-A-REAL-SKU", modules_per_string=26)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "NOT-A-REAL-SKU" in str(exc)


def test_check_coverage_flags_missing_and_extra_strings_case_insensitive():
    readings = [_reading(inverter="Inv-1-1", combiner="DCC-1-1", string_id="STR1")]
    bom = PvcaseBomData(
        project_name="Test", overview={},
        transformer_to_inverter=[], inverter_to_combiner=[],
        combiner_to_string=[
            CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR1", length_ft=10.0),  # tested (case differs)
            CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR2", length_ft=10.0),  # missing
        ],
    )
    diff = check_coverage(readings, bom)
    assert diff.matched_count == 1
    assert diff.expected_only == ["INV-1-1.STR2"]
    assert diff.actual_only == []


def test_build_validation_report_warns_when_no_bom_supplied():
    report = build_validation_report([_reading(isc_measured_a=1.0, isc_modeled_a=1.0, isc_deviation_vs_modeled_pct=0.0)])
    assert report.coverage is None
    assert report.coverage_complete() is True  # vacuously true -- nothing to compare against
    assert any("No BOM supplied" in w for w in report.warnings)


def test_build_validation_report_warns_when_no_readings():
    report = build_validation_report([])
    assert report.reading_count == 0
    assert any("No readings parsed" in w for w in report.warnings)
