"""End-to-end changeset API: PUT a project, refresh the switchboard
changeset, walk it through the pending -> applied and pending -> failed x5
-> needs_attention -> retry lifecycles over real HTTP.
"""

from __future__ import annotations


def test_get_project_404_before_any_put(db_client):
    response = db_client.get("/projects/default")
    assert response.status_code == 404


def test_put_then_get_project_round_trips(db_client):
    put_response = db_client.put("/projects/default", json={"site": {"project_name": "Test Project"}})
    assert put_response.status_code == 200

    get_response = db_client.get("/projects/default")
    assert get_response.status_code == 200
    assert get_response.json()["site"]["project_name"] == "Test Project"


def test_put_project_rejects_unknown_module_sku(db_client):
    response = db_client.put("/projects/default", json={"module": {"sku": "999"}})
    assert response.status_code == 422


def test_refresh_creates_pending_changeset_matching_default_project(db_client):
    response = db_client.post("/changesets/switchboard/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True

    cs = body["changeset"]
    assert cs["target_tag"] == "SWBD-1"
    assert cs["block_type"] == "SWITCHBOARD"
    assert cs["operation"] == "regenerate"
    assert cs["status"] == "pending"
    assert cs["config"]["backfeed_total_amps"] == 5250
    assert len(cs["config"]["positions"]) == 15


def test_refresh_is_idempotent_until_project_changes(db_client):
    first = db_client.post("/changesets/switchboard/refresh").json()
    assert first["created"] is True

    second = db_client.post("/changesets/switchboard/refresh").json()
    assert second["created"] is False
    assert second["changeset"]["changeset_id"] == first["changeset"]["changeset_id"]

    db_client.put("/projects/default", json={"switchboard": {"busbar_rating_a": 2000}})
    third = db_client.post("/changesets/switchboard/refresh").json()
    assert third["created"] is True
    assert third["changeset"]["changeset_id"] != first["changeset"]["changeset_id"]


def test_refresh_creates_aux_panelboard_changeset(db_client):
    response = db_client.post("/changesets/aux-panelboard/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True

    cs = body["changeset"]
    assert cs["target_tag"] == "AUX-1"
    assert cs["block_type"] == "AUX_PANELBOARD"
    assert cs["config"]["main_breaker_rating"] == "100A"
    assert len(cs["config"]["positions"]) == 2


def test_refresh_creates_inverter_dc_changesets_for_combiner_topology(db_client):
    response = db_client.post("/changesets/inverter-dc/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["topology"] == "combiner"
    assert len(body["results"]) == 2  # default project has 2 combiner rows

    tags = {r["changeset"]["target_tag"] for r in body["results"]}
    assert tags == {"DCC-1", "DCC-2"}
    assert all(r["changeset"]["block_type"] == "INVERTER_DC" for r in body["results"])


def test_refresh_creates_single_mppt_changeset_for_direct_topology(db_client):
    db_client.put("/projects/default", json={"inverter": {"dc_topology": "direct"}})
    response = db_client.post("/changesets/inverter-dc/refresh")
    body = response.json()
    assert body["topology"] == "direct"
    assert len(body["results"]) == 1
    assert body["results"][0]["changeset"]["target_tag"] == "INV-1"
    assert body["results"][0]["changeset"]["config"]["block_variant"] == "mppt"


def test_refresh_creates_one_inverter_ac_changeset_per_inverter(db_client):
    response = db_client.post("/changesets/inverter-ac/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["inverter_count"] == 15
    assert len(body["results"]) == 15

    first = body["results"][0]["changeset"]
    assert first["target_tag"] == "INV-1"
    assert first["block_type"] == "INVERTER_AC"
    assert first["operation"] == "attribute_update"
    assert first["config"]["attributes"]["KWAC"] == "350.000 KWAC"
    assert first["config"]["ocpd_rating"] == "350 A/3"


def test_inverter_ac_refresh_is_idempotent_until_project_changes(db_client):
    first = db_client.post("/changesets/inverter-ac/refresh").json()["results"][0]
    assert first["created"] is True

    second = db_client.post("/changesets/inverter-ac/refresh").json()["results"][0]
    assert second["created"] is False

    db_client.put("/projects/default", json={"inverter": {"nominal_ac_voltage_v": 600}})
    third = db_client.post("/changesets/inverter-ac/refresh").json()["results"][0]
    assert third["created"] is True
    assert third["changeset"]["config"]["attributes"]["VAC"] == "600 VAC"


def test_ac_and_dc_blocks_sharing_a_tag_do_not_re_enqueue_each_other(db_client):
    """INV-1 has both an AC and a DC block by design. Refreshing one must not
    invalidate the other's dedup — otherwise every refresh cycle re-enqueues
    both blocks forever."""
    db_client.put("/projects/default", json={"inverter": {"dc_topology": "direct"}})

    assert db_client.post("/changesets/inverter-ac/refresh").json()["results"][0]["created"] is True
    assert db_client.post("/changesets/inverter-dc/refresh").json()["results"][0]["created"] is True

    # Second pass: nothing changed, so neither block should enqueue again.
    assert db_client.post("/changesets/inverter-ac/refresh").json()["results"][0]["created"] is False
    assert db_client.post("/changesets/inverter-dc/refresh").json()["results"][0]["created"] is False

    inv1 = db_client.get("/changesets", params={"target_tag": "INV-1"}).json()
    assert {c["block_type"] for c in inv1} == {"INVERTER_AC", "INVERTER_DC"}
    assert len(inv1) == 2  # exactly one changeset each, no duplicates


def test_refresh_creates_transformer_attribute_update_changeset(db_client):
    response = db_client.post("/changesets/transformer/refresh")
    assert response.status_code == 200
    body = response.json()
    cs = body["changeset"]
    assert cs["target_tag"] == "XFMR-1"
    assert cs["block_type"] == "TRANSFORMER"
    assert cs["operation"] == "attribute_update"
    assert cs["config"]["attributes"]["SDS_FLAG"] == "Yes"


def test_refresh_creates_mv_device_changesets(db_client):
    recloser = db_client.post("/changesets/mv-recloser/refresh").json()["changeset"]
    goab = db_client.post("/changesets/mv-goab/refresh").json()["changeset"]
    meter = db_client.post("/changesets/mv-meter/refresh").json()["changeset"]

    assert recloser["block_type"] == "MV_RECLOSER"
    assert recloser["operation"] == "attribute_update"
    assert goab["block_type"] == "MV_GOAB"
    assert meter["block_type"] == "MV_METER"


def test_pending_list_and_lifecycle_to_applied(db_client):
    created = db_client.post("/changesets/switchboard/refresh").json()["changeset"]
    cs_id = created["changeset_id"]

    pending = db_client.get("/changesets/pending").json()
    assert any(c["changeset_id"] == cs_id for c in pending)

    applied = db_client.post(f"/changesets/{cs_id}/applied").json()
    assert applied["status"] == "applied"
    assert applied["retry_count"] == 0

    pending_after = db_client.get("/changesets/pending").json()
    assert not any(c["changeset_id"] == cs_id for c in pending_after)


def test_lifecycle_to_needs_attention_and_retry(db_client):
    created = db_client.post("/changesets/switchboard/refresh").json()["changeset"]
    cs_id = created["changeset_id"]

    for i in range(5):
        response = db_client.post(f"/changesets/{cs_id}/failed", json={"error": f"attempt {i + 1}"})
        assert response.status_code == 200

    failed_state = db_client.get(f"/changesets/{cs_id}").json()
    assert failed_state["status"] == "needs_attention"
    assert failed_state["retry_count"] == 5
    assert failed_state["last_error"] == "attempt 5"

    retried = db_client.post(f"/changesets/{cs_id}/retry").json()
    assert retried["status"] == "pending"
    assert retried["retry_count"] == 0


def test_unknown_changeset_id_404s(db_client):
    assert db_client.get("/changesets/cs-doesnotexist").status_code == 404
    assert db_client.post("/changesets/cs-doesnotexist/applied").status_code == 404
    assert db_client.post("/changesets/cs-doesnotexist/failed", json={"error": "x"}).status_code == 404
    assert db_client.post("/changesets/cs-doesnotexist/retry").status_code == 404
