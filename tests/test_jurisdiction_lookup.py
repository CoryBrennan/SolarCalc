"""Regression baseline: IL (unresolved) and CA (resolved) verified live in
the HMI draft's browser console this session.
"""

from app.jurisdiction_lookup import resolve_nec_edition


def test_illinois_unresolved_matches_browser_verified():
    result = resolve_nec_edition(state="IL", county="St. Clair")
    assert result["resolved"] is False
    assert result["nec_edition"] == "UNKNOWN — confirm with AHJ"
    assert result["ahj_name"] == "Unresolved — IL"


def test_california_resolved():
    result = resolve_nec_edition(state="CA", county="Los Angeles")
    assert result["resolved"] is True
    assert result["nec_edition"] == "NEC 2022 (CEC, based on NEC 2020)"
    assert result["ahj_name"] == "Los Angeles, CA"


def test_ahj_override_takes_precedence():
    result = resolve_nec_edition(state="IL", ahj_override="City of East St. Louis")
    assert result["ahj_name"] == "City of East St. Louis"
