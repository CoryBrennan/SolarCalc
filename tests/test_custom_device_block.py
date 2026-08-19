"""Unit tests for the custom device config builder — terminal group
expansion, phase-label subset selection, comms protocol selection, and the
validation errors (HTTPException 422) bad input should raise. Uses the same
two example templates as app/device_templates.py's seeded starter set,
built inline here rather than imported so template shape stays a visible
part of each test.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.custom_device_block import build_custom_device_config, validate_custom_device_tags
from app.models import (
    MAX_TERMINALS_PER_GROUP,
    AuxLoadCircuit,
    AuxPanelboardConfig,
    CustomDeviceInstance,
    CustomDeviceTerminalConnection,
    DeviceTemplate,
    InverterSpec,
    ProjectInput,
    TerminalGroupSpec,
)


def _inverter_combiner_template() -> DeviceTemplate:
    return DeviceTemplate(
        id="tpl-inv",
        name="Inverter w/ DC Combiner",
        terminal_groups=[
            TerminalGroupSpec(
                id="ac_input", label="AC Input", terminal_type="ac_phase", count=3,
                phase_labels=["L1-A", "L2-B", "L3-C"], connects_to_types=["breaker"],
            ),
            TerminalGroupSpec(
                id="neutral", label="Neutral", terminal_type="neutral", count=1, optional=True,
                connects_to_types=["neutral_bar"],
            ),
            TerminalGroupSpec(
                id="ground", label="Ground", terminal_type="ground", count=1, count_mode="one_or_more",
                connects_to_types=["ground_bar"],
            ),
            TerminalGroupSpec(
                id="comms", label="Communications", terminal_type="comms", count=1, count_mode="one_or_more",
                protocol_options=["RS485", "Ethernet"], connects_to_types=["comms"],
            ),
            TerminalGroupSpec(
                id="dc_input", label="DC Input", terminal_type="dc_generic", count=2,
                connects_to_types=["generic"],
            ),
        ],
    )


def _split_phase_template() -> DeviceTemplate:
    return DeviceTemplate(
        id="tpl-split",
        name="Split-Phase Load",
        terminal_groups=[
            TerminalGroupSpec(
                id="ac_input", label="AC Input", terminal_type="ac_phase", count=2,
                phase_labels=["L1-A", "L2-B", "L3-C"], connects_to_types=["breaker"],
            ),
            TerminalGroupSpec(
                id="neutral", label="Neutral", terminal_type="neutral", count=1, optional=True,
                connects_to_types=["neutral_bar"],
            ),
            TerminalGroupSpec(
                id="ground", label="Ground", terminal_type="ground", count=1, connects_to_types=["ground_bar"],
            ),
        ],
    )


def _comms_ok() -> list[CustomDeviceTerminalConnection]:
    """A resolved comms choice, so tests targeting a different group aren't
    incidentally tripped by the inverter template's 2-option comms group."""
    return [CustomDeviceTerminalConnection(group_id="comms", index=0, protocol="RS485")]


def test_inverter_combiner_expands_to_eight_terminals_at_minimums():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-CUSTOM-1",
        template_id=template.id,
        connections=[
            CustomDeviceTerminalConnection(group_id="ac_input", index=0, connects_to="AUX-1/CKT-1"),
            *_comms_ok(),
        ],
    )
    config = build_custom_device_config(instance, template)
    assert config["tag"] == "INV-CUSTOM-1"
    assert config["device_type_name"] == "Inverter w/ DC Combiner"
    # 3 AC + 1 neutral (default present) + 1 ground (min) + 1 comms (min) + 2 DC = 8
    assert len(config["terminals"]) == 8

    ac_terminals = [t for t in config["terminals"] if t["group"] == "ac_input"]
    assert [t["label"] for t in ac_terminals] == ["L1-A", "L2-B", "L3-C"]
    assert ac_terminals[0]["connects_to"] == "AUX-1/CKT-1"
    assert ac_terminals[1]["connects_to"] is None


def test_optional_neutral_omitted_when_count_zero():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-2", template_id=template.id, group_counts={"neutral": 0}, connections=_comms_ok()
    )
    config = build_custom_device_config(instance, template)
    assert not any(t["group"] == "neutral" for t in config["terminals"])
    assert len(config["terminals"]) == 7  # 8 minus the omitted neutral


def test_one_or_more_ground_can_be_overridden_up():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-3", template_id=template.id, group_counts={"ground": 3}, connections=_comms_ok()
    )
    config = build_custom_device_config(instance, template)
    ground_terminals = [t for t in config["terminals"] if t["group"] == "ground"]
    assert [t["label"] for t in ground_terminals] == ["Ground 1", "Ground 2", "Ground 3"]


