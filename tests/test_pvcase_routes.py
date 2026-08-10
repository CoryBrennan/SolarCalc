from fastapi.testclient import TestClient

from app.main import app
from app.pvcase_bom_import import CableSegment, PvcaseBomData, PvcaseBomError
from app.pvcase_dwg_scan import DwgDeviceTag, PvcaseDwgError

client = TestClient(app)


def _plan_body(**overrides):
    body = {
        "project": {"inverter": {"quantity": 2, "dc_topology": "combiner"}},
        "plan": {"switchboards": [{"tag": "SWBD-1", "inverter_count": 2, "transformer_tag": "XFMR-1"}]},
    }
    body.update(overrides)
    return body


def test_pvcase_plan_endpoint_returns_expected_tags():
    response = client.post("/pvcase/plan", json=_plan_body())
    assert response.status_code == 200
    data = response.json()
    assert data["expected_tags"]["inverters"] == ["INV-1-1", "INV-1-2"]
    assert data["expected_tags"]["transformers"] == ["XFMR-1"]


def test_pvcase_validate_endpoint_with_no_paths_returns_warning():
    response = client.post("/pvcase/validate", json=_plan_body())
    assert response.status_code == 200
    data = response.json()
    assert data["bom_present"] is False
    assert data["dwg_present"] is False
    assert any("Neither a BOM export" in w for w in data["warnings"])


def test_pvcase_validate_endpoint_parses_bom_path(monkeypatch):
    fake_bom = PvcaseBomData(
        project_name="Test",
        overview={},
        transformer_to_inverter=[CableSegment(from_tag="XFMR-1", to_tag="INV-1-1", length_ft=10.0)],
        inverter_to_combiner=[CableSegment(from_tag="INV-1-1", to_tag="DCC-1-1", length_ft=5.0)],
        combiner_to_string=[],
    )
    captured = {}

    def fake_parse(path):
        captured["path"] = path
        return fake_bom

    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", fake_parse)

    body = _plan_body(bom_path="C:/fake/bom.xlsx")
    response = client.post("/pvcase/validate", json=body)

    assert response.status_code == 200
    assert captured["path"] == "C:/fake/bom.xlsx"
    data = response.json()
    assert data["bom_present"] is True


def test_pvcase_validate_endpoint_returns_422_on_bom_parse_error(monkeypatch):
    def fake_parse(path):
        raise PvcaseBomError("bad sheet")

    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", fake_parse)

    response = client.post("/pvcase/validate", json=_plan_body(bom_path="C:/fake/bom.xlsx"))
    assert response.status_code == 422
    assert "bad sheet" in response.json()["detail"]


def test_pvcase_validate_endpoint_returns_422_on_dwg_scan_error(monkeypatch):
    def fake_scan(path):
        raise PvcaseDwgError("accoreconsole.exe not found")

    monkeypatch.setattr("app.main.pvcase_dwg_scan.scan_device_tags", fake_scan)

    response = client.post("/pvcase/validate", json=_plan_body(dwg_path="C:/fake/site.dwg"))
    assert response.status_code == 422
    assert "accoreconsole" in response.json()["detail"]


def test_pvcase_validate_endpoint_parses_dwg_path(monkeypatch):
    # _plan_body()'s default plan (SWBD-1, 2 inverters, combiner topology)
    # expects DCC-1-1/2 and XFMR-1 too -- include those so report.ok() is True.
    fake_tags = [
        DwgDeviceTag(tag="INV-1-1", x=0, y=0),
        DwgDeviceTag(tag="INV-1-2", x=1, y=1),
        DwgDeviceTag(tag="DCC-1-1", x=0, y=2),
        DwgDeviceTag(tag="DCC-1-2", x=1, y=2),
        DwgDeviceTag(tag="XFMR-1", x=5, y=5),
    ]
    monkeypatch.setattr("app.main.pvcase_dwg_scan.scan_device_tags", lambda path: fake_tags)

    response = client.post("/pvcase/validate", json=_plan_body(dwg_path="C:/fake/site.dwg"))
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["dwg_present"] is True
    inv_comp = next(c for c in data["comparisons"] if c["equipment"] == "inverters")
    assert inv_comp["plan_vs_dwg"]["matched_count"] == 2


def _routing_body(**overrides):
    body = {
        "project": {"inverter": {"quantity": 2, "dc_topology": "combiner"}},
        "bom_path": "C:/fake/bom.xlsx",
    }
    body.update(overrides)
    return body


def test_pvcase_routing_report_endpoint_parses_bom_and_returns_all_circuits(monkeypatch):
    fake_bom = PvcaseBomData(
        project_name="Test",
        overview={},
        transformer_to_inverter=[CableSegment(from_tag="XFMR-1", to_tag="INV-1-1", length_ft=50.0)],
        inverter_to_combiner=[CableSegment(from_tag="INV-1-1", to_tag="DCC-1-1", length_ft=200.0)],
        combiner_to_string=[CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR1", length_ft=100.0)],
    )
    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", lambda path: fake_bom)

    response = client.post("/pvcase/routing-report", json=_routing_body())

    assert response.status_code == 200
    data = response.json()
    circuits = {c["circuit"] for c in data["circuits"]}
    assert circuits == {"transformer_to_inverter", "inverter_to_combiner", "combiner_to_string"}
    for c in data["circuits"]:
        assert c["selected_conductor"] is not None
        assert c["final_conductor"] is not None


def test_pvcase_routing_report_endpoint_returns_422_on_bom_parse_error(monkeypatch):
    def fake_parse(path):
        raise PvcaseBomError("bad sheet")

    monkeypatch.setattr("app.main.pvcase_bom_import.parse_pvcase_bom", fake_parse)

    response = client.post("/pvcase/routing-report", json=_routing_body())
    assert response.status_code == 422
    assert "bad sheet" in response.json()["detail"]
