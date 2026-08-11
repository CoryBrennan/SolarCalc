"""Voltage drop check and auto-upsize for a single circuit segment.

Uses a single-conductor three-phase VD formula (VD = sqrt(3) x K x I x L / CM,
K = 12.9 ohm-cmil/ft for copper, 21.2 ohm-cmil/ft for aluminum — the DC
resistivity ratio between the two metals, ~1.64x) — a simplified
single-point calculation, not a full per-segment topology model.
"""

from __future__ import annotations

import math

from app.conductor_tables import CIRCULAR_MILS, CONDUCTOR_ORDER

K_COPPER_OHM_CMIL_PER_FT = 12.9
K_ALUMINUM_OHM_CMIL_PER_FT = 21.2
K_OHM_CMIL_PER_FT: dict[str, float] = {"CU": K_COPPER_OHM_CMIL_PER_FT, "AL": K_ALUMINUM_OHM_CMIL_PER_FT}


def _vd_percent(conductor: str, current_a: float, length_ft: float, voltage_v: float, material: str = "CU") -> tuple[float, float]:
    cm = CIRCULAR_MILS[conductor]
    k = K_OHM_CMIL_PER_FT.get(material, K_COPPER_OHM_CMIL_PER_FT)
    vd_volts = (math.sqrt(3) * k * current_a * length_ft) / cm
    return vd_volts, (vd_volts / voltage_v) * 100


def check_segment(
    current_a: float,
    length_ft: float,
    voltage_v: float,
    limit_pct: float,
    conductor: str,
    material: str = "CU",
) -> dict:
    if conductor not in CIRCULAR_MILS:
        conductor = "4/0 AWG"

    vd_volts, pct = _vd_percent(conductor, current_a, length_ft, voltage_v, material)
    final_conductor = conductor
    upsized = False

    if pct > limit_pct:
        idx = CONDUCTOR_ORDER.index(conductor)
        for candidate in CONDUCTOR_ORDER[idx + 1:]:
            candidate_volts, candidate_pct = _vd_percent(candidate, current_a, length_ft, voltage_v, material)
            if candidate_pct <= limit_pct:
                vd_volts, pct = candidate_volts, candidate_pct
                final_conductor = candidate
                upsized = True
                break
        # If no candidate clears the limit, vd_volts/pct/final_conductor stay at
        # the *starting* conductor's values, not the largest one tried (750
        # kcmil) — matches the original JS exactly, not an oversight here.

    return {
        "starting_conductor": conductor,
        "final_conductor": final_conductor,
        "upsized": upsized,
        "voltage_drop_v": round(vd_volts, 2),
        "voltage_drop_pct": round(pct, 2),
        "passes": pct <= limit_pct,
    }
