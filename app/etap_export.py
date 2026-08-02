"""One-line equipment list for the arc-flash / protective-device coordination
study — a generic source list to map into ETAP's own Excel-import template,
not a file ETAP accepts directly. Assumes a flat inverter -> POI-switchgear
topology; a site with intermediate pad-mount transformers or MV collection
switchgear needs those buses/cables added by hand.
"""

from __future__ import annotations

from app.models import EtapAssumptions

INVERTER_KV = 0.8  # CPS SCH350KTL-DO/US-800 rated output voltage


def build_etap_export(num_inverters: int, inverter_ac_rating_w: float, assumptions: EtapAssumptions) -> dict:
    inverter_mva = round((inverter_ac_rating_w / 1_000_000) * 10000) / 10000

    rows = []
    for i in range(1, num_inverters + 1):
        bus_id = f"INV-{i:02d}-AC"
        rows.append(
            {
                "bus_id": bus_id, "kv": INVERTER_KV, "cable_id": f"C-INV{i:02d}",
                "length_ft": assumptions.length_ft, "conductor": assumptions.conductor,
                "ocpd_type": assumptions.ocpd_type, "ocpd_rating_a": assumptions.ocpd_rating_a,
                "upstream_bus": "POI-SWGR", "downstream_bus": bus_id, "source_mva": inverter_mva,
            }
        )

    total_mva = round(inverter_mva * num_inverters * 1000) / 1000
    rows.append(
        {
            "bus_id": "POI-SWGR", "kv": INVERTER_KV, "cable_id": None, "length_ft": 0,
            "conductor": None, "ocpd_type": "utility_disconnect", "ocpd_rating_a": 0,
            "upstream_bus": "UTILITY", "downstream_bus": "POI-SWGR", "source_mva": total_mva,
        }
    )

    return {"rows": rows, "total_source_mva_at_poi": total_mva}
