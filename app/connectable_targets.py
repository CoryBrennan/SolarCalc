"""Live registry of tags a custom device's terminal can point at — the
structured picker's data source (app/device_template_routes.py's
GET /connectable-targets).

Deliberately narrow rather than exhaustive: only sources that are real,
already-modeled objects on the current project. Two known gaps, both
flagged rather than papered over — see the module docstring notes below and
custom-device-block-spec.md's Open items:

1. The switchboard has no manual/non-inverter breaker positions today —
   switchboard_block.py always synthesizes exactly one INV-{i} position per
   inverter, 1:1 with inverter.quantity. Those are inverter-owned feeds, not
   available connection points, so they are NOT offered here. Until the
   switchboard model supports adding a manual position, the Aux Panelboard
   (which already models arbitrary user-tagged circuits) is the only real
   "breaker" source.
2. Neither the switchboard nor the aux panelboard models a distinct
   ground/neutral bar object — each panel is assumed to have exactly one of
   each. The synthesized entries below say so in their `note`, so the
   picker surfaces the assumption rather than hiding it.
"""

from __future__ import annotations

from app.models import ConnectableCategory, ProjectInput

# Matches the tag changeset_routes.py hardcodes when building the
# switchboard config (SwitchboardConfig itself carries no tag field) —
# kept here as the one other place that needs to agree on it.
SWITCHBOARD_TAG = "SWBD-1"


def _bus_bar_entries(panel_tag: str) -> list[dict]:
    return [
        {
            "tag": f"{panel_tag}/GND_BUS",
            "label": f"{panel_tag} Ground Bus",
            "category": "ground_bar",
            "note": "Assumed single ground bus for this panel — not an individually modeled object.",
        },
        {
            "tag": f"{panel_tag}/NEUTRAL_BUS",
            "label": f"{panel_tag} Neutral Bus",
            "category": "neutral_bar",
            "note": "Assumed single neutral bus for this panel — not an individually modeled object.",
        },
    ]


def list_connectable_targets(project: ProjectInput, category: ConnectableCategory | None = None) -> list[dict]:
    """Every entry is {tag, label, category, note}. `note` is None for real
    modeled objects (aux panelboard circuits, other custom devices) and set
    for synthesized/assumed entries (bus bars) — the picker should surface
    it either way."""
    entries: list[dict] = []

    for circuit in project.aux_panelboard.circuits:
        entries.append(
            {
                "tag": f"{project.aux_panelboard.tag}/{circuit.circuit_tag}",
                "label": f"{project.aux_panelboard.tag} — {circuit.circuit_tag} ({circuit.description or 'no description'})",
                "category": "breaker",
                "note": None,
            }
        )

    entries.extend(_bus_bar_entries(SWITCHBOARD_TAG))
    entries.extend(_bus_bar_entries(project.aux_panelboard.tag))

    for device in project.custom_devices:
        entries.append(
            {
                "tag": device.tag,
                "label": f"{device.tag} (custom device)",
                "category": "other_device",
                "note": None,
            }
        )
        entries.append(
            {
                "tag": device.tag,
                "label": f"{device.tag} (custom device)",
                "category": "comms",
                "note": None,
            }
        )

    if category is not None:
        entries = [e for e in entries if e["category"] == category]

    return entries
