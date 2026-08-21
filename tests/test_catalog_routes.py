"""End-to-end ingestion pipeline over real HTTP (db_client), mirroring
test_changeset_api.py's pattern: routing by file type, second-document
merge into an existing pending draft vs. incorrectly creating a duplicate,
and approve promoting a version while the prior version stays queryable."""

from __future__ import annotations

import io

import pytest
from reportlab.pdfgen import canvas

from app.extraction_schema import ExtractedField, ExtractionAgentResponse, InverterFields, ModuleFields, ModuleVariant


def _make_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 750
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def _module_response(pmax=700, voc=48.6, source="p.2") -> ExtractionAgentResponse:
    return ExtractionAgentResponse(
        document_type="module_datasheet",
        manufacturer=ExtractedField(value="ReneSola", confidence="high", source="p.1"),
        model=ExtractedField(value="RS9-700", confidence="high", source="p.1"),
        module_fields=ModuleFields(
            variants=[
                ModuleVariant(
                    model_variant="RS9-700",
                    rated_power_w=ExtractedField(value=pmax, confidence="high", source=source),
                    voc_v=ExtractedField(value=voc, confidence="high", source=source),
                    isc_a=ExtractedField(value=18.32, confidence="high", source=source),
                    vmp_v=ExtractedField(value=40.5, confidence="high", source=source),
                    imp_a=ExtractedField(value=17.29, confidence="high", source=source),
                    module_efficiency_pct=ExtractedField(value=22.5, confidence="high", source=source),
                )
            ],
        ),
    )


def _inverter_response() -> ExtractionAgentResponse:
    return ExtractionAgentResponse(
        document_type="inverter_datasheet",
        manufacturer=ExtractedField(value="Chint Power Systems", confidence="high", source="p.1"),
        model=ExtractedField(value="CPS SCH350KTL", confidence="high", source="p.1"),
        inverter_fields=InverterFields(
            nameplate_ac_power_kw=ExtractedField(value=350, confidence="high", source="p.2"),
        ),
    )


def _upload(db_client, monkeypatch, response, equipment_type="module", filename="sheet.pdf"):
    monkeypatch.setattr("app.catalog_routes.call_extraction_agent", lambda pdf_bytes: response)
    pdf = _make_pdf(["fake datasheet text"])
    return db_client.post(
        "/ingest/upload",
        data={"equipment_type": equipment_type},
        files={"file": (filename, pdf, "application/pdf")},
    )


def test_pdf_upload_creates_pending_draft(db_client, monkeypatch):
    resp = _upload(db_client, monkeypatch, _module_response())
    assert resp.status_code == 200
    body = resp.json()
    assert body["job"]["status"] == "merged"
    assert len(body["catalog_versions"]) == 1

    version = body["catalog_versions"][0]
    assert version["status"] == "pending_review"
    assert version["manufacturer"] == "ReneSola"
    assert version["model"] == "RS9-700"
    assert version["fields"]["voc_v"]["value"] == 48.6
    assert version["fields"]["voc_v"]["confidence"] == "high"

    pending = db_client.get("/catalog/pending").json()
    assert any(v["version_id"] == version["version_id"] for v in pending)


def test_inverter_upload_routes_to_inverter_fields(db_client, monkeypatch):
    resp = _upload(db_client, monkeypatch, _inverter_response(), equipment_type="inverter")
    assert resp.status_code == 200
    version = resp.json()["catalog_versions"][0]
    assert version["equipment_type"] == "inverter"
    assert version["fields"]["nameplate_ac_power_kw"]["value"] == 350


