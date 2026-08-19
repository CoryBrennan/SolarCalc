"""End-to-end custom device API: template CRUD, the connectable-targets
registry, tag-collision rejection, and changeset refresh/dedup — mirrors
test_changeset_api.py's shape for the other block types.
"""

from __future__ import annotations


def _create_inverter_combiner_template(db_client) -> str:
    payload = {
        "name": "Inverter w/ DC Combiner",
        "terminal_groups": [
            {
                "id": "ac_input", "label": "AC Input", "terminal_type": "ac_phase", "count": 3,
                "phase_labels": ["L1-A", "L2-B", "L3-C"], "connects_to_types": ["breaker"],
            },
            {
                "id": "neutral", "label": "Neutral", "terminal_type": "neutral", "count": 1, "optional": True,
                "connects_to_types": ["neutral_bar"],
            },
            {
                "id": "ground", "label": "Ground", "terminal_type": "ground", "count": 1,
                "count_mode": "one_or_more", "connects_to_types": ["ground_bar"],
            },
            {
                "id": "comms", "label": "Communications", "terminal_type": "comms", "count": 1,
                "count_mode": "one_or_more", "protocol_options": ["RS485", "Ethernet"],
                "connects_to_types": ["comms"],
            },
            {
                "id": "dc_input", "label": "DC Input", "terminal_type": "dc_generic", "count": 2,
                "connects_to_types": ["generic"],
            },
        ],
    }
    response = db_client.post("/device-templates", json=payload)
    assert response.status_code == 200
    return response.json()["id"]


def test_create_list_get_update_delete_template(db_client):
    template_id = _create_inverter_combiner_template(db_client)
    assert template_id  # a real prefixed-uuid id, not the empty default

    listed = db_client.get("/device-templates").json()
    assert any(t["id"] == template_id for t in listed)

    fetched = db_client.get(f"/device-templates/{template_id}").json()
    assert fetched["name"] == "Inverter w/ DC Combiner"
    assert len(fetched["terminal_groups"]) == 5

    fetched["name"] = "Inverter w/ DC Combiner (renamed)"
    updated = db_client.put(f"/device-templates/{template_id}", json=fetched)
    assert updated.status_code == 200
    assert updated.json()["name"] == "Inverter w/ DC Combiner (renamed)"

    deleted = db_client.delete(f"/device-templates/{template_id}")
    assert deleted.status_code == 200
    assert db_client.get(f"/device-templates/{template_id}").status_code == 404


def test_unknown_template_404s(db_client):
    assert db_client.get("/device-templates/tpl-doesnotexist").status_code == 404
    assert (
        db_client.put("/device-templates/tpl-doesnotexist", json={"name": "X", "terminal_groups": []}).status_code
        == 404
    )
    assert db_client.delete("/device-templates/tpl-doesnotexist").status_code == 404


def test_connectable_targets_excludes_switchboard_inverter_positions(db_client):
    # Default project has 15 inverters -- none of INV-1..INV-15 should
    # appear as a "breaker" target, since those are inverter-owned feeds,
    # not available connection points.
    targets = db_client.get("/connectable-targets", params={"category": "breaker"}).json()
    tags = {t["tag"] for t in targets}
    assert not any(tag.startswith("INV-") for tag in tags)
    # The default project's aux panelboard circuits ARE real breaker targets.
    assert "AUX-1/CKT-1" in tags
    assert "AUX-1/CKT-2" in tags


def test_connectable_targets_bus_bars_carry_a_note(db_client):
    targets = db_client.get("/connectable-targets", params={"category": "ground_bar"}).json()
    tags = {t["tag"]: t for t in targets}
    assert "SWBD-1/GND_BUS" in tags
    assert "AUX-1/GND_BUS" in tags
    assert tags["SWBD-1/GND_BUS"]["note"] is not None


def test_connectable_targets_other_custom_devices(db_client):
    template_id = _create_inverter_combiner_template(db_client)
    project = {
        "custom_devices": [
            {
                "tag": "DAS-1",
                "template_id": template_id,
                "group_counts": {"neutral": 0},
                "connections": [{"group_id": "comms", "index": 0, "protocol": "RS485"}],
            }
        ]
    }
    assert db_client.put("/projects/default", json=project).status_code == 200

    targets = db_client.get("/connectable-targets", params={"category": "comms"}).json()
    assert any(t["tag"] == "DAS-1" for t in targets)


def test_put_project_rejects_custom_device_tag_colliding_with_inverter(db_client):
    response = db_client.put("/projects/default", json={"custom_devices": [{"tag": "INV-1", "template_id": "tpl-x"}]})
    assert response.status_code == 422


def test_put_project_rejects_duplicate_custom_device_tags(db_client):
    response = db_client.put(
        "/projects/default",
        json={
            "custom_devices": [
                {"tag": "LOAD-1", "template_id": "tpl-x"},
                {"tag": "LOAD-1", "template_id": "tpl-y"},
            ]
        },
    )
    assert response.status_code == 422


def test_refresh_creates_one_changeset_per_custom_device(db_client):
    template_id = _create_inverter_combiner_template(db_client)
    project = {
        "custom_devices": [
            {
                "tag": "INV-CUSTOM-1",
                "template_id": template_id,
                "connections": [
                    {"group_id": "ac_input", "index": 0, "connects_to": "AUX-1/CKT-1"},
                    {"group_id": "comms", "index": 0, "protocol": "RS485", "connects_to": "DAS-1"},
                ],
            }
        ]
    }
    assert db_client.put("/projects/default", json=project).status_code == 200

    response = db_client.post("/changesets/custom-device/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["device_count"] == 1

    cs = body["results"][0]["changeset"]
    assert cs["target_tag"] == "INV-CUSTOM-1"
    assert cs["block_type"] == "CUSTOM_DEVICE"
    assert cs["operation"] == "regenerate"
    assert len(cs["config"]["terminals"]) == 8  # 3 AC + 1 N + 1 GND(min) + 1 comms(min) + 2 DC


def test_refresh_is_idempotent_until_project_changes(db_client):
    template_id = _create_inverter_combiner_template(db_client)
    project = {
        "custom_devices": [
            {
                "tag": "INV-CUSTOM-2",
                "template_id": template_id,
                "connections": [{"group_id": "comms", "index": 0, "protocol": "RS485"}],
            }
        ]
    }
    db_client.put("/projects/default", json=project)

    first = db_client.post("/changesets/custom-device/refresh").json()["results"][0]
    assert first["created"] is True

    second = db_client.post("/changesets/custom-device/refresh").json()["results"][0]
    assert second["created"] is False
    assert second["changeset"]["changeset_id"] == first["changeset"]["changeset_id"]

    project["custom_devices"][0]["connections"][0]["protocol"] = "Ethernet"
    db_client.put("/projects/default", json=project)
    third = db_client.post("/changesets/custom-device/refresh").json()["results"][0]
    assert third["created"] is True
    assert third["changeset"]["changeset_id"] != first["changeset"]["changeset_id"]


def test_refresh_404s_on_unknown_template(db_client):
    project = {"custom_devices": [{"tag": "GHOST-1", "template_id": "tpl-doesnotexist"}]}
    db_client.put("/projects/default", json=project)
    response = db_client.post("/changesets/custom-device/refresh")
    assert response.status_code == 404
