"""Validates a parsed Solmetric PVA field export (app/fluke_export_import.py)
against expected values, following the same
plan-vs-BOM comparison spirit as app/pvcase_validate.py.

Three independent checks, each meaningful on its own:

1. `validate_readings()` -- per-string, per-parameter pass/fail. Prefers
   the vendor's own Modeled/Deviation columns (Solmetric already computes
   the STC-translation pass/fail signal per string) over recomputing a
   translation from this app's catalog, falling back to the latter only
   when a reading has no vendor Modeled value for that parameter.
2. `check_design_intent_divergence()` -- diffs the vendor's "Modeled"
   value against what THIS APP's catalog module would predict at the same
   test-day conditions. Not a field-test failure signal -- a real
   divergence means whoever set up the Solmetric project entered
   different module data than what was actually designed, which
   `validate_readings()` alone can't see (it only compares a field
   reading against whatever the Solmetric project itself assumes).
3. `check_coverage()` -- cross-references which (inverter, combiner,
   string) tags actually got tested against the full expected list from a
   parsed PVCase BOM, to catch untested/missed strings -- something the
   field export alone can never reveal, since it only contains what WAS
   tested.

Known limitation carried forward from verification against a real
432-string export (solar_calc_engine/iv_curve_calc.py, 2026-08-10): this
app's Voc/Vmp translation disagrees with Solmetric's own "Modeled" Voc/Vmp
by ~4-5% even with the *correct* module entered -- Solmetric's project
settings report `TemperatureSource: Blended`, so its internal cell-
temperature estimate for that translation isn't simply the reading's own
Temp (C) column this app reads, and the actual blend isn't documented or
reverse-engineered here. `check_design_intent_divergence()` therefore uses
a much looser default tolerance for Voc/Vmp (8%) than Isc/Imp (3%, which
agreed to well under 1% in verification) -- treat Voc/Vmp divergence as a
rough secondary signal, not a precise one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from app import module_catalog
from app.fluke_export_import import FlukeReading
from app.iv_curve_calc import expected_iv_point
from app.models import ModuleSpec, ProjectInput
from app.pvcase_bom_import import PvcaseBomData
from app.pvcase_validate import TagSetDiff


class FlukeValidateRequest(BaseModel):
    """Path-based, same reasoning as PvcaseValidateRequest -- the field
    export and BOM live in the engineer's Dropbox-synced project folder on
    this same machine."""

    project: ProjectInput
    export_path: str
    bom_path: str | None = None  # optional -- coverage check skipped without it
    tolerance_pct: float = 5.0


@dataclass
class IVValidationResult:
    string_id: str
    parameter: str
    expected: float
    measured: float
    deviation_pct: float
    passes: bool
    source: str  # "vendor_modeled" | "app_translation"


def validate_readings(
    readings: list[FlukeReading],
    tolerance_pct: float = 5.0,
    module_sku: str | None = None,
    modules_per_string: int | None = None,
    module_fallback: ModuleSpec | dict[str, ModuleSpec] | None = None,
) -> list[IVValidationResult]:
    """`module_sku`/`modules_per_string` are only needed as a fallback for
    parameters with no vendor Modeled value -- if every reading carries
    Modeled values (true of a real Solmetric export), they're unused."""
    results: list[IVValidationResult] = []
    for reading in readings:
        results.extend(_validate_one(reading, tolerance_pct, module_sku, modules_per_string, module_fallback))
    return results


def _validate_one(
    reading: FlukeReading,
    tolerance_pct: float,
    module_sku: str | None,
    modules_per_string: int | None,
    module_fallback: ModuleSpec | dict[str, ModuleSpec] | None = None,
) -> list[IVValidationResult]:
    checks = [
        ("isc_a", reading.isc_measured_a, reading.isc_modeled_a, reading.isc_deviation_vs_modeled_pct),
        ("voc_v", reading.voc_measured_v, reading.voc_modeled_v, reading.voc_deviation_vs_modeled_pct),
        ("imp_a", reading.imp_measured_a, reading.imp_modeled_a, reading.imp_deviation_vs_modeled_pct),
        ("vmp_v", reading.vmp_measured_v, reading.vmp_modeled_v, reading.vmp_deviation_vs_modeled_pct),
    ]
    fallback = None
    if (
        module_sku is not None and modules_per_string is not None
        and reading.irradiance_w_m2 is not None and reading.temp_c is not None
    ):
        fallback = expected_iv_point(module_sku, reading.irradiance_w_m2, reading.temp_c, modules_per_string, module_fallback)

    results: list[IVValidationResult] = []
    for name, measured, modeled, vendor_deviation_pct in checks:
        if measured is None:
            continue
        if modeled is not None:
            expected_value = modeled
            deviation_pct = (
                abs(vendor_deviation_pct) if vendor_deviation_pct is not None
                else round(abs(measured - modeled) / modeled * 100.0, 2) if modeled else 0.0
            )
            source = "vendor_modeled"
        elif fallback is not None:
            expected_value = fallback[name.split("_")[0]]
            deviation_pct = round(abs(measured - expected_value) / expected_value * 100.0, 2) if expected_value else 0.0
            source = "app_translation"
        else:
            continue
        results.append(IVValidationResult(
            string_id=reading.string_id, parameter=name, expected=expected_value,
            measured=measured, deviation_pct=deviation_pct,
            passes=deviation_pct <= tolerance_pct, source=source,
        ))
    return results