def test_pan_upload_marks_needs_attention_without_catalog_write(db_client):
    fake_pan_bytes = b"fake PVsyst PAN content"
    resp = db_client.post(
        "/ingest/upload",
        data={"equipment_type": "module"},
        files={"file": ("module.PAN", fake_pan_bytes, "application/octet-stream")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job"]["status"] == "needs_attention"
    assert "no .PAN parser implemented" in body["job"]["last_error"]
    assert body["catalog_versions"] == []

    pending = db_client.get("/catalog/pending").json()
    assert pending == []


def test_second_document_merges_into_existing_pending_draft_not_a_duplicate(db_client, monkeypatch):
    first = _upload(db_client, monkeypatch, _module_response(), filename="datasheet.pdf")
    first_version = first.json()["catalog_versions"][0]

    # Second upload for the same manufacturer/model (e.g. an O&M manual) fills
    # in a field the first document didn't report (weight_kg).
    om_response = _module_response()
    om_response.module_fields.variants[0].module_efficiency_pct = ExtractedField(value=None, confidence="not_found")
    om_response.module_fields.weight_kg = ExtractedField(value=33.5, confidence="medium", source="p.4, om manual")

    second = _upload(db_client, monkeypatch, om_response, filename="om_manual.pdf")
    second_version = second.json()["catalog_versions"][0]

    assert second_version["version_id"] == first_version["version_id"]  # same draft, not a duplicate
    assert second_version["fields"]["weight_kg"]["value"] == 33.5
    assert second_version["fields"]["voc_v"]["value"] == 48.6  # preserved from the first document

    versions_list = db_client.get("/catalog/module/ReneSola/RS9-700/versions").json()
    assert len(versions_list["versions"]) == 1


def test_malformed_agent_response_marks_needs_attention(db_client, monkeypatch):
    def _raise(pdf_bytes):
        from app.extraction_agent import ExtractionAgentError

        raise ExtractionAgentError("agent response was not valid JSON")

    monkeypatch.setattr("app.catalog_routes.call_extraction_agent", _raise)
    pdf = _make_pdf(["fake datasheet text"])
    resp = db_client.post(
        "/ingest/upload",
        data={"equipment_type": "module"},
        files={"file": ("sheet.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["job"]["status"] == "needs_attention"
    assert body["catalog_versions"] == []


def test_document_type_mismatch_rejects_with_422(db_client, monkeypatch):
    # An inverter document uploaded against a module catalog entry.
    resp = _upload(db_client, monkeypatch, _inverter_response(), equipment_type="module")
    assert resp.status_code == 422


def test_approve_promotes_version_while_preserving_history(db_client, monkeypatch):
    upload_resp = _upload(db_client, monkeypatch, _module_response())
    version_id = upload_resp.json()["catalog_versions"][0]["version_id"]

    approve_resp = db_client.post(f"/catalog/versions/{version_id}/approve")
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["version"]["status"] == "active"
    assert body["default_auto_set"] is True
    assert body["current_default_version_id"] == version_id

    # A later upload for the same model creates a NEW pending draft, not a mutation of the approved one.
    newer = _upload(db_client, monkeypatch, _module_response(pmax=705, voc=48.8), filename="rev2.pdf")
    newer_version_id = newer.json()["catalog_versions"][0]["version_id"]
    assert newer_version_id != version_id

    versions_list = db_client.get("/catalog/module/ReneSola/RS9-700/versions").json()
    ids = {v["version_id"]: v["status"] for v in versions_list["versions"]}
    assert ids[version_id] == "active"  # prior version stays traceable, untouched
    assert ids[newer_version_id] == "pending_review"
    assert versions_list["default_version_id"] == version_id  # approving the new one doesn't auto-change this

    # Approving the second version does NOT silently change the default.
    second_approve = db_client.post(f"/catalog/versions/{newer_version_id}/approve").json()
    assert second_approve["default_auto_set"] is False
    assert second_approve["needs_default_choice"] is True
    assert second_approve["current_default_version_id"] == version_id

    set_default = db_client.post(
        f"/catalog/module/ReneSola/RS9-700/set-default", json={"version_id": newer_version_id}
    )
    assert set_default.status_code == 200
    assert set_default.json()["default_version_id"] == newer_version_id


def test_reject_keeps_row_with_rejected_status(db_client, monkeypatch):
    upload_resp = _upload(db_client, monkeypatch, _module_response())
    version_id = upload_resp.json()["catalog_versions"][0]["version_id"]

    reject_resp = db_client.post(f"/catalog/versions/{version_id}/reject")
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "rejected"

    # Kept for audit, not deleted.
    versions_list = db_client.get("/catalog/module/ReneSola/RS9-700/versions").json()
    assert any(v["version_id"] == version_id and v["status"] == "rejected" for v in versions_list["versions"])


def test_unknown_ingestion_job_and_version_404(db_client):
    assert db_client.get("/ingest/does-not-exist").status_code == 404
    assert db_client.post("/ingest/does-not-exist/retry").status_code == 404
    assert db_client.post("/catalog/versions/does-not-exist/approve").status_code == 404
    assert db_client.post("/catalog/versions/does-not-exist/reject").status_code == 404


def test_list_catalog_entries_rejects_unknown_equipment_type(db_client):
    resp = db_client.get("/catalog/widget")
    assert resp.status_code == 422


def test_list_catalog_entries_empty_when_nothing_approved(db_client):
    assert db_client.get("/catalog/module").json() == []


def test_list_catalog_entries_only_includes_approved_defaults(db_client, monkeypatch):
    # Pending draft, never approved -- must not appear in the browse list
    # the HMI's Module/Inverter Spec "Import from catalog" picker calls.
    _upload(db_client, monkeypatch, _module_response(), filename="pending.pdf")
    entries = db_client.get("/catalog/module").json()
    assert entries == []

    upload_resp = _upload(db_client, monkeypatch, _inverter_response(), equipment_type="inverter")
    version_id = upload_resp.json()["catalog_versions"][0]["version_id"]
    db_client.post(f"/catalog/versions/{version_id}/approve")

    inverter_entries = db_client.get("/catalog/inverter").json()
    assert len(inverter_entries) == 1
    assert inverter_entries[0]["manufacturer"] == "Chint Power Systems"
    assert inverter_entries[0]["model"] == "CPS SCH350KTL"
    assert inverter_entries[0]["default_version_id"] == version_id
    assert inverter_entries[0]["fields"]["nameplate_ac_power_kw"]["value"] == 350

    # The module catalog is unaffected by the inverter approval.
    assert db_client.get("/catalog/module").json() == []


def test_list_catalog_entries_reflects_a_later_set_default(db_client, monkeypatch):
    first = _upload(db_client, monkeypatch, _module_response(), filename="v1.pdf")
    first_id = first.json()["catalog_versions"][0]["version_id"]
    db_client.post(f"/catalog/versions/{first_id}/approve")

    second = _upload(db_client, monkeypatch, _module_response(pmax=705, voc=48.8), filename="v2.pdf")
    second_id = second.json()["catalog_versions"][0]["version_id"]
    db_client.post(f"/catalog/versions/{second_id}/approve")
    db_client.post("/catalog/module/ReneSola/RS9-700/set-default", json={"version_id": second_id})

    entries = db_client.get("/catalog/module").json()
    assert len(entries) == 1
    assert entries[0]["default_version_id"] == second_id
    assert entries[0]["fields"]["voc_v"]["value"] == 48.8
