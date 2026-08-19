"""CRUD for reusable custom-device templates (DeviceTemplateRow), plus the
two starter templates seeded on first run so the builder panel isn't empty.

Mirrors app/changesets.py's shape: plain session-taking functions, no class,
JSON-encode/decode at the boundary between the Pydantic model
(app/models.py's DeviceTemplate) and the DB row (app/db_models.py's
DeviceTemplateRow).
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app.db_models import DeviceTemplateRow
from app.models import DeviceTemplate, TerminalGroupSpec


def _row_to_template(row: DeviceTemplateRow) -> DeviceTemplate:
    return DeviceTemplate.model_validate(json.loads(row.data))


def create_template(session: Session, template: DeviceTemplate) -> DeviceTemplate:
    row = DeviceTemplateRow(data=template.model_dump_json(exclude={"id"}))
    session.add(row)
    session.commit()
    session.refresh(row)
    return _row_to_template(row).model_copy(update={"id": row.id})


def get_template(session: Session, template_id: str) -> DeviceTemplate | None:
    row = session.get(DeviceTemplateRow, template_id)
    if row is None:
        return None
    return _row_to_template(row).model_copy(update={"id": row.id})


def list_templates(session: Session) -> list[DeviceTemplate]:
    rows = session.exec(select(DeviceTemplateRow).order_by(DeviceTemplateRow.updated_at)).all()
    return [_row_to_template(row).model_copy(update={"id": row.id}) for row in rows]


def update_template(session: Session, template_id: str, template: DeviceTemplate) -> DeviceTemplate | None:
    row = session.get(DeviceTemplateRow, template_id)
    if row is None:
        return None
    row.data = template.model_dump_json(exclude={"id"})
    session.add(row)
    session.commit()
    session.refresh(row)
    return _row_to_template(row).model_copy(update={"id": row.id})


def delete_template(session: Session, template_id: str) -> bool:
    row = session.get(DeviceTemplateRow, template_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _inverter_with_combiner_template() -> DeviceTemplate:
    return DeviceTemplate(
        name="Inverter w/ DC Combiner",
        terminal_groups=[
            TerminalGroupSpec(
                id="ac_input",
                label="AC Input",
                terminal_type="ac_phase",
                count=3,
                count_mode="fixed",
                phase_labels=["L1-A", "L2-B", "L3-C"],
                connects_to_types=["breaker"],
            ),
            TerminalGroupSpec(
                id="neutral",
                label="Neutral",
                terminal_type="neutral",
                count=1,
                count_mode="fixed",
                optional=True,
                connects_to_types=["neutral_bar"],
            ),
            TerminalGroupSpec(
                id="ground",
                label="Ground",
                terminal_type="ground",
                count=1,
                count_mode="one_or_more",
                connects_to_types=["ground_bar"],
            ),
            TerminalGroupSpec(
                id="comms",
                label="Communications",
                terminal_type="comms",
                count=1,
                count_mode="one_or_more",
                protocol_options=["RS485", "Ethernet"],
                connects_to_types=["comms", "other_device"],
            ),
            TerminalGroupSpec(
                id="dc_input",
                label="DC Input",
                terminal_type="dc_generic",
                count=2,
                count_mode="fixed",
                connects_to_types=["other_device", "generic"],
            ),
        ],
    )


def _split_phase_load_template() -> DeviceTemplate:
    return DeviceTemplate(
        name="Split-Phase Load",
        terminal_groups=[
            TerminalGroupSpec(
                id="ac_input",
                label="AC Input",
                terminal_type="ac_phase",
                count=2,
                count_mode="fixed",
                # Three candidates, instance picks which 2 — L1-A/L2-B,
                # L2-B/L3-C, or L1-A/L3-C, matching the user's example.
                phase_labels=["L1-A", "L2-B", "L3-C"],
                connects_to_types=["breaker"],
            ),
            TerminalGroupSpec(
                id="neutral",
                label="Neutral",
                terminal_type="neutral",
                count=1,
                count_mode="fixed",
                optional=True,
                connects_to_types=["neutral_bar"],
            ),
            TerminalGroupSpec(
                id="ground",
                label="Ground",
                terminal_type="ground",
                count=1,
                count_mode="fixed",
                connects_to_types=["ground_bar"],
            ),
        ],
    )


def seed_default_templates(session: Session) -> None:
    """Creates the two example templates if no templates exist yet.
    Idempotent by "table is empty", not by name, so it never overwrites or
    duplicates templates a user has already created or edited."""
    existing = session.exec(select(DeviceTemplateRow).limit(1)).first()
    if existing is not None:
        return
    create_template(session, _inverter_with_combiner_template())
    create_template(session, _split_phase_load_template())
