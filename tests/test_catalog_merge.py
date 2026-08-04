"""Merge logic per claude-code-integration-prompt.md: Source A/B priority,
sanity-range rejection, confidence flagging, document-type mismatch, and
partial-coverage merges preferring higher-confidence values."""

from __future__ import annotations

from app.catalog_merge import (
    PENDING_FLAG,
    CatalogMergeError,
    FieldValue,
    check_document_type_match,
    merge_catalog_fields,
)
from app.extraction_schema import ExtractedField, ExtractionAgentResponse, ModuleFields


def _agent_field(value, confidence="high", source="p.1") -> ExtractedField:
    return ExtractedField(value=value, confidence=confidence, source=source)


def test_source_a_takes_priority_over_source_b():
    source_a = {"voc_v": 49.0}
    source_b = {"voc_v": _agent_field(48.6, "high", "p.2")}

    result = merge_catalog_fields(["voc_v"], source_a, source_b)

    assert result["voc_v"].value == 49.0
    assert result["voc_v"].confidence == "high"
    assert result["voc_v"].source == "Source A (.PAN/.OND parser)"


def test_source_b_fills_field_source_a_does_not_cover():
    result = merge_catalog_fields(["voc_v"], {}, {"voc_v": _agent_field(48.6, "medium", "p.2")})

    assert result["voc_v"].value == 48.6
    assert result["voc_v"].confidence == "medium"
    assert result["voc_v"].flag is None  # medium confidence, no flag needed


def test_out_of_range_value_forced_to_low_and_flagged():
    # voc_v sanity range is 15-90V; 900 is a clear extraction error.
    result = merge_catalog_fields(["voc_v"], None, {"voc_v": _agent_field(900, "high", "p.2")})

    assert result["voc_v"].confidence == "low"
    assert result["voc_v"].flag == PENDING_FLAG
    assert "outside expected range" in result["voc_v"].conflict_note


def test_low_confidence_field_gets_pending_flag():
    result = merge_catalog_fields(["noct_c"], None, {"noct_c": _agent_field(45, "low", "p.3")})

    assert result["noct_c"].flag == PENDING_FLAG


def test_not_found_field_gets_pending_flag_and_null_value():
    result = merge_catalog_fields(["noct_c"], None, {"noct_c": _agent_field(None, "not_found", "")})

    assert result["noct_c"].value is None
    assert result["noct_c"].confidence == "not_found"
    assert result["noct_c"].flag == PENDING_FLAG


def test_pmax_cross_check_flags_deviation_from_vmp_times_imp():
    source_b = {
        "rated_power_w": _agent_field(700, "high", "p.2"),
        "vmp_v": _agent_field(40.5, "high", "p.2"),
        "imp_a": _agent_field(10.0, "high", "p.2"),  # 40.5 * 10 = 405, not 700
    }
    result = merge_catalog_fields(["rated_power_w", "vmp_v", "imp_a"], None, source_b)

    assert result["rated_power_w"].confidence == "low"
    assert result["rated_power_w"].flag == PENDING_FLAG
    assert "deviates" in result["rated_power_w"].conflict_note


def test_pmax_cross_check_passes_consistent_values():
    source_b = {
        "rated_power_w": _agent_field(700, "high", "p.2"),
        "vmp_v": _agent_field(40.5, "high", "p.2"),
        "imp_a": _agent_field(17.29, "high", "p.2"),  # ~700W
    }
    result = merge_catalog_fields(["rated_power_w", "vmp_v", "imp_a"], None, source_b)

    assert result["rated_power_w"].confidence == "high"
    assert result["rated_power_w"].flag is None


def test_document_type_mismatch_raises():
    response = ExtractionAgentResponse(document_type="inverter_datasheet", module_fields=None, inverter_fields=None)
    # inverter_fields is None even though document_type claims inverter — still a mismatch for a module entry
    try:
        check_document_type_match("module", response)
        assert False, "expected CatalogMergeError"
    except CatalogMergeError:
        pass


def test_document_type_match_module_ok():
    response = ExtractionAgentResponse(document_type="module_datasheet", module_fields=ModuleFields())
    check_document_type_match("module", response)  # should not raise


def test_partial_coverage_keeps_higher_confidence_existing_value():
    existing = {"noct_c": FieldValue(value=45.0, confidence="high", source="p.1, datasheet")}
    # Second document (e.g. an O&M manual) reports a lower-confidence value for the same field.
    new_source_b = {"noct_c": _agent_field(46.0, "low", "p.9, om manual")}

    result = merge_catalog_fields(["noct_c"], None, new_source_b, existing_draft_fields=existing)

    assert result["noct_c"].value == 45.0
    assert result["noct_c"].confidence == "high"


def test_partial_coverage_fills_gap_existing_draft_did_not_cover():
    existing = {"voc_v": FieldValue(value=48.6, confidence="high", source="p.1")}
    new_source_b = {"weight_kg": _agent_field(24.5, "medium", "p.3, om manual")}

    result = merge_catalog_fields(["voc_v", "weight_kg"], None, new_source_b, existing_draft_fields=existing)

    assert result["voc_v"].value == 48.6  # untouched, new doc didn't mention it
    assert result["weight_kg"].value == 24.5  # filled in by the second document
