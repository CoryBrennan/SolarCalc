"""End-to-end commissioning API: create a unit, add torque/visual/wire/
electrical items, record field readings, upload a photo, auto-populate wire
items and electrical readings from project design data, delete rows, and
confirm the unit's status rolls up correctly through the lifecycle -- all
over real HTTP against the db_client fixture, mirroring test_skyvisor_api.py's
shape."""

from __future__ import annotations


def _create_unit(db_client, equipment_type="inverter", tag="INV-04") -> dict:
    response = db_client.post(
        "/commissioning/units",
        json={"equipment_type": equipment_type, "tag": tag, "manufacturer": "SMA", "model": "Sunny Tripower"},
    )
    assert response.status_code == 200
    return response.json()


def test_create_unit_rejects_unknown_equipment_type(db_client):
    response = db_client.post("/commissioning/units", json={"equipment_type": "generator", "tag": "GEN-1"})
    assert response.status_code == 422


def test_create_unit_starts_not_started(db_client):
    unit = _create_unit(db_client)
    assert unit["status"] == "not_started"
    assert unit["summary"]["overall"] == "not_started"
    assert "visual_mechanical" in unit["summary"]
    assert "electrical" in unit["summary"]


def test_checklist_template_returns_torque_and_visual_mechanical_labels(db_client):
    response = db_client.get("/commissioning/checklist-templates", params={"equipment_type": "switchboard"})
    assert response.status_code == 200
    body = response.json()
    assert "Main breaker lugs" in body["torque_labels"]
    assert "Nameplate present & legible" in body["visual_mechanical_labels"]


def test_torque_point_lifecycle_updates_unit_status(db_client):
    unit = _create_unit(db_client)
    points = db_client.post(
        f"/commissioning/units/{unit['id']}/torque-points",
        json=[{"connection_label": "AC output lugs", "design_torque_min": 20.0, "design_torque_max": 25.0, "torque_unit": "ft-lb"}],
    ).json()
    assert points[0]["result"] == "pending"

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["status"] == "in_progress"

    point_id = points[0]["id"]
    updated = db_client.patch(
        f"/commissioning/torque-points/{point_id}",
        json={"measured_torque_value": 22.0, "wrench_id": "TW-114", "tech_initials": "CB"},
    ).json()
    assert updated["result"] == "pass"
    assert updated["checked_at"] is not None

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["status"] == "complete"
    assert detail["summary"]["visual_mechanical"] == {"total": 1, "pass": 1, "fail": 0, "pending": 0}


def test_torque_point_out_of_band_marks_unit_needs_attention(db_client):
    unit = _create_unit(db_client)
    points = db_client.post(
        f"/commissioning/units/{unit['id']}/torque-points",
        json=[{"connection_label": "DC input terminals", "design_torque_min": 20.0, "design_torque_max": 25.0}],
    ).json()

    db_client.patch(f"/commissioning/torque-points/{points[0]['id']}", json={"measured_torque_value": 30.0})

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["status"] == "needs_attention"
    assert detail["torque_points"][0]["result"] == "fail"


