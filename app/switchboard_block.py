"""Generator input contract for the AC switchboard AutoCAD Electrical block
(ac-switchboard-addin's SwitchboardConfig/MainBreaker/Position C# classes).

Field names are snake_case on the wire and map onto the C# PascalCase
properties via JsonNamingPolicy.SnakeCaseLower on the add-in side — no field
renaming needed on either end beyond that one naming-policy setting.
"""

from __future__ import annotations


def build_switchboard_config(
    tag: str,
    inverter_phases: int,
    busbar_rating_a: float,
    main_rating_a: float,
    num_inverters: int,
    inverter_ocpd_standard_size_a: int,
    backfeed_total_a: float,
) -> dict:
    phase_config = "3PH" if inverter_phases == 3 else "1PH"
    phase_labels = ["A", "B", "C"] if inverter_phases == 3 else ["A", "B"]

    positions = [
        {
            "position_number": i,
            "type": "inverter",
            "tag": f"INV-{i}",
            "breaker_rating": f"{inverter_ocpd_standard_size_a:g}A",
            "phase": phase_labels,
        }
        for i in range(1, num_inverters + 1)
    ]

    return {
        "tag": tag,
        "phase_config": phase_config,
        "main_breaker": {
            "rating": f"{main_rating_a:g}A",
            # AIC/interrupting rating needs a full arc-flash/short-circuit
            # study this backend doesn't perform — flagged, not guessed.
            "aic_rating": "TBD — requires arc-flash study",
        },
        "bus_rating_amps": int(busbar_rating_a),
        "positions": positions,
        "backfeed_total_amps": int(backfeed_total_a),
    }
