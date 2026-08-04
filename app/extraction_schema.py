"""Pydantic mirror of datasheet-extraction-agent-prompt.md's output schema,
used to validate the extraction agent's JSON response before anything in
this app trusts it. Field names here match the prompt's schema exactly so a
change to one has an obvious counterpart in the other.

Also provides the flatten_* helpers that turn the schema's nested shape into
the flat dict[str, ExtractedField] that app/catalog_merge.py operates on —
one dict entry per canonical catalog field name.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low", "not_found"]

DocumentType = Literal["module_datasheet", "inverter_datasheet", "install_manual", "om_manual"]


class ExtractedField(BaseModel):
    value: object = None
    confidence: Confidence = "not_found"
    source: str = ""


class ExtractedDims(BaseModel):
    length: float | None = None
    width: float | None = None
    depth: float | None = None
    confidence: Confidence = "not_found"
    source: str = ""


class ModuleVariant(BaseModel):
    model_variant: str = ""
    rated_power_w: ExtractedField = Field(default_factory=ExtractedField)
    voc_v: ExtractedField = Field(default_factory=ExtractedField)
    isc_a: ExtractedField = Field(default_factory=ExtractedField)
    vmp_v: ExtractedField = Field(default_factory=ExtractedField)
    imp_a: ExtractedField = Field(default_factory=ExtractedField)
    module_efficiency_pct: ExtractedField = Field(default_factory=ExtractedField)


class ModuleFields(BaseModel):
    variants: list[ModuleVariant] = Field(default_factory=list)
    temp_coeff_voc_pct_per_c: ExtractedField = Field(default_factory=ExtractedField)
    temp_coeff_isc_pct_per_c: ExtractedField = Field(default_factory=ExtractedField)
    temp_coeff_pmax_pct_per_c: ExtractedField = Field(default_factory=ExtractedField)
    noct_c: ExtractedField = Field(default_factory=ExtractedField)
    max_system_voltage_v: ExtractedField = Field(default_factory=ExtractedField)
    max_series_fuse_rating_a: ExtractedField = Field(default_factory=ExtractedField)
    application_class: ExtractedField = Field(default_factory=ExtractedField)
    cell_type: ExtractedField = Field(default_factory=ExtractedField)
    number_of_cells: ExtractedField = Field(default_factory=ExtractedField)
    dimensions_mm: ExtractedDims = Field(default_factory=ExtractedDims)
    weight_kg: ExtractedField = Field(default_factory=ExtractedField)
    connector_type: ExtractedField = Field(default_factory=ExtractedField)
    frame_material: ExtractedField = Field(default_factory=ExtractedField)
    junction_box_ip_rating: ExtractedField = Field(default_factory=ExtractedField)
    certifications: ExtractedField = Field(default_factory=ExtractedField)


class TerminalTorqueSpec(BaseModel):
    connection_point: str = ""
    torque: str = ""
    unit: str = ""


class TerminalTorqueField(BaseModel):
    value: list[TerminalTorqueSpec] = Field(default_factory=list)
    confidence: Confidence = "not_found"
    source: str = ""


class AmbientTempRange(BaseModel):
    min: float | None = None
    max: float | None = None
    confidence: Confidence = "not_found"
    source: str = ""


class InverterFields(BaseModel):
    nameplate_ac_power_kw: ExtractedField = Field(default_factory=ExtractedField)
    dc_fuse_rating_per_string_a: ExtractedField = Field(default_factory=ExtractedField)
    dc_fuse_rating_combined_a: ExtractedField = Field(default_factory=ExtractedField)
    max_dc_string_count_per_mppt: ExtractedField = Field(default_factory=ExtractedField)
    max_dc_string_count_per_combiner: ExtractedField = Field(default_factory=ExtractedField)
    terminal_torque_specs: TerminalTorqueField = Field(default_factory=TerminalTorqueField)
    termination_temp_rating_c: ExtractedField = Field(default_factory=ExtractedField)
    dc_connector_type: ExtractedField = Field(default_factory=ExtractedField)
    ac_termination_type: ExtractedField = Field(default_factory=ExtractedField)
    communications_protocols: ExtractedField = Field(default_factory=ExtractedField)
    enclosure_ip_rating: ExtractedField = Field(default_factory=ExtractedField)
    ambient_temp_range_c: AmbientTempRange = Field(default_factory=AmbientTempRange)
    altitude_derating_notes: ExtractedField = Field(default_factory=ExtractedField)
    mounting_requirements: ExtractedField = Field(default_factory=ExtractedField)
    ground_fault_protection_type: ExtractedField = Field(default_factory=ExtractedField)
    warranty_years: ExtractedField = Field(default_factory=ExtractedField)


class ExtractionAgentResponse(BaseModel):
    document_type: DocumentType
    manufacturer: ExtractedField = Field(default_factory=ExtractedField)
    model: ExtractedField = Field(default_factory=ExtractedField)
    document_date_or_revision: ExtractedField = Field(default_factory=ExtractedField)
    module_fields: ModuleFields | None = None
    inverter_fields: InverterFields | None = None
    conflict_notes: list[str] = Field(default_factory=list)
    unparsed_sections_note: str = ""


def flatten_module_variant(variant: ModuleVariant, doc: ModuleFields) -> dict[str, ExtractedField]:
    """One flat field dict per variant — module datasheets list one row per
    power-class, and each becomes its own catalog record (mirrors the
    existing module_catalog.py convention of one dict entry per wattage
    bin). Document-level fields (temp coeffs, dimensions, certifications,
    etc.) are shared across every variant from the same document."""
    dims = doc.dimensions_mm
    return {
        "rated_power_w": variant.rated_power_w,
        "voc_v": variant.voc_v,
        "isc_a": variant.isc_a,
        "vmp_v": variant.vmp_v,
        "imp_a": variant.imp_a,
        "module_efficiency_pct": variant.module_efficiency_pct,
        "temp_coeff_voc_pct_per_c": doc.temp_coeff_voc_pct_per_c,
        "temp_coeff_isc_pct_per_c": doc.temp_coeff_isc_pct_per_c,
        "temp_coeff_pmax_pct_per_c": doc.temp_coeff_pmax_pct_per_c,
        "noct_c": doc.noct_c,
        "max_system_voltage_v": doc.max_system_voltage_v,
        "max_series_fuse_rating_a": doc.max_series_fuse_rating_a,
        "application_class": doc.application_class,
        "cell_type": doc.cell_type,
        "number_of_cells": doc.number_of_cells,
        "dimensions_length_mm": ExtractedField(value=dims.length, confidence=dims.confidence, source=dims.source),
        "dimensions_width_mm": ExtractedField(value=dims.width, confidence=dims.confidence, source=dims.source),
        "dimensions_depth_mm": ExtractedField(value=dims.depth, confidence=dims.confidence, source=dims.source),
        "weight_kg": doc.weight_kg,
        "connector_type": doc.connector_type,
        "frame_material": doc.frame_material,
        "junction_box_ip_rating": doc.junction_box_ip_rating,
        "certifications": doc.certifications,
    }


def flatten_inverter_fields(doc: InverterFields) -> dict[str, ExtractedField]:
    ambient = doc.ambient_temp_range_c
    return {
        "nameplate_ac_power_kw": doc.nameplate_ac_power_kw,
        "dc_fuse_rating_per_string_a": doc.dc_fuse_rating_per_string_a,
        "dc_fuse_rating_combined_a": doc.dc_fuse_rating_combined_a,
        "max_dc_string_count_per_mppt": doc.max_dc_string_count_per_mppt,
        "max_dc_string_count_per_combiner": doc.max_dc_string_count_per_combiner,
        "terminal_torque_specs": ExtractedField(
            value=[t.model_dump() for t in doc.terminal_torque_specs.value],
            confidence=doc.terminal_torque_specs.confidence,
            source=doc.terminal_torque_specs.source,
        ),
        "termination_temp_rating_c": doc.termination_temp_rating_c,
        "dc_connector_type": doc.dc_connector_type,
        "ac_termination_type": doc.ac_termination_type,
        "communications_protocols": doc.communications_protocols,
        "enclosure_ip_rating": doc.enclosure_ip_rating,
        "ambient_temp_range_min_c": ExtractedField(value=ambient.min, confidence=ambient.confidence, source=ambient.source),
        "ambient_temp_range_max_c": ExtractedField(value=ambient.max, confidence=ambient.confidence, source=ambient.source),
        "altitude_derating_notes": doc.altitude_derating_notes,
        "mounting_requirements": doc.mounting_requirements,
        "ground_fault_protection_type": doc.ground_fault_protection_type,
        "warranty_years": doc.warranty_years,
    }