def test_delete_torque_point(db_client):
    unit = _create_unit(db_client)
    points = db_client.post(
        f"/commissioning/units/{unit['id']}/torque-points",
        json=[{"connection_label": "Ground lug"}],
    ).json()

    response = db_client.delete(f"/commissioning/torque-points/{points[0]['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "id": points[0]["id"]}

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["torque_points"] == []


def test_inspection_item_lifecycle(db_client):
    unit = _create_unit(db_client)
    items = db_client.post(
        f"/commissioning/units/{unit['id']}/inspection-items",
        json=[{"label": "Enclosure condition / no physical damage"}],
    ).json()
    assert items[0]["result"] == "pending"

    passed = db_client.patch(
        f"/commissioning/inspection-items/{items[0]['id']}", json={"result": "pass"}
    ).json()
    assert passed["result"] == "pass"
    assert passed["checked_at"] is not None

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["status"] == "complete"
    assert detail["summary"]["visual_mechanical"] == {"total": 1, "pass": 1, "fail": 0, "pending": 0}


def test_inspection_item_fail_marks_unit_needs_attention(db_client):
    unit = _create_unit(db_client)
    items = db_client.post(
        f"/commissioning/units/{unit['id']}/inspection-items",
        json=[{"label": "Nameplate present & legible"}],
    ).json()
    db_client.patch(f"/commissioning/inspection-items/{items[0]['id']}", json={"result": "fail", "notes": "Missing nameplate"})

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["status"] == "needs_attention"


def test_inspection_item_rejects_bad_result(db_client):
    unit = _create_unit(db_client)
    items = db_client.post(f"/commissioning/units/{unit['id']}/inspection-items", json=[{"label": "X"}]).json()
    response = db_client.patch(f"/commissioning/inspection-items/{items[0]['id']}", json={"result": "bogus"})
    assert response.status_code == 422


def test_delete_inspection_item(db_client):
    unit = _create_unit(db_client)
    items = db_client.post(f"/commissioning/units/{unit['id']}/inspection-items", json=[{"label": "X"}]).json()
    response = db_client.delete(f"/commissioning/inspection-items/{items[0]['id']}")
    assert response.status_code == 200
    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["inspection_items"] == []


def test_wire_item_lifecycle_and_conductor_mismatch(db_client):
    unit = _create_unit(db_client)
    items = db_client.post(
        f"/commissioning/units/{unit['id']}/wire-items",
        json=[{"circuit_label": "DC input MPPT1", "design_conductor": "500 kcmil CU"}],
    ).json()
    item_id = items[0]["id"]

    mismatched = db_client.patch(
        f"/commissioning/wire-items/{item_id}",
        json={
            "as_built_conductor": "4/0 AWG CU",
            "termination_ok": True,
            "labeling_ok": True,
            "continuity_ok": True,
            "insulation_resistance_megohm": 5.0,
        },
    ).json()
    assert mismatched["result"] == "fail"

    fixed = db_client.patch(
        f"/commissioning/wire-items/{item_id}",
        json={"as_built_conductor": "500 kcmil CU"},
    ).json()
    assert fixed["result"] == "pass"


def test_delete_wire_item(db_client):
    unit = _create_unit(db_client)
    items = db_client.post(
        f"/commissioning/units/{unit['id']}/wire-items",
        json=[{"circuit_label": "DC input MPPT1", "design_conductor": "500 kcmil CU"}],
    ).json()
    response = db_client.delete(f"/commissioning/wire-items/{items[0]['id']}")
    assert response.status_code == 200
    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["wire_items"] == []


def test_wire_items_auto_populate_matches_raceway_run_by_tag(db_client):
    unit = _create_unit(db_client, tag="INV-04")
    project = {
        "raceway_runs": [
            {"tag": "INV-04-AC", "current_a": 340, "voltage_v": 480, "length_ft": 200, "insulation_rating": 90, "vd_limit_pct": 1.5, "conduit_material": "EMT"},
            {"tag": "SWBD-1", "current_a": 50, "voltage_v": 480, "length_ft": 100},
        ]
    }
    response = db_client.post(f"/commissioning/units/{unit['id']}/wire-items/auto-populate", json={"project": project})
    assert response.status_code == 200
    body = response.json()
    assert body["matched_run_count"] == 1
    assert len(body["created"]) == 1
    assert body["created"][0]["circuit_label"] == "INV-04-AC"
    assert "CU" in body["created"][0]["design_conductor"] or "AL" in body["created"][0]["design_conductor"]

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert len(detail["wire_items"]) == 1

    # re-running is idempotent: updates the existing row instead of duplicating
    response2 = db_client.post(f"/commissioning/units/{unit['id']}/wire-items/auto-populate", json={"project": project})
    body2 = response2.json()
    assert len(body2["created"]) == 0
    assert len(body2["updated"]) == 1

    detail2 = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert len(detail2["wire_items"]) == 1


def test_wire_items_auto_populate_no_match_returns_empty(db_client):
    unit = _create_unit(db_client, tag="INV-99")
    project = {"raceway_runs": [{"tag": "SWBD-1", "current_a": 50, "voltage_v": 480, "length_ft": 100}]}
    response = db_client.post(f"/commissioning/units/{unit['id']}/wire-items/auto-populate", json={"project": project})
    assert response.status_code == 200
    body = response.json()
    assert body["matched_run_count"] == 0
    assert body["created"] == []


def test_electrical_reading_lifecycle(db_client):
    unit = _create_unit(db_client)
    readings = db_client.post(
        f"/commissioning/units/{unit['id']}/electrical-readings",
        json=[{"label": "AC output L1-L2", "design_min": 760.0, "design_max": 840.0, "unit": "VAC"}],
    ).json()
    assert readings[0]["result"] == "pending"

    passed = db_client.patch(
        f"/commissioning/electrical-readings/{readings[0]['id']}", json={"measured_value": 802.0}
    ).json()
    assert passed["result"] == "pass"

    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["summary"]["electrical"] == {"total": 1, "pass": 1, "fail": 0, "pending": 0}


def test_electrical_reading_out_of_band_fails(db_client):
    unit = _create_unit(db_client)
    readings = db_client.post(
        f"/commissioning/units/{unit['id']}/electrical-readings",
        json=[{"label": "AC output L1-L2", "design_min": 760.0, "design_max": 840.0}],
    ).json()
    result = db_client.patch(f"/commissioning/electrical-readings/{readings[0]['id']}", json={"measured_value": 700.0}).json()
    assert result["result"] == "fail"


def test_delete_electrical_reading(db_client):
    unit = _create_unit(db_client)
    readings = db_client.post(
        f"/commissioning/units/{unit['id']}/electrical-readings", json=[{"label": "AC output L1-L2"}]
    ).json()
    response = db_client.delete(f"/commissioning/electrical-readings/{readings[0]['id']}")
    assert response.status_code == 200
    detail = db_client.get(f"/commissioning/units/{unit['id']}").json()
    assert detail["electrical_readings"] == []


def test_electrical_readings_auto_populate_derives_three_phase_bands(db_client):
    unit = _create_unit(db_client, "inverter", "INV-04")
    project = {"inverter": {"nominal_ac_voltage_v": 800.0, "phases": 3}}
    response = db_client.post(f"/commissioning/units/{unit['id']}/electrical-readings/auto-populate", json={"project": project})
    assert response.status_code == 200
    body = response.json()
    assert body["derived_count"] == 3
    assert len(body["created"]) == 3
    labels = {r["label"] for r in body["created"]}
    assert labels == {"AC output L1-L2", "AC output L2-L3", "AC output L1-L3"}
    for r in body["created"]:
        assert r["design_min"] == 760.0
        assert r["design_max"] == 840.0

    # idempotent re-run
    response2 = db_client.post(f"/commissioning/units/{unit['id']}/electrical-readings/auto-populate", json={"project": project})
    body2 = response2.json()
    assert body2["created"] == []
    assert len(body2["updated"]) == 3


def test_electrical_readings_auto_populate_noop_for_load_center(db_client):
    unit = _create_unit(db_client, "load_center", "LC-1")
    response = db_client.post(f"/commissioning/units/{unit['id']}/electrical-readings/auto-populate", json={"project": {}})
    assert response.status_code == 200
    body = response.json()
    assert body["derived_count"] == 0
    assert "note" in body


def test_sign_off_requires_complete_status(db_client):
    unit = _create_unit(db_client)
    db_client.post(
        f"/commissioning/units/{unit['id']}/torque-points",
        json=[{"connection_label": "Ground lug", "design_torque_min": 8.0, "design_torque_max": 10.0}],
    )

    response = db_client.patch(f"/commissioning/units/{unit['id']}", json={"commissioned_by": "CB"})
    assert response.status_code == 422


def test_sign_off_succeeds_once_complete(db_client):
    unit = _create_unit(db_client)
    points = db_client.post(
        f"/commissioning/units/{unit['id']}/torque-points",
        json=[{"connection_label": "Ground lug", "design_torque_min": 8.0, "design_torque_max": 10.0}],
    ).json()
    db_client.patch(f"/commissioning/torque-points/{points[0]['id']}", json={"measured_torque_value": 9.0})

    response = db_client.patch(f"/commissioning/units/{unit['id']}", json={"commissioned_by": "CB"})
    assert response.status_code == 200
    body = response.json()
    assert body["commissioned_by"] == "CB"
    assert body["commissioned_at"] is not None


def test_photo_upload_and_fetch(db_client):
    unit = _create_unit(db_client)
    response = db_client.post(
        f"/commissioning/units/{unit['id']}/photos",
        files={"file": ("torque_mark.jpg", b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")},
        data={"category": "visual_mechanical", "caption": "AC lugs, paint pen witness mark"},
    )
    assert response.status_code == 200
    photo = response.json()
    assert photo["category"] == "visual_mechanical"
    assert photo["size_bytes"] > 0

    fetched = db_client.get(f"/commissioning/photos/{photo['id']}")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/jpeg"
    assert fetched.content == b"\xff\xd8\xff\xe0fakejpegbytes"

    listed = db_client.get(f"/commissioning/units/{unit['id']}/photos").json()
    assert len(listed) == 1


def test_photo_upload_rejects_bad_category(db_client):
    unit = _create_unit(db_client)
    response = db_client.post(
        f"/commissioning/units/{unit['id']}/photos",
        files={"file": ("x.jpg", b"data", "image/jpeg")},
        data={"category": "bogus"},
    )
    assert response.status_code == 422


def test_delete_photo(db_client):
    unit = _create_unit(db_client)
    photo = db_client.post(
        f"/commissioning/units/{unit['id']}/photos",
        files={"file": ("x.jpg", b"data", "image/jpeg")},
        data={"category": "electrical"},
    ).json()

    response = db_client.delete(f"/commissioning/photos/{photo['id']}")
    assert response.status_code == 200
    assert db_client.get(f"/commissioning/photos/{photo['id']}").status_code == 404


def test_list_units_filters_by_equipment_type_and_status(db_client):
    _create_unit(db_client, "inverter", "INV-01")
    _create_unit(db_client, "switchboard", "SWBD-1")

    inverters = db_client.get("/commissioning/units", params={"equipment_type": "inverter"}).json()
    assert len(inverters) == 1
    assert inverters[0]["tag"] == "INV-01"

    not_started = db_client.get("/commissioning/units", params={"status": "not_started"}).json()
    assert len(not_started) == 2


def test_get_unknown_unit_404s(db_client):
    response = db_client.get("/commissioning/units/cxu-doesnotexist")
    assert response.status_code == 404


def test_update_unknown_torque_point_404s(db_client):
    response = db_client.patch("/commissioning/torque-points/trq-doesnotexist", json={"measured_torque_value": 5.0})
    assert response.status_code == 404


def test_delete_unit_cascades_to_all_children(db_client):
    unit = _create_unit(db_client)
    uid = unit["id"]
    db_client.post(f"/commissioning/units/{uid}/torque-points", json=[{"connection_label": "Ground lug"}])
    db_client.post(f"/commissioning/units/{uid}/inspection-items", json=[{"label": "Nameplate"}])
    db_client.post(f"/commissioning/units/{uid}/wire-items", json=[{"circuit_label": "AC out", "design_conductor": "4/0 AWG CU"}])
    db_client.post(f"/commissioning/units/{uid}/electrical-readings", json=[{"label": "AC output L1-L2"}])
    db_client.post(
        f"/commissioning/units/{uid}/photos",
        files={"file": ("x.jpg", b"data", "image/jpeg")},
        data={"category": "visual_mechanical"},
    )

    response = db_client.delete(f"/commissioning/units/{uid}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "id": uid}

    assert db_client.get(f"/commissioning/units/{uid}").status_code == 404
    assert db_client.get("/commissioning/units").json() == []


def test_delete_unknown_unit_404s(db_client):
    response = db_client.delete("/commissioning/units/cxu-doesnotexist")
    assert response.status_code == 404
