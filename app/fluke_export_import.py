"""Parser for a real Solmetric PVA field IV-curve export (the `.xlsm` a
Fluke/Solmetric tracer produces after a commissioning test session). Pure
stdlib (zipfile + xml.etree), matching `app/pvcase_bom_import.py` -- reuses
its shared-strings/workbook-rels/cell-value helpers, since those are
correct regardless of sheet shape.

Confirmed against a real 432-string export (Fluke Solmetric_IV Curve_
Encore Brighton 1 T1_20260713.xlsm, verified in solar_calc_engine/
iv_curve_calc.py on 2026-08-10, cross-checked with zero missing/extra
against the matching PVCase BOM's 432-string coverage list). That export
is NOT a flat CSV -- it's a 40+-sheet macro-driven workbook (localized
user guides, license text, a Home/Report template, one raw-curve-trace
sheet per combiner). The one sheet that matters here is `Table`: one flat
row per string (the current/latest measurement, not a full retest
history), data starting at row 13 -- rows 1-8 are per-column QA statistics
(limit/mean/std-dev), row 9 is blank, rows 10-12 are a 3-level merged
header. Column layout (0-indexed):

    A-E   System, Switchboard, Inverter, Combiner, String
    F     Irradiance (W/m^2)
    G-L   Isc:  Measured, Combiner Avg, Deviation-vs-avg, Modeled,
                Deviation-vs-modeled, Translated-to-STC
    M     Temp (C)
    N-S   Voc:  same 6-column pattern
    T-Y   Imp:  same 6-column pattern
    Z-AE  Vmp:  same 6-column pattern
    AF-AG Pmax: Measured, Translated-to-STC
    AH    PF -- measured/modeled Pmax ratio
    AI    FF -- fill factor
    AJ-AN Date, Time, Note, Module (model name, per-row), SortBy

The vendor tool already computes "Modeled" (expected-at-test-day-
conditions) and "Deviation" for every parameter -- app/fluke_validate.py
prefers those over recomputing a translation from this app's own catalog.

Unlike pvcase_bom_import.py's narrow From/To/length sheets, this sheet is
wide and genuinely sparse (a `note` cell, or a deviation column on a string
with no vendor Modeled value, is often just absent from the row's XML
entirely, not present-but-empty) -- so cells here are placed by their real
`r="G13"`-style spreadsheet reference, not by raw document order, unlike
`pvcase_bom_import._read_sheet_rows()`. Reusing that function positionally
would silently misalign every column after the first gap.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from app.pvcase_bom_import import _NS, _read_shared_strings, _read_workbook_sheets, _cell_text

_TABLE_SHEET_NAME = "Table"
_TABLE_DATA_START_ROW = 13
_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


class FlukeImportError(ValueError):
    pass


@dataclass
class FlukeReading:
    """One row from the real Solmetric `Table` export sheet -- see module
    docstring for the verified column map. Every "Deviation" field is a
    signed percent (the sheet's raw fraction x100), matching the "(%)"
    units the sheet itself uses for `pf_pct`."""

    switchboard: str
    inverter: str
    combiner: str
    string_id: str
    irradiance_w_m2: float | None
    temp_c: float | None

    isc_measured_a: float | None = None
    isc_combiner_avg_a: float | None = None
    isc_deviation_vs_avg_pct: float | None = None
    isc_modeled_a: float | None = None
    isc_deviation_vs_modeled_pct: float | None = None
    isc_translated_stc_a: float | None = None

    voc_measured_v: float | None = None
    voc_combiner_avg_v: float | None = None
    voc_deviation_vs_avg_pct: float | None = None
    voc_modeled_v: float | None = None
    voc_deviation_vs_modeled_pct: float | None = None
    voc_translated_stc_v: float | None = None

    imp_measured_a: float | None = None
    imp_combiner_avg_a: float | None = None
    imp_deviation_vs_avg_pct: float | None = None
    imp_modeled_a: float | None = None
    imp_deviation_vs_modeled_pct: float | None = None
    imp_translated_stc_a: float | None = None

    vmp_measured_v: float | None = None
    vmp_combiner_avg_v: float | None = None
    vmp_deviation_vs_avg_pct: float | None = None
    vmp_modeled_v: float | None = None
    vmp_deviation_vs_modeled_pct: float | None = None
    vmp_translated_stc_v: float | None = None

    pmax_measured_w: float | None = None
    pmax_translated_stc_w: float | None = None
    pf_pct: float | None = None  # measured/modeled Pmax ratio
    ff: float | None = None      # fill factor, dimensionless

    date: str | None = None  # raw Excel date serial as text (e.g. "46196") -- not reformatted, unused by validation logic
    time: str | None = None
    note: str | None = None
    module_model: str | None = None
    sort_by: float | None = None

    def tested_string_tag(self) -> str:
        """(inverter, combiner, string) identity, PVCase-tag-shaped, for
        app/fluke_validate.py's coverage check against a parsed BOM."""
        return f"{self.inverter}.{self.string_id}"


_COLUMN_FIELDS = [
    "_system",  # column A -- literal unused label ("System"), not real data
    "switchboard", "inverter", "combiner", "string_id",
    "irradiance_w_m2",
    "isc_measured_a", "isc_combiner_avg_a", "isc_deviation_vs_avg_pct",
    "isc_modeled_a", "isc_deviation_vs_modeled_pct", "isc_translated_stc_a",
    "temp_c",
    "voc_measured_v", "voc_combiner_avg_v", "voc_deviation_vs_avg_pct",
    "voc_modeled_v", "voc_deviation_vs_modeled_pct", "voc_translated_stc_v",
    "imp_measured_a", "imp_combiner_avg_a", "imp_deviation_vs_avg_pct",
    "imp_modeled_a", "imp_deviation_vs_modeled_pct", "imp_translated_stc_a",
    "vmp_measured_v", "vmp_combiner_avg_v", "vmp_deviation_vs_avg_pct",
    "vmp_modeled_v", "vmp_deviation_vs_modeled_pct", "vmp_translated_stc_v",
    "pmax_measured_w", "pmax_translated_stc_w",
    "pf_pct", "ff",
    "date", "time", "note", "module_model", "sort_by",
]
_STRING_ID_COL = _COLUMN_FIELDS.index("string_id")
_PCT_FIELDS = {
    "isc_deviation_vs_avg_pct", "isc_deviation_vs_modeled_pct",
    "voc_deviation_vs_avg_pct", "voc_deviation_vs_modeled_pct",
    "imp_deviation_vs_avg_pct", "imp_deviation_vs_modeled_pct",
    "vmp_deviation_vs_avg_pct", "vmp_deviation_vs_modeled_pct",
    "pf_pct",
}
_TEXT_FIELDS = {"switchboard", "inverter", "combiner", "string_id", "date", "time", "note", "module_model"}


def _col_letters_to_index(letters: str) -> int:
    """"A" -> 0, "Z" -> 25, "AA" -> 26, ... -- standard base-26 (no zero
    digit) spreadsheet column decoding."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _read_table_rows_by_reference(zf: zipfile.ZipFile, part_path: str, shared: list[str]) -> list[tuple[int, list[str]]]:
    """Like pvcase_bom_import._read_sheet_rows(), but places each cell at
    its real column index (from the `r="G13"` cell reference) instead of
    document order, AND returns each row's real spreadsheet row number
    (from the `<row r="13">` element itself) instead of its position in
    the returned list. Both are needed for a wide, genuinely sparse sheet:
    Excel omits a blank cell's `<c>` from a row's XML entirely rather than
    emitting it empty, and likewise omits an entirely-blank row's `<row>`
    element entirely -- naive document-order indexing would silently
    misalign columns after a gap, or rows after a skipped one."""
    root = ET.fromstring(zf.read(part_path))
    rows: list[tuple[int, list[str]]] = []
    width = len(_COLUMN_FIELDS)
    for row_el in root.findall(".//m:sheetData/m:row", _NS):
        row_num = int(row_el.get("r", 0)) or (rows[-1][0] + 1 if rows else 1)
        cells: list[str] = [""] * width
        for c in row_el.findall("m:c", _NS):
            ref = c.get("r", "")
            m = _CELL_REF_RE.match(ref)
            if not m:
                continue
            col_idx = _col_letters_to_index(m.group(1))
            if col_idx < width:
                cells[col_idx] = _cell_text(c, shared)
        rows.append((row_num, cells))
    return rows


def parse_fluke_export(path: str | Path) -> list[FlukeReading]:
    path = Path(path)
    if not path.exists():
        raise FlukeImportError(f"Fluke export file not found: {path}")

    with zipfile.ZipFile(path) as zf:
        shared = _read_shared_strings(zf)
        sheet_parts = _read_workbook_sheets(zf)
        if _TABLE_SHEET_NAME not in sheet_parts:
            raise FlukeImportError(
                f"'{_TABLE_SHEET_NAME}' sheet not found -- got {list(sheet_parts)}. "
                "This parser targets Solmetric PVA's own export shape; a "
                "different tracer's export needs its own column mapping."
            )
        rows = _read_table_rows_by_reference(zf, sheet_parts[_TABLE_SHEET_NAME], shared)

    readings: list[FlukeReading] = []
    for row_num, row in rows:
        if row_num < _TABLE_DATA_START_ROW:
            continue
        if not row[_STRING_ID_COL]:
            continue  # not a data row (trailing blank row, etc.)

        values: dict[str, object] = {}
        for field_name, cell in zip(_COLUMN_FIELDS, row):
            if field_name == "_system":
                continue
            if cell == "":
                values[field_name] = None
                continue
            if field_name in _TEXT_FIELDS:
                values[field_name] = cell
                continue
            try:
                num = float(cell)
            except ValueError:
                values[field_name] = None
                continue
            values[field_name] = round(num * 100.0, 2) if field_name in _PCT_FIELDS else num
        readings.append(FlukeReading(**values))
    return readings
