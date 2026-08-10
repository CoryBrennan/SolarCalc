"""Builds a minimal real xlsx package in-memory (just the parts pvcase_bom_import
actually reads -- no Content_Types.xml/root .rels, since nothing but our own
parser opens these fixtures) so the parser is tested against real OOXML
structure rather than a hand-rolled stand-in format."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.pvcase_bom_import import PvcaseBomError, parse_pvcase_bom

WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    {sheet_entries}
  </sheets>
</workbook>"""

RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {rel_entries}
</Relationships>"""

SHARED_STRINGS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{count}" uniqueCount="{count}">
  {si_entries}
</sst>"""

SHEET_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    {row_entries}
  </sheetData>
</worksheet>"""


def _build_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> None:
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def sidx(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared)
            shared.append(value)
        return shared_index[value]

    sheet_xmls: dict[str, str] = {}
    for name, rows in sheets.items():
        row_xml_parts = []
        for r, row in enumerate(rows, start=1):
            cell_parts = []
            for c, value in enumerate(row):
                if value == "":
                    continue
                col_letter = chr(ord("A") + c)
                cell_parts.append(f'<c r="{col_letter}{r}" t="s"><v>{sidx(str(value))}</v></c>')
            row_xml_parts.append(f'<row r="{r}">{"".join(cell_parts)}</row>')
        sheet_xmls[name] = SHEET_XML.format(row_entries="".join(row_xml_parts))

    sheet_entries = []
    rel_entries = []
    for i, name in enumerate(sheets, start=1):
        rid = f"rId{i}"
        sheet_entries.append(f'<sheet name="{name}" sheetId="{i}" r:id="{rid}"/>')
        rel_entries.append(
            f'<Relationship Id="{rid}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
        )

    si_entries = "".join(f"<si><t>{s}</t></si>" for s in shared)

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", WORKBOOK_XML.format(sheet_entries="".join(sheet_entries)))
        zf.writestr("xl/_rels/workbook.xml.rels", RELS_XML.format(rel_entries="".join(rel_entries)))
        zf.writestr("xl/sharedStrings.xml", SHARED_STRINGS_XML.format(count=len(shared), si_entries=si_entries))
        for i, name in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xmls[name])


def _minimal_sheets(**overrides) -> dict[str, list[list[str]]]:
    sheets = {
        "Project Overview": [
            ["Project name: Test Site.dwg", "Project name: Test Site.dwg"],
            [],
            ["General information", "General information"],
            ["Total capacity, kWp", "1000.5"],
            ["Module quantity", "2000"],
            ["String inverter quantity", "2"],
        ],
        "Transformer To Inverter": [
            ["From", "To", "Cable length, in"],
            ["Transformer XFMR-1", "Inverter INV-1-1", "120.0"],
            ["Inverter INV-1-2", "132.0"],
        ],
        "Inverter To DC Combiner": [
            ["From", "To", "Cable length, in"],
            ["Inverter INV-1-1", "DC combiner DCC-1-1", "60.0"],
            ["Inverter INV-1-2", "DC combiner DCC-1-2", "72.0"],
        ],
        "DC Combiner To String": [
            ["From", "To", "Cable length +, in", "Cable length -, in", "Extension, in", "Table #"],
            ["DC combiner DCC-1-1", "String INV-1-1.STR1", "300.0", "280.0", "24.0", "1"],
            ["String INV-1-1.STR2", "310.0", "290.0", "24.0", "1"],
        ],
    }
    sheets.update(overrides)
    return sheets


def test_parses_real_shaped_export(tmp_path):
    path = tmp_path / "bom.xlsx"
    _build_xlsx(path, _minimal_sheets())

    data = parse_pvcase_bom(path)

    assert data.project_name == "Test Site.dwg"
    assert data.overview["Total capacity, kWp"] == 1000.5
    assert data.overview["Module quantity"] == 2000
    assert "General information" not in data.overview  # section-header artifact filtered out

    assert len(data.transformer_to_inverter) == 2
    assert data.transformer_to_inverter[0] == pytest_approx_segment("XFMR-1", "INV-1-1", 10.0)
    assert data.transformer_to_inverter[1] == pytest_approx_segment("XFMR-1", "INV-1-2", 11.0)


def test_carried_forward_from_matches_merged_cell_export(tmp_path):
    """The second row in each circuit sheet omits the leading From cell
    (PVCase's merged-cell export behavior) -- must reuse the prior From."""
    path = tmp_path / "bom.xlsx"
    _build_xlsx(path, _minimal_sheets())
    data = parse_pvcase_bom(path)
    assert data.inverter_to_combiner[1].from_tag == "INV-1-2"
    assert data.inverter_to_combiner[1].to_tag == "DCC-1-2"


def test_plus_minus_length_columns_dont_collide(tmp_path):
    path = tmp_path / "bom.xlsx"
    _build_xlsx(path, _minimal_sheets())
    data = parse_pvcase_bom(path)
    seg = data.combiner_to_string[0]
    assert seg.length_ft == pytest.approx(25.0)  # "+"" length is the primary length
    assert seg.extra_ft["cable_length_minus_in"] == pytest.approx(280.0 / 12)
    assert seg.extra_ft["extension_in"] == pytest.approx(2.0)


def test_all_tags_excludes_string_suffix(tmp_path):
    path = tmp_path / "bom.xlsx"
    _build_xlsx(path, _minimal_sheets())
    data = parse_pvcase_bom(path)
    tags = data.all_tags()
    assert "INV-1-1.STR1" not in tags
    assert "INV-1-1" in tags
    assert "DCC-1-1" in tags
    assert "XFMR-1" in tags


def test_parses_piling_information_sheet(tmp_path):
    """Rows copied verbatim from a real PVCase BOM export (SIE Torch Cay,
    90% Design Drawings, 2026-06-26) -- "PV area #1 (Existing grade)"
    section title, then the Frame/Pile/X/Y/Lat/Long header, then two piles
    of frame 1 ("1A", "1B")."""
    path = tmp_path / "bom.xlsx"
    sheets = _minimal_sheets(**{
        "Piling information": [
            ["PV area #1 (Existing grade)"],
            ["Frame", "Preset type", "Pile", "X", "Y", "Lat", "Long", "Z frame attach",
             "Z terrain enter", "Pile reveal length, in", "Frame slope (S/W +, N/E -), deg", "Frame fault"],
            ["1", "Polar 2Px14 Jinko 650W", "1A", "-34316.16", "-38066.825",
             "23.392813429123287", "-75.476268883581398", "142.06807", "72.29679", "69.77128", "0.34", "None"],
            ["1", "Polar 2Px14 Jinko 650W", "1B", "-34222.86", "-38066.923",
             "23.392813402189955", "-75.476245692250799", "158.51908", "72.99817", "85.52091", "0.34", "None"],
        ],
    })
    _build_xlsx(path, sheets)

    data = parse_pvcase_bom(path)

    assert len(data.piles) == 2
    p1a = data.piles[0]
    assert p1a.frame == "1"
    assert p1a.pile == "1A"
    assert p1a.tag == "1-1A"
    assert p1a.latitude == pytest.approx(23.392813429123287)
    assert p1a.longitude == pytest.approx(-75.476268883581398)
    assert p1a.z_terrain_ft == pytest.approx(72.29679)


def test_missing_piling_sheet_degrades_to_empty_pile_list(tmp_path):
    path = tmp_path / "bom.xlsx"
    _build_xlsx(path, _minimal_sheets())  # no "Piling information" sheet

    data = parse_pvcase_bom(path)

    assert data.piles == []


def test_missing_required_sheet_raises(tmp_path):
    path = tmp_path / "bom.xlsx"
    sheets = _minimal_sheets()
    del sheets["DC Combiner To String"]
    _build_xlsx(path, sheets)
    with pytest.raises(PvcaseBomError, match="missing expected sheet"):
        parse_pvcase_bom(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(PvcaseBomError, match="not found"):
        parse_pvcase_bom(tmp_path / "nope.xlsx")


def pytest_approx_segment(from_tag: str, to_tag: str, length_ft: float):
    from app.pvcase_bom_import import CableSegment

    return CableSegment(from_tag=from_tag, to_tag=to_tag, length_ft=pytest.approx(length_ft), extra_ft={})
