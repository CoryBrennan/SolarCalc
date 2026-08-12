"""End-to-end commissioning API: create a unit, add torque points and wire
items, record field readings, upload a photo, and confirm the unit's status
rolls up correctly through the lifecycle -- all over real HTTP against the
db_client fixture, mirroring test_skyvisor_api.py's shape."""

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


def test_checklist_template_returns_labels_for_equipment_type(db_client):
    response = db_client.get("/commissioning/checklist-templates", params={"equipment_type": "switchboard"})
    assert response.status_code == 200
    body = response.json()
    assert "Main breaker lugs" in body["connection_labels"]


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
    assert detail["summary"]["torque"] == {"total": 1, "pass": 1, "fail": 0, "pending": 0}


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
        data={"category": "torque", "caption": "AC lugs, paint pen witness mark"},
    )
    assert response.status_code == 200
    photo = response.json()
    assert photo["category"] == "torque"
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
