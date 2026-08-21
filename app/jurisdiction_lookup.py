"""NEC edition resolution by state/county. Stub table — 4 states covered;
everything else falls back to "confirm with AHJ" unless the caller supplies
a manual nec_edition_override (the HMI's Jurisdiction panel is now a direct
write-in, not a lookup display, so this is the common path in practice).
"""

from __future__ import annotations

NEC_EDITION_BY_STATE: dict[str, str] = {
    "CA": "NEC 2022 (CEC, based on NEC 2020)",
    "TX": "NEC 2023",
    "AZ": "NEC 2020",
    "NV": "NEC 2023",
}


def resolve_nec_edition(
    state: str, county: str = "", ahj_override: str = "", nec_edition_override: str = ""
) -> dict:
    edition = nec_edition_override or NEC_EDITION_BY_STATE.get(state)

    if not edition:
        return {
            "resolved": False,
            "nec_edition": "UNKNOWN — confirm with AHJ",
            "ahj_name": ahj_override or f"Unresolved — {state}",
        }

    return {
        "resolved": True,
        "nec_edition": edition,
        "ahj_name": ahj_override or (f"{county}, {state}" if county else state),
    }
