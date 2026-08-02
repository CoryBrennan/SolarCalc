from app.document_header import build_document_header, missing_header_fields
from app.models import ClientInfo, ContactEntry, SiteConfig


def test_build_document_header_flattens_fields():
    site = SiteConfig(project_name="REE ESTL Landfill", e911_address="1601 Depot Ave")
    client_info = ClientInfo(
        business_name="Azimuth Energy",
        business_address="123 Main St",
        contacts=[ContactEntry(name="Cory Brennan", email="cory@azimuth.energy")],
    )
    header = build_document_header(site, client_info, ahj_name="St. Clair, IL", nec_edition="NEC 2023")

    assert header["project_name"] == "REE ESTL Landfill"
    assert header["ahj_name"] == "St. Clair, IL"
    assert header["nec_edition"] == "NEC 2023"
    assert header["contacts"] == [{"name": "Cory Brennan", "title": "", "email": "cory@azimuth.energy", "phone": ""}]


def test_missing_header_fields_flags_gaps():
    header = build_document_header(SiteConfig(e911_address=""), ClientInfo(), ahj_name="", nec_edition="")
    missing = missing_header_fields(header)
    assert "e911_address" in missing
    assert "business_name" in missing
    assert "contacts" in missing
    assert "logo" in missing


def test_complete_header_has_no_gaps():
    site = SiteConfig(project_name="X", e911_address="Y")
    client_info = ClientInfo(
        business_name="Azimuth Energy",
        business_address="123 Main St",
        logo_data_uri="data:image/png;base64,xyz",
        contacts=[ContactEntry(name="Cory Brennan")],
    )
    header = build_document_header(site, client_info, ahj_name="IL", nec_edition="NEC 2023")
    assert missing_header_fields(header) == []
