from __future__ import annotations

from fastapi.testclient import TestClient

from app.fluke_export_import import FlukeImportError, FlukeReading
from app.main import app
from app.pvcase_bom_import import CableSegment, PvcaseBomData, PvcaseBomError

client = TestClient(app)

_FAKE_READING = FlukeReading(
    switchboard="SWBD1", inverter="Inv-1-1", combiner="DCC-1-1", string_id="STR1",
    irradiance_w_m2=850.0, temp_c=42.0,
    isc_measured_a=12.0, isc_modeled_a=11.9, isc_deviation_vs_modeled_pct=0.8,
)
_FAKE_BOM = PvcaseBomData(
    project_name="Test", overview={},
    transformer_to_inverter=[], inverter_to_combiner=[],
    combiner_to_string=[CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR1", length_ft=10.0)],
)


def test_fluke_validate_endpoint_returns_report(monkeypatch):
    monkeypatch.setattr("app.main.fluke_export_import.parse_fluke_export", lambda path: [_FAKE_READING])
    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", lambda path: _FAKE_BOM)

    response = client.post("/fluke/validate", json={
        "project": {"module": {"sku": "720"}, "iv_curve_conditions": {"modules_per_string": 28}},
        "export_path": "C:/fake/export.xlsm",
        "bom_path": "C:/fake/bom.xlsx",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["reading_count"] == 1
    assert data["coverage_complete"] is True
    assert data["coverage"]["matched_count"] == 1


def test_fluke_validate_endpoint_skips_coverage_without_bom_path(monkeypatch):
    monkeypatch.setattr("app.main.fluke_export_import.parse_fluke_export", lambda path: [_FAKE_READING])

    response = client.post("/fluke/validate", json={
        "project": {"module": {"sku": "720"}},
        "export_path": "C:/fake/export.xlsm",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["coverage"] is None
    assert any("No BOM supplied" in w for w in data["warnings"])


def test_fluke_validate_endpoint_returns_422_on_parse_error(monkeypatch):
    def fake_parse(path):
        raise FlukeImportError("'Table' sheet not found")

    monkeypatch.setattr("app.main.fluke_export_import.parse_fluke_export", fake_parse)

    response = client.post("/fluke/validate", json={
        "project": {"module": {"sku": "720"}},
        "export_path": "C:/fake/export.xlsm",
    })

    assert response.status_code == 422
    assert "Table" in response.json()["detail"]


def test_fluke_pvapx_endpoint_returns_422_on_unknown_sku():
    response = client.post("/fluke/pvapx", json={
        "project": {"module": {"sku": "NOT-A-REAL-SKU"}},
        "bom_path": "C:/fake/bom.xlsx",
        "template_path": "C:/fake/template.pvapx",
        "output_path": "C:/fake/out.pvapx",
        "modules_per_string": 26,
        "manufacturer": "Test Manufacturer",
    })

    assert response.status_code == 422
    assert "NOT-A-REAL-SKU" in response.json()["detail"]


def test_fluke_pvapx_endpoint_returns_422_on_bom_parse_error(monkeypatch):
    def fake_parse(path):
        raise PvcaseBomError("bad sheet")

    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", fake_parse)

    response = client.post("/fluke/pvapx", json={
        "project": {"module": {"sku": "720"}},
        "bom_path": "C:/fake/bom.xlsx",
        "template_path": "C:/fake/template.pvapx",
        "output_path": "C:/fake/out.pvapx",
        "modules_per_string": 26,
        "manufacturer": "Test Manufacturer",
    })

    assert response.status_code == 422
    assert "bad sheet" in response.json()["detail"]


def test_fluke_pvapx_endpoint_calls_generator_and_includes_validation_gate(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", lambda path: _FAKE_BOM)

    from app.pvapx_generator import PvapxTreeCounts

    captured = {}

    def fake_generate(template_path, output_path, bom, module, **kwargs):
        captured["output_path"] = output_path
        captured["manufacturer"] = kwargs["manufacturer"]
        return PvapxTreeCounts(switchboards=1, inverters=1, combiners=1, strings=1)

    monkeypatch.setattr("app.main.pvapx_generator.generate_pvapx", fake_generate)

    response = client.post("/fluke/pvapx", json={
        "project": {"module": {"sku": "720"}},
        "bom_path": "C:/fake/bom.xlsx",
        "template_path": "C:/fake/template.pvapx",
        "output_path": str(tmp_path / "out.pvapx"),
        "modules_per_string": 26,
        "manufacturer": "Test Manufacturer",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["strings"] == 1
    assert "UNVERIFIED" in data["validation_gate"]
    assert captured["manufacturer"] == "Test Manufacturer"