@dataclass
class DesignIntentDivergence:
    string_id: str
    parameter: str
    vendor_modeled: float
    design_intent_expected: float
    deviation_pct: float
    flagged: bool


def check_design_intent_divergence(
    readings: list[FlukeReading],
    module_sku: str,
    modules_per_string: int,
    current_tolerance_pct: float = 3.0,
    voltage_tolerance_pct: float = 8.0,
    module_fallback: ModuleSpec | dict[str, ModuleSpec] | None = None,
) -> list[DesignIntentDivergence]:
    try:
        module_catalog.resolve_module_spec(module_sku, module_fallback)
    except KeyError:
        raise ValueError(f"Unknown module_sku {module_sku!r} -- not in module_catalog.MODULE_SKUS and no valid electricals supplied inline")

    results: list[DesignIntentDivergence] = []
    for reading in readings:
        if reading.irradiance_w_m2 is None or reading.temp_c is None:
            continue
        design_point = expected_iv_point(module_sku, reading.irradiance_w_m2, reading.temp_c, modules_per_string, module_fallback)
        checks = [
            ("isc_a", reading.isc_modeled_a, design_point["isc"], current_tolerance_pct),
            ("voc_v", reading.voc_modeled_v, design_point["voc"], voltage_tolerance_pct),
            ("imp_a", reading.imp_modeled_a, design_point["imp"], current_tolerance_pct),
            ("vmp_v", reading.vmp_modeled_v, design_point["vmp"], voltage_tolerance_pct),
        ]
        for name, vendor_modeled, design_expected, tolerance_pct in checks:
            if vendor_modeled is None:
                continue
            deviation_pct = round(abs(vendor_modeled - design_expected) / design_expected * 100.0, 2) if design_expected else 0.0
            results.append(DesignIntentDivergence(
                string_id=reading.string_id, parameter=name, vendor_modeled=vendor_modeled,
                design_intent_expected=design_expected, deviation_pct=deviation_pct,
                flagged=deviation_pct > tolerance_pct,
            ))
    return results


def _diff_case_insensitive(expected: set[str], actual: set[str]) -> TagSetDiff:
    """Like pvcase_validate._diff(), but case-insensitive -- a BOM tag
    ("INV-1-1.STR1") and a Solmetric export tag ("Inv-1-1.STR1") differ
    only in case in every real sample checked. Reported values keep their
    original source casing (BOM casing for expected_only, export casing
    for actual_only) rather than a normalized form, so the output still
    reads as "what that source actually calls it"."""
    actual_lower = {a.lower() for a in actual}
    expected_lower_map = {e.lower(): e for e in expected}
    return TagSetDiff(
        expected_only=sorted(e for key, e in expected_lower_map.items() if key not in actual_lower),
        actual_only=sorted(a for a in actual if a.lower() not in expected_lower_map),
        matched_count=len(set(expected_lower_map) & actual_lower),
    )


def check_coverage(readings: list[FlukeReading], bom: PvcaseBomData) -> TagSetDiff:
    """(inverter, combiner, string) tags actually present in a parsed field
    export vs. the full expected list from `bom.combiner_to_string` --
    catches untested/missed strings the export alone can never reveal."""
    expected = {seg.to_tag for seg in bom.combiner_to_string}
    tested = {r.tested_string_tag() for r in readings}
    return _diff_case_insensitive(expected, tested)


@dataclass
class FlukeValidationReport:
    reading_count: int
    validation: list[IVValidationResult] = field(default_factory=list)
    design_intent_divergence: list[DesignIntentDivergence] = field(default_factory=list)
    coverage: TagSetDiff | None = None
    warnings: list[str] = field(default_factory=list)

    def all_pass(self) -> bool:
        return all(r.passes for r in self.validation)

    def coverage_complete(self) -> bool:
        return self.coverage is None or not self.coverage.expected_only


def build_validation_report(
    readings: list[FlukeReading],
    tolerance_pct: float = 5.0,
    module_sku: str | None = None,
    modules_per_string: int | None = None,
    bom: PvcaseBomData | None = None,
    module_fallback: ModuleSpec | dict[str, ModuleSpec] | None = None,
) -> FlukeValidationReport:
    warnings: list[str] = []
    if not readings:
        warnings.append("No readings parsed from the export -- check it's the right file/sheet.")

    divergence: list[DesignIntentDivergence] = []
    if module_sku is not None and modules_per_string is not None:
        divergence = check_design_intent_divergence(readings, module_sku, modules_per_string, module_fallback=module_fallback)

    coverage = check_coverage(readings, bom) if bom is not None else None
    if bom is None:
        warnings.append("No BOM supplied -- coverage (untested/missed strings) can't be checked.")

    return FlukeValidationReport(
        reading_count=len(readings),
        validation=validate_readings(readings, tolerance_pct, module_sku, modules_per_string, module_fallback),
        design_intent_divergence=divergence,
        coverage=coverage,
        warnings=warnings,
    )
