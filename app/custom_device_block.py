"""Generator input contract for the custom device AutoCAD Electrical block
(ac-switchboard-addin's CustomDeviceConfig/TerminalInstance C# classes).

Expands one DeviceTemplate's terminal_groups, applied to one
CustomDeviceInstance's per-group overrides and per-terminal connections,
into the flat terminal list the generic CAD generator lays out. Variable
terminal composition is structural (same reasoning as the switchboard's
Positions list), so this always emits a "regenerate" changeset — see the
plan's Architecture note on why a hybrid attribute_update path was
rejected.

Validation raises HTTPException(422) directly, matching the only other
cross-reference validator in this codebase
(project_calc.validate_module_skus) rather than a plain exception the route
would have to translate.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.models import MAX_TERMINALS_PER_GROUP, CustomDeviceInstance, DeviceTemplate, ProjectInput, TerminalGroupSpec


def validate_custom_device_tags(project: ProjectInput) -> None:
    """Rejects a custom_devices tag that collides with any other tag
    already in use on the project. Not about CAD block-name collisions —
    each block type already gets its own name prefix (CUSTOM_DEVICE_{Tag}
    vs AC_SWITCHBOARD_{Tag} vs STATIC_DEVICE_{Tag}) — this is about the
    drawing tag itself being ambiguous to a reader of the drawing set."""
    reserved = {f"INV-{i}" for i in range(1, project.inverter.quantity + 1)}
    reserved |= {circuit.circuit_tag for circuit in project.aux_panelboard.circuits}

    seen: set[str] = set()
    for device in project.custom_devices:
        if device.tag in reserved:
            raise HTTPException(
                422, f"Custom device tag {device.tag!r} is already in use by an inverter or aux panelboard circuit."
            )
        if device.tag in seen:
            raise HTTPException(422, f"Custom device tag {device.tag!r} is used by more than one device.")
        seen.add(device.tag)


def _effective_count(group: TerminalGroupSpec, instance: CustomDeviceInstance) -> int:
    requested = instance.group_counts.get(group.id, group.count)

    if requested == 0:
        if not group.optional:
            raise HTTPException(
                422, f"Terminal group {group.id!r} is required and cannot have a count of 0."
            )
        return 0

    if group.count_mode == "fixed" and requested != group.count:
        raise HTTPException(
            422,
            f"Terminal group {group.id!r} has a fixed count of {group.count}, got {requested}.",
        )

    if group.count_mode == "one_or_more":
        if requested < group.count:
            raise HTTPException(
                422,
                f"Terminal group {group.id!r} needs at least {group.count} terminal(s), got {requested}.",
            )
        if requested > MAX_TERMINALS_PER_GROUP:
            raise HTTPException(
                422,
                f"Terminal group {group.id!r} requested {requested} terminals, "
                f"exceeding the {MAX_TERMINALS_PER_GROUP}-terminal cap per group.",
            )

    return requested


def _phase_labels_for_group(
    group: TerminalGroupSpec, count: int, connections_by_index: dict[int, object]
) -> list[str]:
    candidates = group.phase_labels or []
    if count == len(candidates):
        # Every candidate is used — positional order unless a specific
        # terminal's override disagrees (still validated below).
        labels = list(candidates)
        for index, conn in connections_by_index.items():
            override = getattr(conn, "phase_label_override", None)
            if override is not None:
                if override not in candidates:
                    raise HTTPException(
                        422, f"{override!r} is not one of {group.id!r}'s phase_labels {candidates}."
                    )
                labels[index] = override
        return labels

    # Fewer terminals than candidates (e.g. split-phase choosing 2 of 3) —
    # every terminal needs an explicit choice, since positional slicing
    # would always yield the same subset and hide the other valid pairs.
    labels = []
    for index in range(count):
        conn = connections_by_index.get(index)
        override = getattr(conn, "phase_label_override", None) if conn else None
        if override is None:
            raise HTTPException(
                422,
                f"{group.id!r} offers {len(candidates)} phase options for {count} terminal(s) — "
                f"terminal {index} needs an explicit phase_label_override.",
            )
        if override not in candidates:
            raise HTTPException(
                422, f"{override!r} is not one of {group.id!r}'s phase_labels {candidates}."
            )
        labels.append(override)
    if len(set(labels)) != len(labels):
        raise HTTPException(422, f"{group.id!r}'s phase_label_override values must be distinct, got {labels}.")
    return labels


def build_custom_device_config(instance: CustomDeviceInstance, template: DeviceTemplate) -> dict:
    if instance.template_id != template.id:
        raise HTTPException(
            422,
            f"Instance {instance.tag!r} references template {instance.template_id!r}, "
            f"not the provided template {template.id!r}.",
        )

    terminals: list[dict] = []

    for group in template.terminal_groups:
        count = _effective_count(group, instance)
        if count == 0:
            continue

        connections_by_index = {
            c.index: c for c in instance.connections if c.group_id == group.id
        }

        phase_labels: list[str] | None = None
        if group.terminal_type == "ac_phase":
            phase_labels = _phase_labels_for_group(group, count, connections_by_index)

        for index in range(count):
            conn = connections_by_index.get(index)

            if group.terminal_type == "ac_phase" and phase_labels is not None:
                label = phase_labels[index]
            elif group.terminal_type == "comms":
                protocol = getattr(conn, "protocol", None) if conn else None
                if group.protocol_options:
                    if protocol is None:
                        if len(group.protocol_options) == 1:
                            protocol = group.protocol_options[0]
                        else:
                            raise HTTPException(
                                422,
                                f"{group.id!r} terminal {index} needs a protocol choice from "
                                f"{group.protocol_options}.",
                            )
                    elif protocol not in group.protocol_options:
                        raise HTTPException(
                            422,
                            f"{protocol!r} is not one of {group.id!r}'s protocol_options {group.protocol_options}.",
                        )
                label = protocol or group.label
            else:
                label = group.label if count == 1 else f"{group.label} {index + 1}"

            terminals.append(
                {
                    "id": f"{group.id.upper()}_{index + 1}",
                    "group": group.id,
                    "terminal_type": group.terminal_type,
                    "label": label,
                    "connects_to": (conn.connects_to if conn else None),
                }
            )

    return {
        "tag": instance.tag,
        "device_type_name": template.name,
        "terminals": terminals,
        "attributes": instance.attributes,
    }
