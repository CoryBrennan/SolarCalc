"""Separately derived system determination (NEC Art. 100) and bonding/grounding
sizing (250.30(A), 250.28(D)(1), Table 250.66) for an inverter step-up
transformer's secondary.
"""

from __future__ import annotations

import math

from app.conductor_tables import grounding_conductor_size, select_conductor
from app.models import TransformerConfig

_WINDING_LABEL = {"delta": "delta", "wye": "ungrounded wye", "grounded_wye": "grounded wye"}


def is_separately_derived_system(primary_winding: str, secondary_winding: str) -> bool:
    return secondary_winding == "grounded_wye" and primary_winding != "grounded_wye"


def size_bonding_and_grounding(transformer: TransformerConfig) -> dict:
    sds = is_separately_derived_system(transformer.primary_winding, transformer.secondary_winding)

    primary_label = _WINDING_LABEL[transformer.primary_winding]
    secondary_label = _WINDING_LABEL[transformer.secondary_winding]
    if sds:
        explanation = (
            f"A {primary_label} primary feeding a grounded-wye secondary has no shared "
            "neutral back to the primary — NEC 250.30 bonding applies to the secondary."
        )
    else:
        explanation = (
            f"A {primary_label} primary feeding a {secondary_label} secondary shares "
            "grounding continuity with the primary — 250.30 bonding does not apply; "
            "treat it as part of the existing grounded system instead."
        )

    result = {
        "separately_derived_system": sds,
        "explanation": explanation,
        "secondary_flc_a": None,
        "secondary_conductor": None,
        "system_bonding_jumper": None,
        "grounding_electrode_conductor": None,
    }
    if not sds:
        return result

    flc = (transformer.kva * 1000) / (math.sqrt(3) * transformer.secondary_v)
    conductor = select_conductor(flc, 75)
    gec_sbj = grounding_conductor_size(conductor) if conductor else None

    result.update(
        {
            "secondary_flc_a": round(flc, 2),
            "secondary_conductor": conductor,
            "system_bonding_jumper": f"{gec_sbj} Cu" if gec_sbj else None,
            "grounding_electrode_conductor": f"{gec_sbj} Cu" if gec_sbj else None,
        }
    )
    return result
