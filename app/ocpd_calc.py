"""Overcurrent protective device sizing — 125% of continuous current (690.9 for
PV circuits, 705.12/705.28 for inverter output), rounded to the next NEC
240.6(A) standard size, checked against a manufacturer max where applicable.
"""

from __future__ import annotations

from app.conductor_tables import next_standard_size


def size_ocpd(
    continuous_current_a: float,
    circuit: str,
    manufacturer_max_ocpd_a: float | None = None,
) -> dict:
    min_rating = continuous_current_a * 1.25
    standard_size = next_standard_size(min_rating)

    if circuit == "inverter_output" and manufacturer_max_ocpd_a:
        mfr_ok = standard_size <= manufacturer_max_ocpd_a
        mfr_check = (
            f"OK — within mfr max ({manufacturer_max_ocpd_a:g} A)"
            if mfr_ok
            else f"Exceeds mfr max ({manufacturer_max_ocpd_a:g} A)"
        )
    else:
        mfr_ok = True
        mfr_check = "n/a — no mfr max for this circuit"

    return {
        "min_rating_a": round(min_rating, 2),
        "standard_size_a": standard_size,
        "manufacturer_check_ok": mfr_ok,
        "manufacturer_check": mfr_check,
    }
