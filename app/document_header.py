"""Flattens project/client info into one header object any output document
(calc report, drawing title block, quote) can consume directly.
"""

from __future__ import annotations

from app.models import ClientInfo, SiteConfig

_REQUIRED_FIELDS = ["project_name", "e911_address", "business_name", "business_address"]


def build_document_header(site: SiteConfig, client_info: ClientInfo, ahj_name: str, nec_edition: str) -> dict:
    return {
        "project_name": site.project_name,
        "e911_address": site.e911_address,
        "ahj_name": ahj_name,
        "nec_edition": nec_edition,
        "business_name": client_info.business_name,
        "business_address": client_info.business_address,
        "website": client_info.website,
        "logo_data_uri": client_info.logo_data_uri,
        "contacts": [c.model_dump() for c in client_info.contacts],
    }


def missing_header_fields(header: dict) -> list[str]:
    missing = [field for field in _REQUIRED_FIELDS if not header.get(field)]
    if not header.get("contacts"):
        missing.append("contacts")
    if not header.get("logo_data_uri"):
        missing.append("logo")
    return missing
