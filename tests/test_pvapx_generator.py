from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

from app.module_catalog import MODULE_SKUS
from app.pvapx_generator import build_inverter_groups_xml, generate_pvapx
from app.pvcase_bom_import import CableSegment, PvcaseBomData

SETTING_TEMPLATE = (
    '<?xml version="1.0"?>\n'
    '<PvaPcProject xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
    '  <Latitude>39.0</Latitude>\n'
    '  <PvDesignTree2>\n'
    '    <InverterGroups>\n'
    '      <PvInverterOrGroupModel xsi:type="InverterGroupModel">\n'
    '        <CustomName>SWBD1</CustomName>\n'
    '        <NumberModulesPerString>0</NumberModulesPerString>\n'
    '        <InverterGroups>\n'
    '          <PvInverterOrGroupModel xsi:type="PvInverterModel">\n'
    '            <CustomName>Inv-1-1</CustomName>\n'
    '            <NumberModulesPerString>0</NumberModulesPerString>\n'
    '            <SourceGroups>\n'
    '              <DcSourceOrGroupModel xsi:type="CombinerModel" measureId="0">\n'
    '                <CustomName>DCC-1-1</CustomName>\n'
    '                <SourceGroups>\n'
    '                  <DcSourceOrGroupModel xsi:type="PvStringModel" measureId="1">\n'
    '                    <CustomName>STR1</CustomName>\n'
    '                    <NumberModulesPerString>26</NumberModulesPerString>\n'
    '                    <Modules />\n'
    '                  </DcSourceOrGroupModel>\n'
    '                </SourceGroups>\n'
    '              </DcSourceOrGroupModel>\n'
    '            </SourceGroups>\n'
    '          </PvInverterOrGroupModel>\n'
    '        </InverterGroups>\n'
    '      </PvInverterOrGroupModel>\n'
    '    </InverterGroups>\n'
    '    <ModuleDefinitions>\n'
    '      <PvModuleDefinitionModel>\n'
    '        <Manufacturer>Old Manufacturer</Manufacturer>\n'
    '        <Model>Old Model</Model>\n'
    '        <ParametricData>\n'
    '          <Pmpp>500</Pmpp>\n'
    '        </ParametricData>\n'
    '      </PvModuleDefinitionModel>\n'
    '    </ModuleDefinitions>\n'
    '    <InverterDefinitions />\n'
    '    <WireProperties>\n'
    '      <WireGauge>10</WireGauge>\n'
    '      <WireLength>30</WireLength>\n'
    '    </WireProperties>\n'
    '    <NumberModulesPerString>26</NumberModulesPerString>\n'
    '  </PvDesignTree2>\n'
    '</PvaPcProject>\n'
)
HISTORY_TEMPLATE = (
    '<?xml version="1.0"?>\n'
    '<PvaHistoryFile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
    '  <Length>5</Length>\n'
    '  <Snapshots><MeasurementSnapshot><Isc>1</Isc></MeasurementSnapshot></Snapshots>\n'
    '</PvaHistoryFile>\n'
)


def _write_fixture_template(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("setting9.xml", SETTING_TEMPLATE)
        z.writestr("history.xml", HISTORY_TEMPLATE)
        z.writestr("data/1", "<PvaMeasureFile />")
        z.writestr("meas/06-01-2026_12_00_00", "<x/>")


def _two_switchboard_bom() -> PvcaseBomData:
    return PvcaseBomData(
        project_name="Test", overview={},
        transformer_to_inverter=[],
        inverter_to_combiner=[
            CableSegment(from_tag="INV-1-1", to_tag="DCC-1-1", length_ft=10.0),
            CableSegment(from_tag="INV-2-1", to_tag="DCC-2-1", length_ft=10.0),
        ],
        combiner_to_string=[
            CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR1", length_ft=10.0),
            CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR2", length_ft=10.0),
            CableSegment(from_tag="DCC-2-1", to_tag="INV-2-1.STR1", length_ft=10.0),
        ],
    )


def test_build_inverter_groups_xml_groups_by_switchboard_and_matches_real_measure_id_sequence():
    bom = _two_switchboard_bom()
    xml, counts = build_inverter_groups_xml(bom, modules_per_string=26)

    assert counts.switchboards == 2
    assert counts.inverters == 2
    assert counts.strings == 3
    assert xml.count('xsi:type="InverterGroupModel"') == 2
    assert '<CustomName>SWBD1</CustomName>' in xml
    assert '<CustomName>SWBD2</CustomName>' in xml
    assert '<CustomName>Inv-1-1</CustomName>' in xml  # PVCase "INV-1-1" -> Solmetric "Inv-1-1"
    # measureId sequence: combiner=0, STR1=1, STR2=28 (1 + 26 + 1 = 28) -- matches the
    # real pattern verified against three real .pvapx samples.
    assert 'measureId="0"' in xml
    assert 'measureId="1"' in xml
    assert 'measureId="28"' in xml


def test_generate_pvapx_produces_valid_stripped_structure(tmp_path):
    template_path = tmp_path / "template.pvapx"
    output_path = tmp_path / "generated.pvapx"
    _write_fixture_template(template_path)

    bom = _two_switchboard_bom()
    module = MODULE_SKUS["ZXM7-UHLDD144"]
    counts = generate_pvapx(
        str(template_path), str(output_path), bom, module,
        manufacturer="Znshine PV-Tech", model_name="ZXM7-UHLDD144", modules_per_string=26,
    )
    assert counts.switchboards == 2
    assert counts.strings == 3

    with zipfile.ZipFile(output_path) as z:
        names = z.namelist()
        assert not any(n.startswith("data/") or n.startswith("meas/") for n in names)
        setting = z.read("setting9.xml").decode("utf-8")
        history = z.read("history.xml").decode("utf-8")

    root = ET.fromstring(setting)  # must be well-formed
    assert root.tag == "PvaPcProject"
    ET.fromstring(history)
    assert "<Length>0</Length>" in history

    assert "Znshine PV-Tech" in setting
    assert "ZXM7-UHLDD144" in setting
    assert "Old Manufacturer" not in setting
    assert setting.count('xsi:type="InverterGroupModel"') == 2
    assert setting.count('xsi:type="PvStringModel"') == 3
