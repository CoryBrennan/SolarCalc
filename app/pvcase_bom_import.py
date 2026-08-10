"""Parser for PVCase's BOM export (the "..._BOM_Data.xlsx" PVCase writes
alongside a CAD Release DWG). Pure stdlib (zipfile + xml.etree) -- an xlsx is
just zipped XML, so this needs no new dependency for what is, structurally, a
handful of flat tables.

Confirmed against a real export (Encore Brighton 2, WIP90 CHINT 2025-04-18):
six sheets -- Project Overview, Piling information, Pile grouping,
Transformer To Inverter, Inverter To DC Combiner, DC Combiner To String. The
three "From/To/length" sheets are exactly the DC/AC routing lengths this
codebase has hardcoded as placeholders since the original solar_calc_engine
prototype (run_reference_design.py's `pvcase_combiner_to_inverter_length_ft`
comment, voltage_drop_calc.py's docstring) -- this module is what finally
reads the real numbers instead of guessing them.

Known PVCase export limitation (not something this parser can work around):
lengths are module-connector-to-endpoint only, with no above-ground
cable-tray/hanger vs. underground-conduit breakdown, so they can't be fed
straight into ampacity derating as one uniform installation condition. See
memory/pvcase_integration_gaps.md.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}

# Longest-match-first so "DC combiner " doesn't get shadowed by a shorter prefix.
_LABEL_PREFIXES = ["DC combiner ", "Transformer ", "Inverter ", "String "]


class PvcaseBomError(ValueError):
    pass


@dataclass
class CableSegment:
    from_tag: str
    to_tag: str
    length_ft: float
    extra_ft: dict[str, float] = field(default_factory=dict)


@dataclass
class PileRow:
    """One row of the "Piling information" sheet -- racking/table piles
    only. PVCase does not track piles/pads for inverters, DC combiners, or
    transformers here (see memory/pvcase_integration_gaps.md); those
    coordinates have to come from the CAD Release DWG (app/pvcase_dwg_scan.py)
    instead. `frame` + `pile` is PVCase's own identity for a pile (e.g. frame
    "12", pile "A") -- whether that lines up with a field crew's own point
    naming (e.g. an Emlid Flow "TCU12") is a site-specific convention this
    parser has no way to know, so callers shouldn't assume `tag` matches
    as-built point names without confirming it first."""

    frame: str
    pile: str
    preset_type: str
    x_ft: float
    y_ft: float
    latitude: float
    longitude: float
    z_frame_attach_ft: float
    z_terrain_ft: float

    @property
    def tag(self) -> str:
        # "-" separator, not straight concatenation -- frame "11" pile "A"
        # and frame "1" pile "1A" would otherwise both read "11A".
        return f"{self.frame}-{self.pile}"


def parse_piling_information(rows: list[list[str]]) -> list[PileRow]:
    """Parses the "Piling information" sheet. Confirmed against a real export
    (SIE Torch Cay, 90% Design Drawings, 2026-06-26): a "PV area #N (...)"
    section-title row, then a "Frame, Preset type, Pile, X, Y, Lat, Long,
    Z frame attach, Z terrain enter, ..." header row, then one data row per
    pile. Multiple PV areas each repeat their own title + header pair.
    Columns are read by name (not position) so header reordering or an added
    column between PVCase versions doesn't silently misalign fields."""
    piles: list[PileRow] = []
    header_index: dict[str, int] | None = None
    for raw_row in rows:
        cells = _trim_trailing_empty(raw_row)
        if not cells:
            continue
        if cells[0].startswith("PV area"):
            header_index = None
            continue
        if cells[0] == "Frame":
            header_index = {name: i for i, name in enumerate(cells)}
            continue
        if header_index is None:
            continue

        def get(name: str) -> str:
            idx = header_index.get(name)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        try:
            piles.append(
                PileRow(
                    frame=get("Frame"),
                    pile=get("Pile"),
                    preset_type=get("Preset type"),
                    x_ft=float(get("X")),
                    y_ft=float(get("Y")),
                    latitude=float(get("Lat")),
                    longitude=float(get("Long")),
                    z_frame_attach_ft=float(get("Z frame attach")),
                    z_terrain_ft=float(get("Z terrain enter")),
                )
            )
        except ValueError:
            continue
    return piles


@dataclass
class PvcaseBomData:
    project_name: str
    overview: dict[str, float | str]
    transformer_to_inverter: list[CableSegment]
    inverter_to_combiner: list[CableSegment]
    combiner_to_string: list[CableSegment]
    piles: list[PileRow] = field(default_factory=list)

    def all_tags(self) -> set[str]:
        """Every equipment tag mentioned anywhere in the BOM (from + to sides
        of all three circuit sheets), excluding string sub-tags (the
        '.STRnn' suffix) since those aren't independently placed equipment."""
        tags: set[str] = set()
        for seg in (*self.transformer_to_inverter, *self.inverter_to_combiner, *self.combiner_to_string):
            for tag in (seg.from_tag, seg.to_tag):
                base = tag.split(".")[0]
                tags.add(base)
        return tags


def _strip_label(cell: str) -> str:
    for prefix in _LABEL_PREFIXES:
        if cell.startswith(prefix):
            return cell[len(prefix):]
    return cell


def _cell_text(cell_el, shared: list[str]) -> str:
    t = cell_el.get("t")
    v = cell_el.find("m:v", _NS)
    val = v.text if v is not None else ""
    if t == "s" and val != "":
        val = shared[int(val)]
    return val or ""


