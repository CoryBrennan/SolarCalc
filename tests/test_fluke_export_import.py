"""Builds a minimal real xlsm package in-memory (same spirit as
test_pvcase_bom_import.py's _build_xlsx, extended for multi-letter column
references and numeric cells -- the Fluke `Table` sheet is wide (39 real
columns) and genuinely sparse, unlike the BOM's narrow all-text sheets)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.fluke_export_import import FlukeImportError, parse_fluke_export

WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="{sheet_name}" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""

SHEET_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    {row_entries}
  </sheetData>
</worksheet>"""


def _col_letters(index: int) -> str:
    """0 -> "A", 25 -> "Z", 26 -> "AA" ... inverse of the parser's own
    _col_letters_to_index(), used here only to build test fixtures."""
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _build_table_xlsm(path: Path, row_number: int, sparse_row: dict[int, object]) -> None:
    """Writes one data row at `row_number` with values at the given column
    indices (0-indexed) -- any index not present is omitted from the row's
    XML entirely, matching how Excel encodes a genuinely blank cell (not
    present-but-empty), which is exactly the case this parser has to
    handle correctly (see module docstring)."""
    # pvcase_bom_import._cell_text() only special-cases t=="s" (shared
    # string); anything else falls through to the cell's own raw <v> text
    # -- so a plain, untyped <c><v>text</v></c> works for text cells too,
    # no shared-strings table needed in this minimal fixture.
    cell_parts = []
    for col_idx, value in sparse_row.items():
        ref = f"{_col_letters(col_idx)}{row_number}"
        cell_parts.append(f'<c r="{ref}"><v>{value}</v></c>')
    row_xml = f'<row r="{row_number}">{"".join(cell_parts)}</row>'
    sheet_xml = SHEET_XML.format(row_entries=row_xml)

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", WORKBOOK_XML.format(sheet_name="Table"))
        zf.writestr("xl/_rels/workbook.xml.rels", RELS_XML)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


# Column indices, mirroring app/fluke_export_import.py's _COLUMN_FIELDS
# (index 0 is the unused "System" column A).
_SWITCHBOARD, _INVERTER, _COMBINER, _STRING_ID = 1, 2, 3, 4
_IRR = 5
_ISC_MEASURED, _ISC_MODELED, _ISC_DEV_MODELED = 6, 9, 10
_TEMP = 12
_VOC_MEASURED, _VOC_MODELED = 13, 16
_PF, _FF = 33, 34
_MODULE_MODEL = 38


def test_parses_real_shaped_table_row(tmp_path):
    path = tmp_path / "export.xlsm"
    _build_table_xlsm(path, row_number=13, sparse_row={
        _SWITCHBOARD: "SWBD1", _INVERTER: "Inv-1-1", _COMBINER: "DCC-1-1", _STRING_ID: "STR1",
        _IRR: 819.75, _ISC_MEASURED: 12.19, _ISC_MODELED: 11.86, _ISC_DEV_MODELED: 0.0281,
        _TEMP: 27.24, _VOC_MEASURED: 1271.46, _VOC_MODELED: 1271.46,
        _PF: 1.016, _FF: 0.7634, _MODULE_MODEL: "ZXM7-UHLDD144",
    })

    readings = parse_fluke_export(path)

    assert len(readings) == 1
    r = readings[0]
    assert (r.switchboard, r.inverter, r.combiner, r.string_id) == ("SWBD1", "Inv-1-1", "DCC-1-1", "STR1")
    assert r.isc_measured_a == 12.19
    # raw sheet fraction (0.0281) -> stored as a percent (2.81)
    assert r.isc_deviation_vs_modeled_pct == 2.81
    assert r.pf_pct == 101.6
    assert r.module_model == "ZXM7-UHLDD144"


def test_gap_column_does_not_misalign_later_fields(tmp_path):
    """The real bug risk this parser exists to avoid: a blank cell in the
    middle of the row (e.g. no `note`) is omitted from the row's XML
    entirely by Excel, not emitted empty -- a naive document-order reader
    would shift every field after the gap into the wrong slot."""
    path = tmp_path / "export.xlsm"
    _build_table_xlsm(path, row_number=13, sparse_row={
        _SWITCHBOARD: "SWBD1", _INVERTER: "Inv-1-1", _COMBINER: "DCC-1-1", _STRING_ID: "STR1",
        # deliberately skip several middle columns (irradiance, isc block, temp, voc block)
        _PF: 1.016,
        _MODULE_MODEL: "ZXM7-UHLDD144",
    })

    readings = parse_fluke_export(path)

    assert len(readings) == 1
    r = readings[0]
    assert r.irradiance_w_m2 is None
    assert r.isc_measured_a is None
    assert r.pf_pct == 101.6  # not shifted into isc_measured_a's slot
    assert r.module_model == "ZXM7-UHLDD144"  # not shifted into pf_pct's slot


def test_rows_before_data_start_row_are_ignored(tmp_path):
    path = tmp_path / "export.xlsm"
    _build_table_xlsm(path, row_number=10, sparse_row={_STRING_ID: "STR1", _SWITCHBOARD: "SWBD1", _INVERTER: "Inv-1-1", _COMBINER: "DCC-1-1"})

    readings = parse_fluke_export(path)

    assert readings == []


def test_missing_table_sheet_raises_fluke_import_error(tmp_path):
    path = tmp_path / "export.xlsm"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", WORKBOOK_XML.format(sheet_name="NotTable"))
        zf.writestr("xl/_rels/workbook.xml.rels", RELS_XML)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET_XML.format(row_entries=""))

    with pytest.raises(FlukeImportError, match="'Table' sheet not found"):
        parse_fluke_export(path)


def test_missing_file_raises_fluke_import_error(tmp_path):
    with pytest.raises(FlukeImportError, match="not found"):
        parse_fluke_export(tmp_path / "does_not_exist.xlsm")


def test_tested_string_tag_matches_pvcase_tag_shape():
    from app.fluke_export_import import FlukeReading

    reading = FlukeReading(switchboard="SWBD1", inverter="Inv-1-1", combiner="DCC-1-1", string_id="STR1", irradiance_w_m2=None, temp_c=None)
    assert reading.tested_string_tag() == "Inv-1-1.STR1"