def test_one_or_more_below_minimum_raises_422():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(tag="INV-4", template_id=template.id, group_counts={"ground": 0})
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_one_or_more_over_cap_raises_422():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-5", template_id=template.id, group_counts={"ground": MAX_TERMINALS_PER_GROUP + 1}
    )
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_fixed_group_count_mismatch_raises_422():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-6", template_id=template.id, group_counts={"dc_input": 1}, connections=_comms_ok()
    )
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_required_group_cannot_be_zeroed():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-7", template_id=template.id, group_counts={"dc_input": 0}, connections=_comms_ok()
    )
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_split_phase_requires_explicit_phase_choice():
    template = _split_phase_template()
    instance = CustomDeviceInstance(tag="LOAD-1", template_id=template.id)
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_split_phase_picks_the_chosen_pair():
    template = _split_phase_template()
    instance = CustomDeviceInstance(
        tag="LOAD-2",
        template_id=template.id,
        connections=[
            CustomDeviceTerminalConnection(
                group_id="ac_input", index=0, phase_label_override="L2-B", connects_to="AUX-1/CKT-1"
            ),
            CustomDeviceTerminalConnection(
                group_id="ac_input", index=1, phase_label_override="L3-C", connects_to="AUX-1/CKT-1"
            ),
        ],
    )
    config = build_custom_device_config(instance, template)
    ac = [t for t in config["terminals"] if t["group"] == "ac_input"]
    assert [t["label"] for t in ac] == ["L2-B", "L3-C"]


def test_split_phase_duplicate_choice_raises_422():
    template = _split_phase_template()
    instance = CustomDeviceInstance(
        tag="LOAD-3",
        template_id=template.id,
        connections=[
            CustomDeviceTerminalConnection(group_id="ac_input", index=0, phase_label_override="L1-A"),
            CustomDeviceTerminalConnection(group_id="ac_input", index=1, phase_label_override="L1-A"),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_comms_protocol_must_be_chosen_when_multiple_options():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(tag="INV-8", template_id=template.id)
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_comms_protocol_selected_and_connects_to_passes_through():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(
        tag="INV-9",
        template_id=template.id,
        connections=[CustomDeviceTerminalConnection(group_id="comms", index=0, protocol="RS485", connects_to="DAS-1")],
    )
    config = build_custom_device_config(instance, template)
    comms = [t for t in config["terminals"] if t["group"] == "comms"][0]
    assert comms["label"] == "RS485"
    assert comms["connects_to"] == "DAS-1"


def test_instance_template_mismatch_raises_422():
    template = _inverter_combiner_template()
    instance = CustomDeviceInstance(tag="X", template_id="wrong-id")
    with pytest.raises(HTTPException) as exc:
        build_custom_device_config(instance, template)
    assert exc.value.status_code == 422


def test_attributes_pass_through_untouched():
    template = _split_phase_template()
    instance = CustomDeviceInstance(
        tag="LOAD-4",
        template_id=template.id,
        group_counts={"neutral": 0},
        connections=[
            CustomDeviceTerminalConnection(group_id="ac_input", index=0, phase_label_override="L1-A"),
            CustomDeviceTerminalConnection(group_id="ac_input", index=1, phase_label_override="L2-B"),
        ],
        attributes={"MFG": "GENERIC", "CAT": "SPLIT-PHASE-1"},
    )
    config = build_custom_device_config(instance, template)
    assert config["attributes"] == {"MFG": "GENERIC", "CAT": "SPLIT-PHASE-1"}
    assert len(config["terminals"]) == 3  # 2 AC + 1 ground, neutral omitted


def test_validate_custom_device_tags_rejects_inverter_collision():
    project = ProjectInput(
        inverter=InverterSpec(quantity=2),
        custom_devices=[CustomDeviceInstance(tag="INV-2", template_id="tpl-x")],
    )
    with pytest.raises(HTTPException) as exc:
        validate_custom_device_tags(project)
    assert exc.value.status_code == 422


def test_validate_custom_device_tags_rejects_aux_circuit_collision():
    project = ProjectInput(
        aux_panelboard=AuxPanelboardConfig(circuits=[AuxLoadCircuit(position=1, circuit_tag="CKT-1")]),
        custom_devices=[CustomDeviceInstance(tag="CKT-1", template_id="tpl-x")],
    )
    with pytest.raises(HTTPException) as exc:
        validate_custom_device_tags(project)
    assert exc.value.status_code == 422


def test_validate_custom_device_tags_rejects_duplicate_custom_tags():
    project = ProjectInput(
        custom_devices=[
            CustomDeviceInstance(tag="LOAD-1", template_id="tpl-x"),
            CustomDeviceInstance(tag="LOAD-1", template_id="tpl-y"),
        ],
    )
    with pytest.raises(HTTPException) as exc:
        validate_custom_device_tags(project)
    assert exc.value.status_code == 422


def test_validate_custom_device_tags_passes_for_distinct_tags():
    project = ProjectInput(
        custom_devices=[
            CustomDeviceInstance(tag="LOAD-1", template_id="tpl-x"),
            CustomDeviceInstance(tag="LOAD-2", template_id="tpl-y"),
        ],
    )
    validate_custom_device_tags(project)  # must not raise
