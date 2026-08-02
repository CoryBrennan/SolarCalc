"""Generator input contract for the auxiliary load panelboard AutoCAD
Electrical block — variable circuit position count, same code-generation
pattern as the switchboard (one box per position at fixed pitch).
"""

from __future__ import annotations

from app.models import AuxPanelboardConfig


def build_aux_panelboard_config(config: AuxPanelboardConfig) -> dict:
    return {
        "tag": config.tag,
        "main_breaker_rating": f"{config.main_breaker_rating_a:g}A",
        "voltage": config.voltage,
        "phase": config.phase,
        "positions": [
            {
                "position": c.position,
                "circuit_tag": c.circuit_tag,
                "breaker_rating": f"{c.breaker_rating_a:g}A",
                "description": c.description,
            }
            for c in config.circuits
        ],
    }
