"""Regression baseline: 15 inverters + 1 POI row (16 total) verified live in
the HMI draft's browser console this session.
"""

from app.etap_export import build_etap_export
from app.models import EtapAssumptions


def test_default_project_matches_browser_verified():
    result = build_etap_export(num_inverters=15, inverter_ac_rating_w=350_000, assumptions=EtapAssumptions())
    assert len(result["rows"]) == 16
    assert result["total_source_mva_at_poi"] == 5.25

    poi_row = result["rows"][-1]
    assert poi_row["bus_id"] == "POI-SWGR"
    assert poi_row["source_mva"] == 5.25

    first_inverter_row = result["rows"][0]
    assert first_inverter_row["bus_id"] == "INV-01-AC"
    assert first_inverter_row["source_mva"] == 0.35
    assert first_inverter_row["upstream_bus"] == "POI-SWGR"
