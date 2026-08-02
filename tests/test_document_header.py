from app.document_header import build_document_header, missing_header_fields
from app.models import ClientInfo, ContactEntry, SiteAddress, SiteConfig


def test_build_document_header_flattens_fields():
    site = SiteConfig(
        project_name="REE ESTL Landfill",
        utility_name="Ameren Illinois",
        address=SiteAddress(street="1601 Depot Ave", city="East St. Louis", state="IL", zip="62201"),
    )
    client_info = ClientInfo(
        business_name="Azimuth Energy",
        business_address="123 Main St",
        contacts=[ContactEntry(name="Cory Brennan", email="cory@azimuth.energy")],
    )
    header = build_document_header(site, client_info, ahj_name="St. Clair, IL", nec_edition="NEC 2023")

    assert header["project_name"] == "REE ESTL Landfill"
    assert header["utility_name"] == "Ameren Illinois"
    assert header["site_address"] == "1601 Depot Ave, East St. Louis, IL 62201"
    assert header["ahj_name"] == "St. Clair, IL"
    assert header["nec_edition"] == "NEC 2023"
    assert header["contacts"] == [
        {"name": "Cory Brennan", "role": "", "title": "", "email": "cory@azimuth.energy", "phone": ""}
    ]


def test_missing_header_fields_flags_gaps():
    header = build_document_header(SiteConfig(address=SiteAddress(state="")), ClientInfo(), ahj_name="", nec_edition="")
    missing = missing_header_fields(header)
    assert "site_address" in missing
    assert "business_name" in missing
    assert "contacts" in missing
    assert "logo" in missing


def test_complete_header_has_no_gaps():
    site = SiteConfig(project_name="X", address=SiteAddress(street="Y"))
    client_info = ClientInfo(
        business_name="Azimuth Energy",
        business_address="123 Main St",
        logo_data_uri="data:image/png;base64,xyz",
        contacts=[ContactEntry(name="Cory Brennan")],
    )
    header = build_document_header(site, client_info, ahj_name="IL", nec_edition="NEC 2023")
    assert missing_header_fields(header) == []