def _read_workbook_sheets(zf: zipfile.ZipFile) -> dict[str, str]:
    """Maps sheet name -> worksheet XML part path, honoring the real
    r:id/relationship mapping rather than assuming sheetN.xml lines up with
    the Nth <sheet> entry (usually true, not guaranteed)."""
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {
        rel.get("Id"): rel.get("Target")
        for rel in rels_root.findall("r:Relationship", _REL_NS)
    }
    result: dict[str, str] = {}
    rid_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sheet_el in wb_root.findall(".//m:sheets/m:sheet", _NS):
        name = sheet_el.get("name")
        rid = sheet_el.get(rid_key)
        target = rid_to_target.get(rid, "")
        target = target if target.startswith("worksheets/") else f"worksheets/{target}"
        result[name] = f"xl/{target}"
    return result


def _read_sheet_rows(zf: zipfile.ZipFile, part_path: str, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(zf.read(part_path))
    rows: list[list[str]] = []
    for row_el in root.findall(".//m:sheetData/m:row", _NS):
        cells = [_cell_text(c, shared) for c in row_el.findall("m:c", _NS)]
        rows.append(cells)
    return rows


def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall("m:si", _NS):
        out.append("".join(t.text or "" for t in si.findall(".//m:t", _NS)))
    return out


def _trim_trailing_empty(cells: list[str]) -> list[str]:
    trimmed = list(cells)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return trimmed


def _parse_circuit_sheet(rows: list[list[str]]) -> list[CableSegment]:
    """Shared parser for the three From/To/length(s) sheets. PVCase's
    exporter omits the leading `From` cell on rows that continue the
    previous group instead of repeating it every row, so a data row has
    either `len(header)` meaningful cells (starts a new From) or
    `len(header)-1` (continues the carried-forward From) -- see the
    module docstring's real-file walkthrough."""
    if not rows:
        return []
    header = _trim_trailing_empty(rows[0])
    if len(header) < 3 or header[0] != "From" or header[1] != "To":
        raise PvcaseBomError(f"Unexpected circuit-sheet header: {header!r}")

    segments: list[CableSegment] = []
    current_from: str | None = None
    for raw_row in rows[1:]:
        cells = _trim_trailing_empty(raw_row)
        if not cells:
            continue
        if len(cells) == len(header):
            current_from = _strip_label(cells[0])
            rest = cells[1:]
        elif len(cells) == len(header) - 1:
            if current_from is None:
                raise PvcaseBomError(f"Continuation row before any From was set: {raw_row!r}")
            rest = cells
        else:
            raise PvcaseBomError(f"Unexpected row width {len(cells)} (header has {len(header)}): {raw_row!r}")

        to_tag = _strip_label(rest[0])
        length_in = float(rest[1])
        extra_ft = {}
        for name, value in zip(header[3:], rest[2:]):
            try:
                extra_ft[_extra_key(name)] = float(value) / 12.0
            except ValueError:
                pass
        segments.append(CableSegment(from_tag=current_from, to_tag=to_tag, length_ft=length_in / 12.0, extra_ft=extra_ft))
    return segments


def _extra_key(header_name: str) -> str:
    # "+"/"-" distinguish positive/negative-conductor run lengths on the DC
    # Combiner To String sheet (e.g. "Cable length -, in") -- spell them out
    # before stripping punctuation so they don't collapse to the same key.
    normalized = header_name.lower().replace("+", " plus ").replace("-", " minus ")
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def _parse_overview(rows: list[list[str]]) -> tuple[str, dict[str, float | str]]:
    project_name = ""
    overview: dict[str, float | str] = {}
    for row in rows:
        cells = _trim_trailing_empty(row)
        if not cells:
            continue
        if cells[0].startswith("Project name:") and not project_name:
            project_name = cells[0].split(":", 1)[1].strip()
            continue
        if len(cells) == 2 and cells[0] and cells[1] != "" and cells[0] != cells[1]:
            key, value = cells[0], cells[1]
            try:
                overview[key] = float(value)
            except ValueError:
                overview[key] = value
    return project_name, overview


def parse_pvcase_bom(path: str | Path) -> PvcaseBomData:
    path = Path(path)
    if not path.exists():
        raise PvcaseBomError(f"BOM file not found: {path}")

    with zipfile.ZipFile(path) as zf:
        shared = _read_shared_strings(zf)
        sheet_parts = _read_workbook_sheets(zf)

        required = ["Project Overview", "Transformer To Inverter", "Inverter To DC Combiner", "DC Combiner To String"]
        missing = [name for name in required if name not in sheet_parts]
        if missing:
            raise PvcaseBomError(f"BOM is missing expected sheet(s): {missing}. Found: {list(sheet_parts)}")

        overview_rows = _read_sheet_rows(zf, sheet_parts["Project Overview"], shared)
        project_name, overview = _parse_overview(overview_rows)

        transformer_to_inverter = _parse_circuit_sheet(_read_sheet_rows(zf, sheet_parts["Transformer To Inverter"], shared))
        inverter_to_combiner = _parse_circuit_sheet(_read_sheet_rows(zf, sheet_parts["Inverter To DC Combiner"], shared))
        combiner_to_string = _parse_circuit_sheet(_read_sheet_rows(zf, sheet_parts["DC Combiner To String"], shared))

        # Not in `required` -- older exports or a non-tracker (fixed-tilt,
        # carport) site may not have it -- so absence degrades to an empty
        # pile list rather than a hard error.
        piles = (
            parse_piling_information(_read_sheet_rows(zf, sheet_parts["Piling information"], shared))
            if "Piling information" in sheet_parts
            else []
        )

    return PvcaseBomData(
        project_name=project_name,
        overview=overview,
        transformer_to_inverter=transformer_to_inverter,
        inverter_to_combiner=inverter_to_combiner,
        combiner_to_string=combiner_to_string,
        piles=piles,
    )
