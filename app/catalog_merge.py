"""Merge logic for the equipment catalog ingestion pipeline, per
claude-code-integration-prompt.md:

- Source A (.PAN/.OND parser output — not yet implemented anywhere in this
  codebase, see app/catalog_routes.py) takes priority over Source B (the
  extraction agent) for any field both cover.
- Sanity-range validation on numeric fields; failures are forced to "low"
  confidence and flagged rather than silently accepted.
- Fields left at "low"/"not_found" confidence (and not covered by a code
  default — this app has none for datasheet-sourced fields today) get the
  "manufacturer data pending — verify before issue" flag rather than
  blocking record creation.
- A document whose populated field section doesn't match the catalog
  entry's equipment_type is rejected outright, not merged.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.extraction_schema import ExtractedField, ExtractionAgentResponse

PENDING_FLAG = "manufacturer data pending — verify before issue"

# No datasheet-sourced field has a code default elsewhere in this app today
# (module_catalog.py's per-SKU values are themselves datasheet-derived, not
# fallbacks) — kept as an explicit set so a future default doesn't require
# touching the flagging logic below, just adding the field name here.
CODE_DEFAULT_FIELDS: frozenset[str] = frozenset()

_CONFIDENCE_RANK = {"not_found": 0, "low": 1, "medium": 2, "high": 3}

# (min, max) plausibility ranges for numeric fields, sourced from the same
# label/value bounds the HMI's JS regex parser already uses (findLabeledNumber
# calls in static/index.html) plus standard PV/inverter engineering ranges.
_SANITY_RANGES: dict[str, tuple[float, float]] = {
    "rated_power_w": (50, 1000),
    "voc_v": (15, 90),
    "isc_a": (1, 20),
    "vmp_v": (10, 75),
    "imp_a": (1, 20),
    "module_efficiency_pct": (5, 30),
    "temp_coeff_voc_pct_per_c": (-1.0, 0.0),
    "temp_coeff_isc_pct_per_c": (0.0, 1.0),
    "temp_coeff_pmax_pct_per_c": (-1.0, 0.0),
    "noct_c": (30, 60),
    "max_system_voltage_v": (600, 1500),
    "weight_kg": (1, 120),
    "nameplate_ac_power_kw": (1, 1000),
    "dc_fuse_rating_per_string_a": (1, 30),
    "dc_fuse_rating_combined_a": (1, 200),
    "termination_temp_rating_c": (40, 120),
    "warranty_years": (1, 30),
}

# Cross-field sanity check: Pmax should be close to Vmp * Imp.
_PMAX_TOLERANCE_PCT = 5.0

# Canonical field names per equipment type — matches the keys produced by
# extraction_schema.flatten_module_variant / flatten_inverter_fields.
MODULE_FIELD_NAMES: list[str] = [
    "rated_power_w",
    "voc_v",
    "isc_a",
    "vmp_v",
    "imp_a",
    "module_efficiency_pct",
    "temp_coeff_voc_pct_per_c",
    "temp_coeff_isc_pct_per_c",
    "temp_coeff_pmax_pct_per_c",
    "noct_c",
    "max_system_voltage_v",
    "max_series_fuse_rating_a",
    "application_class",
    "cell_type",
    "number_of_cells",
    "dimensions_length_mm",
    "dimensions_width_mm",
    "dimensions_depth_mm",
    "weight_kg",
    "connector_type",
    "frame_material",
    "junction_box_ip_rating",
    "certifications",
]

INVERTER_FIELD_NAMES: list[str] = [
    "nameplate_ac_power_kw",
    "dc_fuse_rating_per_string_a",
    "dc_fuse_rating_combined_a",
    "max_dc_string_count_per_mppt",
    "max_dc_string_count_per_combiner",
    "terminal_torque_specs",
    "termination_temp_rating_c",
    "dc_connector_type",
    "ac_termination_type",
    "communications_protocols",
    "enclosure_ip_rating",
    "ambient_temp_range_min_c",
    "ambient_temp_range_max_c",
    "altitude_derating_notes",
    "mounting_requirements",
    "ground_fault_protection_type",
    "warranty_years",
]


class CatalogMergeError(Exception):
    """Raised when the merge can't proceed at all — a document/equipment
    type mismatch. The caller surfaces this as a 422; no record is created."""


class FieldValue(BaseModel):
    value: object = None
    confidence: str = "not_found"
    source: str = ""
    flag: str | None = None
    conflict_note: str | None = None


def check_document_type_match(equipment_type: str, response: ExtractionAgentResponse) -> None:
    """Rejects the merge outright when the agent's populated field section
    doesn't match the catalog entry being ingested into (e.g. an inverter
    manual parsed against a module catalog record)."""
    if equipment_type == "module" and response.module_fields is None:
        raise CatalogMergeError(
            f"document_type {response.document_type!r} did not produce module_fields; "
            "expected a module datasheet/manual for this catalog entry"
        )
    if equipment_type == "inverter" and response.inverter_fields is None:
        raise CatalogMergeError(
            f"document_type {response.document_type!r} did not produce inverter_fields; "
            "expected an inverter datasheet/manual for this catalog entry"
        )


def _flag_for(field_name: str, confidence: str) -> str | None:
    if confidence in ("low", "not_found") and field_name not in CODE_DEFAULT_FIELDS:
        return PENDING_FLAG
    return None


def _merge_one_field(
    field_name: str,
    source_a: dict[str, float | str] | None,
    source_b: ExtractedField | None,
    existing: FieldValue | None,
) -> FieldValue:
    if source_a is not None and field_name in source_a:
        return FieldValue(value=source_a[field_name], confidence="high", source="Source A (.PAN/.OND parser)")

    b_value = source_b.value if source_b is not None else None
    b_confidence = source_b.confidence if source_b is not None else "not_found"
    b_has_value = source_b is not None and b_value is not None and b_confidence != "not_found"

    if b_has_value:
        confidence = b_confidence
        conflict_note = None
        sanity_range = _SANITY_RANGES.get(field_name)
        if sanity_range is not None and isinstance(b_value, (int, float)):
            low, high = sanity_range
            if not (low <= b_value <= high):
                confidence = "low"
                conflict_note = f"value {b_value} outside expected range [{low}, {high}]"
        candidate = FieldValue(
            value=b_value,
            confidence=confidence,
            source=source_b.source,
            flag=_flag_for(field_name, confidence),
            conflict_note=conflict_note,
        )
        if existing is not None and _CONFIDENCE_RANK[existing.confidence] > _CONFIDENCE_RANK[confidence]:
            kept = existing.model_copy()
            if existing.value != b_value:
                kept.conflict_note = f"newer document reported {b_value!r}, kept existing higher-confidence value"
            return kept
        return candidate

    if existing is not None:
        return existing

    return FieldValue(value=None, confidence="not_found", source="", flag=_flag_for(field_name, "not_found"))


def merge_catalog_fields(
    field_names: list[str],
    source_a: dict[str, float | str] | None,
    source_b: dict[str, ExtractedField],
    existing_draft_fields: dict[str, FieldValue] | None = None,
) -> dict[str, FieldValue]:
    """Builds one FieldValue per canonical field name. `existing_draft_fields`
    carries a pending draft's already-accepted fields forward when this
    document is the second one ingested for the same manufacturer/model
    (or seeds from the current active version — see catalog_routes)."""
    existing = existing_draft_fields or {}
    result = {
        field_name: _merge_one_field(field_name, source_a, source_b.get(field_name), existing.get(field_name))
        for field_name in field_names
    }
    _apply_pmax_cross_check(result)
    return result


def _apply_pmax_cross_check(fields: dict[str, FieldValue]) -> None:
    pmax, vmp, imp = fields.get("rated_power_w"), fields.get("vmp_v"), fields.get("imp_a")
    if not (pmax and vmp and imp):
        return
    if not all(isinstance(f.value, (int, float)) for f in (pmax, vmp, imp)):
        return
    expected = vmp.value * imp.value
    if expected <= 0:
        return
    deviation_pct = abs(pmax.value - expected) / expected * 100
    if deviation_pct > _PMAX_TOLERANCE_PCT:
        pmax.confidence = "low"
        pmax.flag = _flag_for("rated_power_w", "low")
        pmax.conflict_note = f"Pmax {pmax.value} deviates {deviation_pct:.1f}% from Vmp*Imp ({expected:.1f})"
