"""End-to-end SkyVisor API: seed an asset map via the bulk endpoints, export
it, import a synthetic anomaly CSV, and walk an anomaly through the
resolve lifecycle -- all over real HTTP against the db_client fixture."""

from __future__ import annotations

CSV_HEADER = "anomaly_type,severity,latitude,longitude,delta_t_c,image_url"


def _seed_one_module(db_client) -> dict:
    table = db_client.post(
        "/skyvisor/array-tables/bulk",
        json=[{"block_tag": "TBL-1", "row_label": "Row 1", "latitude": 40.0, "longitude": -89.0}],
    ).json()[0]

    string = db_client.post(
        "/skyvisor/strings/bulk",
        json=[{"inverter_tag": "INV-1", "combiner_index": 1, "module_count": 28, "module_sku": "720"}],
    ).json()[0]

    module = db_client.post(
        "/skyvisor/modules/bulk",
        json=[
            {
                "array_table_id": table["id"],
                "string_asset_id": string["id"],
                "position_in_string": 1,
                "module_sku": "720",
                "latitude": 40.0,
                "longitude": -89.0,
            }
        ],
    ).json()[0]

    return {"table_id": table["id"], "string_id": string["id"], "module_id": module["id"]}


def test_modules_bulk_rejects_unknown_table_or_string(db_client):
    response = db_client.post(
        "/skyvisor/modules/bulk",
        json=[
            {
                "array_table_id": "tbl-doesnotexist",
                "string_asset_id": "str-doesnotexist",
                "position_in_string": 1,
                "module_sku": "720",
                "latitude": 40.0,
                "longitude": -89.0,
            }
        ],
    )
    assert response.status_code == 422


def test_asset_map_export_empty_before_any_seed(db_client):
    response = db_client.get("/skyvisor/asset-map.csv")
    assert response.status_code == 200
    assert response.text == ""


def test_asset_map_export_reflects_seeded_module(db_client):
    _seed_one_module(db_client)

    response = db_client.get("/skyvisor/asset-map.csv")

    assert response.status_code == 200
    assert "TBL-1" in response.text
    assert "INV-1" in response.text


def test_import_matches_anomaly_and_lists_it(db_client):
    ids = _seed_one_module(db_client)
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,40.0,-89.0,18.0,"])

    response = db_client.post(
        "/skyvisor/import",
        files={"file": ("flight1.csv", csv_text, "text/csv")},
        data={"flight_date": "2026-08-01T00:00:00+00:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["batch"]["status"] == "matched"
    assert len(body["anomalies"]) == 1
    assert body["anomalies"][0]["module_asset_id"] == ids["module_id"]

    listed = db_client.get("/skyvisor/anomalies").json()
    assert len(listed) == 1
    assert listed[0]["resolution_status"] == "open"


def test_import_rejects_bad_flight_date(db_client):
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,40.0,-89.0,18.0,"])

    response = db_client.post(
        "/skyvisor/import",
        files={"file": ("flight1.csv", csv_text, "text/csv")},
        data={"flight_date": "not-a-date"},
    )

    assert response.status_code == 422


def test_unmatched_anomaly_needs_attention_and_filters_by_status(db_client):
    _seed_one_module(db_client)
    # ~1 degree away -- far outside the default match tolerance.
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,41.0,-89.0,18.0,"])

    db_client.post(
        "/skyvisor/import",
        files={"file": ("flight2.csv", csv_text, "text/csv")},
        data={"flight_date": "2026-08-01T00:00:00+00:00"},
    )

    open_anomalies = db_client.get("/skyvisor/anomalies", params={"resolution_status": "open"}).json()
    assert len(open_anomalies) == 1
    assert open_anomalies[0]["module_asset_id"] is None


def test_resolve_anomaly_lifecycle(db_client):
    _seed_one_module(db_client)
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,40.0,-89.0,18.0,"])
    body = db_client.post(
        "/skyvisor/import",
        files={"file": ("flight1.csv", csv_text, "text/csv")},
        data={"flight_date": "2026-08-01T00:00:00+00:00"},
    ).json()
    anomaly_id = body["anomalies"][0]["id"]

    response = db_client.post(f"/skyvisor/anomalies/{anomaly_id}/resolve", json={"resolution_status": "resolved"})
    assert response.status_code == 200
    assert response.json()["resolution_status"] == "resolved"

    still_open = db_client.get("/skyvisor/anomalies", params={"resolution_status": "open"}).json()
    assert still_open == []


def test_resolve_rejects_invalid_status(db_client):
    _seed_one_module(db_client)
    csv_text = "\n".join([CSV_HEADER, "hotspot,high,40.0,-89.0,18.0,"])
    body = db_client.post(
        "/skyvisor/import",
        files={"file": ("flight1.csv", csv_text, "text/csv")},
        data={"flight_date": "2026-08-01T00:00:00+00:00"},
    ).json()
    anomaly_id = body["anomalies"][0]["id"]

    response = db_client.post(f"/skyvisor/anomalies/{anomaly_id}/resolve", json={"resolution_status": "bogus"})
    assert response.status_code == 422


def test_resolve_unknown_anomaly_404s(db_client):
    response = db_client.post("/skyvisor/anomalies/anom-doesnotexist/resolve", json={"resolution_status": "resolved"})
    assert response.status_code == 404
