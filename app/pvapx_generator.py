"""Generates a real Solmetric PVA `.pvapx` project file directly from a
parsed PVCase BOM (app/pvcase_bom_import.py) + a catalog module
(app/module_catalog.py) -- so a field technician can open a ready-built
test plan instead of hand-keying the switchboard/inverter/combiner/string
tree before an IV-curve commissioning test.

This is a straight port of solar_calc_engine/pvapx_generator.py (built and
CONFIRMED WORKING in real Solmetric PVA software, 2026-08-10, against the
real Encore Brighton 1 BOM -- 2 switchboards, 36 inverters, 432 strings),
adapted to consume this codebase's own `PvcaseBomData` (CableSegment lists)
directly instead of reintroducing solar_calc_engine's separate `BomTree`
type. THIS PORT HAS ALSO BEEN CONFIRMED WORKING: a file generated via the
real `POST /fluke/pvapx` endpoint against the same real Encore Brighton 1
BOM was opened in real Solmetric PVA software on 2026-08-10 and loaded
correctly, matching the original. That confirms the ported glue code
(consuming `PvcaseBomData` instead of `BomTree`) didn't introduce a
regression -- it does NOT retroactively confirm the one structural
assumption neither port has ever tested (see below): every generated
`.pvapx` still needs its own validation-gate check before real field use.

`IV_Curve_Panel_Handoff_Spec.md` previously listed PVA project-file auto-
generation as explicitly deferred ("no documented import path exists" --
true; this doesn't use one, it clones a real file and rewrites known
regions of its XML). See that file's 2026-08-10 update for the reversal
and the evidence behind it.

HOW THIS WORKS: `.pvapx` is a zip of XML (undocumented, reverse-engineered
from three real, already-measured project files -- not from a Solmetric
spec). `generate_pvapx()` clones an existing, real, working `.pvapx`
(`template_path` -- any project file someone at the firm has, doesn't need
to relate to the target site) and rewrites only the two parts that are
actually project-specific: `ModuleDefinitions` (from a catalog module) and
the `InverterGroups`/`SourceGroups`/`PvStringModel` tree (from a parsed
BOM). Everything else -- GPS/elevation/timezone, wire gauge/length/
material, sensor calibration -- is carried through from the template
untouched, since neither this app nor the BOM has a reliable source for
those yet.

`data/` and `meas/` are stripped entirely from the output (those only
exist once real measurements are taken), and `history.xml` (a rolling
measurement-snapshot buffer) is reset to empty. This is the one
structural assumption that remains UNVERIFIED against a blank Solmetric-
authored project (none of the samples this was built against was ever a
never-tested project) -- see the port's own validation, same as the
original.

MANDATORY VALIDATION GATE: a `.pvapx` this produces must be opened in the
real Solmetric PVA desktop software and confirmed to load correctly
before it's trusted on an actual job. This module cannot verify that
itself -- two prior generated files (one per codebase) have passed this
check, but that's evidence the *approach* works, not a blanket clearance
for every future output (a template with a materially different site
config, a much larger tree, etc. is still worth spot-checking).

`measureId` allocation (on `CombinerModel`/`PvStringModel` nodes): a
running counter across the whole tree in document order, +1 per combiner,
+(1 + NumberModulesPerString) per string -- inferred from three real
samples (e.g. measureIds 1, 28, 55 for consecutive 26-module strings,
28-1=27=1+26) and confirmed to match a real 2-switchboard, 432-string
project exactly when ported. What the reserved per-module id space is
actually for isn't known.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape

from pydantic import BaseModel

from app.models import ProjectInput
from app.module_catalog import ModuleElectricalSpec
from app.pvcase_bom_import import PvcaseBomData


class FlukePvapxRequest(BaseModel):
    """Path-based and local-machine-only, same reasoning as
    PvcaseValidateRequest's dwg_path: both the BOM and the template
    .pvapx live in the engineer's Dropbox-synced folder, and the output
    file is only useful to whoever runs this backend on their own machine
    (there's nowhere meaningful to open a .pvapx from a cloud deployment)."""

    project: ProjectInput
    bom_path: str
    template_path: str
    output_path: str
    modules_per_string: int
    manufacturer: str
    model_name: str | None = None  # defaults to project.module.sku if unset
    noct_c: float = 45.0


def _esc(value) -> str:
    return escape(str(value))


_SWITCHBOARD_RE = re.compile(r"^[A-Za-z]*-?(\d+)-\d+$")


def _switchboard_label(inverter_tag: str) -> str:
    """Infers switchboard membership from an inverter tag's leading numeric
    segment ("INV-1-1"/"INV-1-2" -> "SWBD1", "INV-2-1" -> "SWBD2") --
    matches this app's own naming convention (app/pvcase_plan.py's
    PvcaseNamingConvention: prefix-separator-board-separator-n) and
    confirmed against the real Encore Brighton 1 BOM (36 inverters split
    exactly into INV-1-1..INV-1-18 / INV-2-1..INV-2-18, matching the two
    real switchboards in that project's .pvapx/.xlsm files). Falls back to
    a single "SWBD1" group if a tag doesn't match that shape."""
    m = _SWITCHBOARD_RE.match(inverter_tag)
    return f"SWBD{m.group(1)}" if m else "SWBD1"


def _to_solmetric_inverter_case(raw_tag: str) -> str:
    """PVCase's "INV-1-1" vs. Solmetric's "Inv-1-1" -- confirmed against
    three real .pvapx samples that combiner tags ("DCC-1-1") and string
    tags ("STR1") already match verbatim; only the inverter prefix differs."""
    if raw_tag.upper().startswith("INV-"):
        return "Inv-" + raw_tag[4:]
    return raw_tag


@dataclass
class _BomHierarchy:
    """Inverter tag -> combiner tag -> list of string suffixes ("STR1"),
    built from PvcaseBomData's flat CableSegment lists."""
    inverters: dict[str, dict[str, list[str]]]


def _build_hierarchy(bom: PvcaseBomData) -> _BomHierarchy:
    combiners_by_inverter: dict[str, list[str]] = {}
    for seg in bom.inverter_to_combiner:
        combiners_by_inverter.setdefault(seg.from_tag, []).append(seg.to_tag)

    strings_by_combiner: dict[str, list[str]] = {}
    for seg in bom.combiner_to_string:
        # PVCase's own string label keeps the full "INV-1-1.STR1" shape
        # (see pvcase_bom_import.py) -- only the suffix after the dot is
        # the string's own tag.
        string_suffix = seg.to_tag.split(".")[-1]
        strings_by_combiner.setdefault(seg.from_tag, []).append(string_suffix)

    inverters: dict[str, dict[str, list[str]]] = {}
    for inverter_tag, combiner_tags in combiners_by_inverter.items():
        inverters[inverter_tag] = {
            combiner_tag: strings_by_combiner.get(combiner_tag, [])
            for combiner_tag in combiner_tags
        }
    return _BomHierarchy(inverters=inverters)


class _MeasureIdAllocator:
    def __init__(self, start: int = 0):
        self.next_id = start

    def take_combiner_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    def take_string_id(self, modules_per_string: int) -> int:
        value = self.next_id
        self.next_id += 1 + modules_per_string
        return value


def _render_string(allocator: _MeasureIdAllocator, string_tag: str, modules_per_string: int) -> str:
    measure_id = allocator.take_string_id(modules_per_string)
    return (
        f'                  <DcSourceOrGroupModel xsi:type="PvStringModel" measureId="{measure_id}">\n'
        f'                    <CustomName>{_esc(string_tag)}</CustomName>\n'
        f'                    <IsChildNamingCustom>false</IsChildNamingCustom>\n'
        f'                    <IsChildEnumerationAlphabetic>false</IsChildEnumerationAlphabetic>\n'
        f'                    <NumberModulesPerString>{modules_per_string}</NumberModulesPerString>\n'
        f'                    <Modules />\n'
        f'                  </DcSourceOrGroupModel>\n'
    )


def _render_combiner(allocator: _MeasureIdAllocator, combiner_tag: str, string_tags: list[str], modules_per_string: int) -> str:
    measure_id = allocator.take_combiner_id()
    strings_xml = "".join(_render_string(allocator, tag, modules_per_string) for tag in string_tags)
    return (
        f'              <DcSourceOrGroupModel xsi:type="CombinerModel" measureId="{measure_id}">\n'
        f'                <CustomName>{_esc(combiner_tag)}</CustomName>\n'
        f'                <IsChildNamingCustom>true</IsChildNamingCustom>\n'
        f'                <IsChildEnumerationAlphabetic>false</IsChildEnumerationAlphabetic>\n'
        f'                <NumberModulesPerString>0</NumberModulesPerString>\n'
        f'                <SourceGroups>\n'
        f'{strings_xml}'
        f'                </SourceGroups>\n'
        f'              </DcSourceOrGroupModel>\n'
    )


def _render_inverter(allocator: _MeasureIdAllocator, inverter_tag: str, combiners: dict[str, list[str]], modules_per_string: int) -> str:
    combiners_xml = "".join(
        _render_combiner(allocator, combiner_tag, string_tags, modules_per_string)
        for combiner_tag, string_tags in combiners.items()
    )
    return (
        f'          <PvInverterOrGroupModel xsi:type="PvInverterModel">\n'
        f'            <CustomName>{_esc(_to_solmetric_inverter_case(inverter_tag))}</CustomName>\n'
        f'            <IsChildEnumerationAlphabetic>false</IsChildEnumerationAlphabetic>\n'
        f'            <IsChildNamingCustom>true</IsChildNamingCustom>\n'
        f'            <NumberModulesPerString>0</NumberModulesPerString>\n'
        f'            <InverterName>Undefined Inverter</InverterName>\n'
        f'            <SourceGroups>\n'
        f'{combiners_xml}'
        f'            </SourceGroups>\n'
        f'          </PvInverterOrGroupModel>\n'
    )


def _render_switchboard(switchboard_id: str, inverters: dict[str, dict[str, list[str]]], allocator: _MeasureIdAllocator, modules_per_string: int) -> str:
    inverters_xml = "".join(
        _render_inverter(allocator, inverter_tag, combiners, modules_per_string)
        for inverter_tag, combiners in inverters.items()
    )
    return (
        '      <PvInverterOrGroupModel xsi:type="InverterGroupModel">\n'
        f'        <CustomName>{_esc(switchboard_id)}</CustomName>\n'
        '        <IsChildEnumerationAlphabetic>false</IsChildEnumerationAlphabetic>\n'
        '        <IsChildNamingCustom>true</IsChildNamingCustom>\n'
        '        <NumberModulesPerString>0</NumberModulesPerString>\n'
        '        <InverterGroups>\n'
        f'{inverters_xml}'
        '        </InverterGroups>\n'
        '      </PvInverterOrGroupModel>\n'
    )


@dataclass
class PvapxTreeCounts:
    switchboards: int
    inverters: int
    combiners: int
    strings: int


def build_inverter_groups_xml(bom: PvcaseBomData, modules_per_string: int) -> tuple[str, PvapxTreeCounts]:
    """Renders the full <InverterGroups>...</InverterGroups> block for
    every switchboard/inverter/combiner/string in `bom`.

    Verified against a real 2-switchboard project's XML nesting: ONE outer
    <InverterGroups> wraps one <PvInverterOrGroupModel
    xsi:type="InverterGroupModel"> sibling per switchboard, each of which
    has its own nested <InverterGroups> holding that switchboard's
    inverters -- not N separate top-level <InverterGroups> blocks.
    """
    hierarchy = _build_hierarchy(bom)
    groups: dict[str, dict[str, dict[str, list[str]]]] = {}
    for inverter_tag, combiners in hierarchy.inverters.items():
        groups.setdefault(_switchboard_label(inverter_tag), {})[inverter_tag] = combiners

    allocator = _MeasureIdAllocator()
    switchboards_xml = "".join(
        _render_switchboard(label, inverters, allocator, modules_per_string)
        for label, inverters in groups.items()
    )
    counts = PvapxTreeCounts(
        switchboards=len(groups),
        inverters=sum(len(inv) for inv in groups.values()),
        combiners=len(bom.inverter_to_combiner),
        strings=len(bom.combiner_to_string),
    )
    return f'    <InverterGroups>\n{switchboards_xml}    </InverterGroups>\n', counts


def build_module_definitions_xml(
    module: ModuleElectricalSpec,
    manufacturer: str,
    model_name: str,
    noct_c: float = 45.0,
) -> str:
    """Solmetric's ModuleDefinitions only carries Beta (Voc) and Gamma
    (Pmax) temperature coefficients -- confirmed against three real .pvapx
    samples, none of which carried an Isc/Alpha coefficient even though
    `ModuleElectricalSpec` has one (`temp_coeff_isc_pct_per_c` is simply
    not written here). Gamma/Pmax isn't tracked on `ModuleElectricalSpec`
    at all (this catalog never needed it -- see app/iv_curve_calc.py's Vmp
    formula, which reuses the Voc coefficient) -- passed in in the caller's
    real datasheet value where available, defaulting to a generic
    crystalline-silicon placeholder. Same for NOCT."""
    return (
        '    <ModuleDefinitions>\n'
        '      <PvModuleDefinitionModel>\n'
        f'        <Manufacturer>{_esc(manufacturer)}</Manufacturer>\n'
        f'        <Model>{_esc(model_name)}</Model>\n'
        '        <ParametricData>\n'
        f'          <Pmpp>{module.pmax:g}</Pmpp>\n'
        f'          <Vmpp>{module.vmp:g}</Vmpp>\n'
        f'          <Impp>{module.imp:g}</Impp>\n'
        f'          <Voc>{module.voc:g}</Voc>\n'
        f'          <Isc>{module.isc:g}</Isc>\n'
        f'          <Noct>{noct_c:g}</Noct>\n'
        f'          <GammaPmppPercent>-0.3</GammaPmppPercent>\n'
        f'          <BetaVocPercent>{module.temp_coeff_voc_pct_per_c:g}</BetaVocPercent>\n'
        '        </ParametricData>\n'
        '      </PvModuleDefinitionModel>\n'
        '    </ModuleDefinitions>\n'
    )


_RESET_HISTORY_XML = (
    '<?xml version="1.0"?>\n'
    '<PvaHistoryFile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
    '  <Length>0</Length>\n'
    '  <Snapshots />\n'
    '</PvaHistoryFile>'
)


def _replace_balanced_block(xml_text: str, tag: str, replacement: str) -> str:
    """Replaces the first <tag>...</tag> block, correctly handling the case
    where `tag` nests inside itself (true of InverterGroups: the top-level
    wrapper has a same-named child for each switchboard) -- a naive
    non-greedy regex would stop at the first, innermost closing tag and
    silently truncate the replacement."""
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    start = xml_text.find(open_tag)
    if start == -1:
        raise ValueError(f"template setting XML has no <{tag}> block")

    depth = 0
    i = start
    while True:
        next_open = xml_text.find(open_tag, i)
        next_close = xml_text.find(close_tag, i)
        if next_close == -1:
            raise ValueError(f"template setting XML has an unbalanced <{tag}> block")
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + len(open_tag)
        else:
            depth -= 1
            i = next_close + len(close_tag)
            if depth == 0:
                return xml_text[:start] + replacement + xml_text[i:]


def generate_pvapx(
    template_path: str,
    output_path: str,
    bom: PvcaseBomData,
    module: ModuleElectricalSpec,
    manufacturer: str,
    model_name: str,
    modules_per_string: int,
    noct_c: float = 45.0,
) -> PvapxTreeCounts:
    """See module docstring -- clones `template_path` and rewrites its
    ModuleDefinitions + InverterGroups tree from `bom`/`module`. UNVERIFIED
    against real Solmetric software for THIS port; see the mandatory
    validation gate in the module docstring before using output on an
    actual job. Returns the tree counts written, for the caller to report
    back (e.g. via the /fluke/pvapx endpoint)."""
    with zipfile.ZipFile(template_path, "r") as template_zip:
        names = template_zip.namelist()
        setting_name = next((n for n in names if re.match(r"setting\d*\.xml$", n)), None)
        if setting_name is None:
            raise ValueError(f"no setting*.xml found in template -- got {names!r}")
        setting_xml = template_zip.read(setting_name).decode("utf-8")
        history_name = "history.xml" if "history.xml" in names else None

        inverter_groups_xml, counts = build_inverter_groups_xml(bom, modules_per_string)

        setting_xml = _replace_balanced_block(
            setting_xml, "ModuleDefinitions",
            build_module_definitions_xml(module, manufacturer, model_name, noct_c),
        )
        setting_xml = _replace_balanced_block(setting_xml, "InverterGroups", inverter_groups_xml)
        # Top-level default (used by Solmetric's own "add string" UI) --
        # distinguished from the many per-node <NumberModulesPerString>0</...>
        # tags by its unique position right after </WireProperties>.
        setting_xml = re.sub(
            r"(</WireProperties>\s*<NumberModulesPerString>)\d+(</NumberModulesPerString>)",
            rf"\g<1>{modules_per_string}\g<2>",
            setting_xml, count=1,
        )

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out_zip:
            for item in template_zip.infolist():
                name = item.filename
                if name.startswith("data/") or name.startswith("meas/"):
                    continue
                if name == setting_name:
                    out_zip.writestr(item, setting_xml.encode("utf-8"))
                elif history_name and name == history_name:
                    out_zip.writestr(item, _RESET_HISTORY_XML.encode("utf-8"))
                else:
                    out_zip.writestr(item, template_zip.read(name))

    return counts
