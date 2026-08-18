"""End-to-end Bluebeam plan-review API: create a review set, upload the clean
master and several reviewers' marked-up copies, consolidate them onto one set,
work the markups through disposition to sign-off, then run a second round and
diff it against the first -- all over real HTTP against the db_client fixture,
mirroring test_commissioning_api.py's shape.
"""

from __future__ import annotations

from tests.bluebeam_pdf_fixtures import blank_set, marked_set

SHARED = {"page": 0, "rect": [100, 100, 220, 170], "contents": "verify trench depth", "nm": "shared"}
RUIZ_2 = {"page": 1, "rect": [300, 300, 400, 380], "contents": "conduit clashes with pile", "nm": "a2"}
CHEN_2 = {"page": 1, "rect": [350, 340, 470, 420], "contents": "reroute instead", "nm": "b2"}


def _create_set(db_client, name="Encore Brighton 2 -- Electrical") -> dict:
    response = db_client.post(
        "/bluebeam/review-sets",
        json={"name": name, "revision_label": "IFC Rev C", "discipline": "Electrical"},
    )
    assert response.status_code == 200
    return response.json()


def _upload_master(db_client, set_id, pages=3) -> dict:
    response = db_client.post(
        f"/bluebeam/review-sets/{set_id}/master",
        files={"file": ("plans_rev_c.pdf", blank_set(pages), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload_submission(db_client, set_id, label, specs, round_number=1, replies=None, pages=3):
    return db_client.post(
        f"/bluebeam/review-sets/{set_id}/submissions",
        data={"label": label, "round_number": round_number, "uploaded_by": "cory"},
        files={
            "file": (
                f"{label}.pdf",
                marked_set(label, specs, status_replies=replies, pages=pages),
                "application/pdf",
            )
        },
    )


def _seeded_set(db_client):
    """A review set with a master and two reviewers' round-1 submissions."""
    rs = _create_set(db_client)
    _upload_master(db_client, rs["id"])
    a = _upload_submission(
        db_client, rs["id"], "A. Ruiz", [SHARED, RUIZ_2],
        replies=[{"page": 0, "parent_nm": "shared", "state": "Accepted"}],
    )
    b = _upload_submission(db_client, rs["id"], "B. Chen", [SHARED, CHEN_2])
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    return rs["id"]


# ---------------------------------------------------------------------------
# review sets and submissions
# ---------------------------------------------------------------------------


def test_create_review_set_starts_open_and_empty(db_client):
    rs = _create_set(db_client)
    assert rs["status"] == "open"
    assert rs["current_round"] == 0
    assert rs["page_count"] is None
    assert rs["summary"]["total"] == 0


def test_upload_master_records_the_page_count(db_client):
    rs = _create_set(db_client)
    updated = _upload_master(db_client, rs["id"], pages=4)
    assert updated["page_count"] == 4
    assert updated["has_master_bytes"] is True
    assert updated["master_filename"] == "plans_rev_c.pdf"


def test_upload_master_rejects_a_non_pdf(db_client):
    rs = _create_set(db_client)
    response = db_client.post(
        f"/bluebeam/review-sets/{rs['id']}/master",
        files={"file": ("notes.txt", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 400


def test_submission_extracts_markups_and_advances_the_round(db_client):
    rs = _create_set(db_client)
    _upload_master(db_client, rs["id"])
    body = _upload_submission(db_client, rs["id"], "A. Ruiz", [SHARED, RUIZ_2]).json()

    assert body["markup_count"] == 2
    assert body["label"] == "A. Ruiz"
    assert body["round_number"] == 1

    detail = db_client.get(f"/bluebeam/review-sets/{rs['id']}").json()
    assert detail["current_round"] == 1
    assert len(detail["markups"]) == 2
    assert all(m["disposition"] == "open" for m in detail["markups"])
    assert detail["summary"]["outstanding"] == 2


def test_revu_status_is_imported_as_advisory_alongside_the_app_disposition(db_client):
    set_id = _seeded_set(db_client)
    markups = db_client.get(f"/bluebeam/review-sets/{set_id}").json()["markups"]
    shared = [m for m in markups if m["contents"] == "verify trench depth"]

    from_ruiz = next(m for m in shared if m["source_label"] == "A. Ruiz")
    assert from_ruiz["revu_status"] == "Accepted"  # what the file said
    assert from_ruiz["disposition"] == "open"      # what the app has decided


def test_a_comment_inherited_by_two_copies_is_recorded_once(db_client):
    """Every issued copy carries a prior round's comments. Storing one row per
    copy would mean dispositioning the same comment twice, and would put the
    review table out of step with the consolidated PDF, which merges it once."""
    rs = _create_set(db_client)
    _upload_master(db_client, rs["id"])
    _upload_submission(db_client, rs["id"], "A. Ruiz", [SHARED, RUIZ_2])
    second = _upload_submission(db_client, rs["id"], "B. Chen", [SHARED, CHEN_2]).json()

    assert second["markup_count"] == 2  # what was in Chen's file
    assert second["already_recorded_from_another_copy"] == 1

    markups = db_client.get(f"/bluebeam/review-sets/{rs['id']}").json()["markups"]
    assert len(markups) == 3
    shared = next(m for m in markups if m["key"] == "shared")
    assert shared["source_label"] == "A. Ruiz"
    assert shared["also_from"] == ["B. Chen"]


def test_submission_with_wrong_page_count_is_refused_with_a_reason(db_client):
    rs = _create_set(db_client)
    _upload_master(db_client, rs["id"], pages=3)
    response = _upload_submission(db_client, rs["id"], "D. Stale", [SHARED], pages=2)

    assert response.status_code == 422
    assert "2 pages but the master has 3" in response.json()["detail"]


def test_submission_to_a_missing_review_set_is_404(db_client):
    assert _upload_submission(db_client, "rev-nope", "A", [SHARED]).status_code == 404


def test_deleting_a_submission_removes_its_markups(db_client):
    set_id = _seeded_set(db_client)
    subs = db_client.get(f"/bluebeam/review-sets/{set_id}").json()["submissions"]
    target = next(s for s in subs if s["label"] == "B. Chen")

    deleted = db_client.delete(f"/bluebeam/submissions/{target['id']}")
    # Only b2 belongs to Chen's row; the shared comment was recorded once under
    # Ruiz, with Chen listed in also_from.
    assert deleted.status_code == 200
    assert deleted.json()["markup_items_deleted"] == 1

    remaining = db_client.get(f"/bluebeam/review-sets/{set_id}").json()["markups"]
    assert {m["source_label"] for m in remaining} == {"A. Ruiz"}
    assert len(remaining) == 2


# ---------------------------------------------------------------------------
# consolidation
# ---------------------------------------------------------------------------


def test_compatibility_check_passes_for_matching_sets(db_client):
    set_id = _seeded_set(db_client)
    body = db_client.post(f"/bluebeam/review-sets/{set_id}/check-compatibility").json()
    assert body["compatible"] is True
    assert body["submission_count"] == 2
    assert body["problems"] == []


def test_consolidate_merges_reviewers_into_one_layered_set(db_client):
    set_id = _seeded_set(db_client)
    response = db_client.post(f"/bluebeam/review-sets/{set_id}/consolidate")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["layers"] == ["A. Ruiz", "B. Chen"]
    assert body["markup_count"] == 3  # 4 submitted, the shared one merged
    assert body["duplicate_count"] == 1
    assert body["duplicates"][0]["also_from"] == "B. Chen"
    assert body["overlap_cluster_count"] == 1
    assert body["size_bytes"] > 0

    detail = db_client.get(f"/bluebeam/review-sets/{set_id}").json()
    assert detail["status"] == "consolidated"
    assert detail["has_consolidated_pdf"] is True


def test_consolidated_pdf_downloads_as_a_real_pdf(db_client):
    set_id = _seeded_set(db_client)
    db_client.post(f"/bluebeam/review-sets/{set_id}/consolidate")

    response = db_client.get(f"/bluebeam/review-sets/{set_id}/consolidated.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "IFC_Rev_C_consolidated.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")

    # and it still reads back as live markups, not a flattened image
    from app.bluebeam_markup_io import extract_markups

    markups, pages = extract_markups(response.content)
    assert pages == 3
    assert len(markups) == 3


def test_downloading_before_consolidating_is_404(db_client):
    set_id = _seeded_set(db_client)
    assert db_client.get(f"/bluebeam/review-sets/{set_id}/consolidated.pdf").status_code == 404


def test_consolidating_an_empty_round_is_refused(db_client):
    rs = _create_set(db_client)
    _upload_master(db_client, rs["id"])
    response = db_client.post(f"/bluebeam/review-sets/{rs['id']}/consolidate")
    assert response.status_code == 400
    assert "no submissions" in response.json()["detail"]


def test_consolidating_without_a_master_is_refused(db_client):
    rs = _create_set(db_client)
    _upload_submission(db_client, rs["id"], "A. Ruiz", [SHARED])
    response = db_client.post(f"/bluebeam/review-sets/{rs['id']}/consolidate")
    assert response.status_code == 400
    assert "no master drawing set" in response.json()["detail"]


# ---------------------------------------------------------------------------
# dispositions, audit trail, approval
# ---------------------------------------------------------------------------


def _markups(db_client, set_id):
    return db_client.get(f"/bluebeam/review-sets/{set_id}").json()["markups"]


def test_disposition_moves_through_accepted_to_incorporated(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]

    accepted = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "accepted", "actor": "C. Brennan", "assigned_to": "drafting"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["disposition"] == "accepted"
    assert accepted.json()["assigned_to"] == "drafting"
    assert accepted.json()["resolved_at"] is None  # not terminal yet

    done = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "incorporated", "actor": "C. Brennan"},
    )
    assert done.status_code == 200
    assert done.json()["resolved_by"] == "C. Brennan"
    assert done.json()["resolved_at"] is not None


def test_skipping_straight_to_incorporated_is_refused(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]
    response = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition", json={"disposition": "incorporated"}
    )
    assert response.status_code == 422
    assert "legal moves" in response.json()["detail"]


def test_rejecting_without_a_reason_is_refused(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]

    bare = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "rejected", "actor": "CB"},
    )
    assert bare.status_code == 422
    assert "written response" in bare.json()["detail"]

    with_reason = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "rejected", "actor": "CB", "response": "existing routing is per IFC"},
    )
    assert with_reason.status_code == 200
    assert with_reason.json()["response"] == "existing routing is per IFC"


def test_every_disposition_change_lands_in_the_audit_trail(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "accepted", "actor": "C. Brennan"},
    )
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "incorporated", "actor": "R. Patel"},
    )

    audit = db_client.get(f"/bluebeam/markups/{item['id']}/audit").json()
    assert [(a["from_disposition"], a["to_disposition"]) for a in audit] == [
        ("open", "accepted"), ("accepted", "incorporated"),
    ]
    assert [a["actor"] for a in audit] == ["C. Brennan", "R. Patel"]


def test_bulk_disposition_reports_successes_and_failures_separately(db_client):
    set_id = _seeded_set(db_client)
    items = _markups(db_client, set_id)
    response = db_client.post(
        f"/bluebeam/review-sets/{set_id}/dispositions/bulk",
        json={
            "markup_item_ids": [i["id"] for i in items] + ["mki-nonexistent"],
            "disposition": "accepted",
            "actor": "C. Brennan",
        },
    )
    body = response.json()
    assert body["updated_count"] == len(items)
    assert len(body["failed"]) == 1
    assert body["failed"][0]["id"] == "mki-nonexistent"


def test_approval_is_blocked_until_every_markup_is_terminal(db_client):
    set_id = _seeded_set(db_client)
    gate = db_client.get(f"/bluebeam/review-sets/{set_id}/approval").json()
    assert gate["ready_to_approve"] is False
    assert gate["blockers"][0]["disposition"] == "open"

    refused = db_client.post(
        f"/bluebeam/review-sets/{set_id}/approve", json={"approved_by": "C. Brennan"}
    )
    assert refused.status_code == 422
    assert "block sign-off" in refused.json()["detail"]


def test_accepted_but_not_drawn_still_blocks_sign_off(db_client):
    set_id = _seeded_set(db_client)
    items = _markups(db_client, set_id)
    db_client.post(
        f"/bluebeam/review-sets/{set_id}/dispositions/bulk",
        json={"markup_item_ids": [i["id"] for i in items], "disposition": "accepted", "actor": "CB"},
    )
    gate = db_client.get(f"/bluebeam/review-sets/{set_id}/approval").json()
    assert gate["ready_to_approve"] is False
    assert gate["blockers"][0]["disposition"] == "accepted"
    assert "hasn't been confirmed" in gate["blockers"][0]["reason"]


def _resolve_everything(db_client, set_id):
    ids = [i["id"] for i in _markups(db_client, set_id)]
    db_client.post(
        f"/bluebeam/review-sets/{set_id}/dispositions/bulk",
        json={"markup_item_ids": ids, "disposition": "accepted", "actor": "CB"},
    )
    db_client.post(
        f"/bluebeam/review-sets/{set_id}/dispositions/bulk",
        json={"markup_item_ids": ids, "disposition": "incorporated", "actor": "CB"},
    )


def test_a_fully_resolved_set_can_be_approved(db_client):
    set_id = _seeded_set(db_client)
    _resolve_everything(db_client, set_id)

    gate = db_client.get(f"/bluebeam/review-sets/{set_id}/approval").json()
    assert gate["ready_to_approve"] is True

    approved = db_client.post(
        f"/bluebeam/review-sets/{set_id}/approve",
        json={"approved_by": "C. Brennan", "note": "all comments incorporated in Rev D"},
    )
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == "C. Brennan"
    assert body["approved_at"] is not None


def test_approving_an_empty_set_is_refused(db_client):
    rs = _create_set(db_client)
    _upload_master(db_client, rs["id"])
    response = db_client.post(
        f"/bluebeam/review-sets/{rs['id']}/approve", json={"approved_by": "CB"}
    )
    assert response.status_code == 422
    assert "nothing to approve" in response.json()["detail"]


def test_reopening_a_markup_after_sign_off_unwinds_the_approval(db_client):
    set_id = _seeded_set(db_client)
    _resolve_everything(db_client, set_id)
    db_client.post(f"/bluebeam/review-sets/{set_id}/approve", json={"approved_by": "CB"})

    item = _markups(db_client, set_id)[0]
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "open", "actor": "CB"},
    )

    detail = db_client.get(f"/bluebeam/review-sets/{set_id}").json()
    assert detail["status"] != "approved"
    assert detail["approved_by"] is None


def test_a_new_submission_after_sign_off_reopens_the_set(db_client):
    set_id = _seeded_set(db_client)
    _resolve_everything(db_client, set_id)
    db_client.post(f"/bluebeam/review-sets/{set_id}/approve", json={"approved_by": "CB"})

    late = _upload_submission(
        db_client, set_id, "D. Late",
        [{"page": 2, "rect": [10, 10, 60, 60], "contents": "late comment", "nm": "d1"}],
    )
    assert late.status_code == 200

    detail = db_client.get(f"/bluebeam/review-sets/{set_id}").json()
    assert detail["status"] == "open"
    assert detail["approved_by"] is None


# ---------------------------------------------------------------------------
# round two: change tracking
# ---------------------------------------------------------------------------


def test_round_two_carries_dispositions_forward_for_unchanged_markups(db_client):
    set_id = _seeded_set(db_client)
    ruiz = [m for m in _markups(db_client, set_id) if m["source_label"] == "A. Ruiz"]
    db_client.post(
        f"/bluebeam/review-sets/{set_id}/dispositions/bulk",
        json={"markup_item_ids": [m["id"] for m in ruiz], "disposition": "accepted", "actor": "CB"},
    )

    # Round 2: the shared markup is untouched, RUIZ_2's text is edited.
    edited = dict(RUIZ_2, contents="conduit clashes with pile -- moved 6 ft east")
    body = _upload_submission(db_client, set_id, "A. Ruiz", [SHARED, edited], round_number=2).json()
    assert body["round_number"] == 2
    assert body["carried_forward"] == 2

    round2 = db_client.get(f"/bluebeam/review-sets/{set_id}").json()["markups"]
    by_key = {m["key"]: m for m in round2}
    assert by_key["shared"]["disposition"] == "accepted", "untouched comment shouldn't need re-deciding"
    assert by_key["a2"]["disposition"] == "open", "an edited comment must be re-decided"


def test_round_diff_reports_what_changed_between_rounds(db_client):
    set_id = _seeded_set(db_client)
    edited = dict(RUIZ_2, contents="conduit clashes with pile -- moved 6 ft east")
    new = {"page": 2, "rect": [10, 10, 60, 60], "contents": "add ground bar detail", "nm": "a3"}
    _upload_submission(db_client, set_id, "A. Ruiz", [SHARED, edited, new], round_number=2)
    _upload_submission(db_client, set_id, "B. Chen", [SHARED, CHEN_2], round_number=2)

    diff = db_client.get(f"/bluebeam/review-sets/{set_id}/rounds/2/diff").json()
    assert diff["from_round"] == 1 and diff["to_round"] == 2
    assert diff["counts"]["added"] == 1
    assert diff["counts"]["modified"] == 1
    assert diff["counts"]["unchanged"] == 2
    assert diff["counts"]["withdrawn"] == 0
    assert diff["added"][0]["contents"] == "add ground bar detail"
    assert diff["modified"][0]["previous"]["contents"] == "conduit clashes with pile"


def test_a_withdrawn_markup_shows_up_in_the_diff(db_client):
    set_id = _seeded_set(db_client)
    _upload_submission(db_client, set_id, "A. Ruiz", [SHARED], round_number=2)
    _upload_submission(db_client, set_id, "B. Chen", [SHARED, CHEN_2], round_number=2)

    diff = db_client.get(f"/bluebeam/review-sets/{set_id}/rounds/2/diff").json()
    assert diff["counts"]["withdrawn"] == 1
    assert diff["withdrawn"][0]["contents"] == "conduit clashes with pile"


def test_round_one_has_nothing_to_diff_against(db_client):
    set_id = _seeded_set(db_client)
    response = db_client.get(f"/bluebeam/review-sets/{set_id}/rounds/1/diff")
    assert response.status_code == 400
    assert "baseline" in response.json()["detail"]


# ---------------------------------------------------------------------------
# exports and cleanup
# ---------------------------------------------------------------------------


def test_markup_csv_carries_disposition_columns(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "accepted", "actor": "CB", "assigned_to": "drafting"},
    )

    response = db_client.get(f"/bluebeam/review-sets/{set_id}/markups.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]

    lines = response.text.strip().splitlines()
    assert "review_disposition" in lines[0]
    assert len(lines) == 4  # header + the 3 unique markups across both reviewers
    assert any("accepted" in line and "drafting" in line for line in lines[1:])


def test_deleting_a_review_set_takes_its_markups_with_it(db_client):
    set_id = _seeded_set(db_client)
    response = db_client.delete(f"/bluebeam/review-sets/{set_id}")
    assert response.status_code == 200
    assert response.json()["markup_items_deleted"] == 3
    assert db_client.get(f"/bluebeam/review-sets/{set_id}").status_code == 404


def test_listing_review_sets_filters_by_status(db_client):
    open_id = _seeded_set(db_client)
    done_id = _seeded_set(db_client)
    _resolve_everything(db_client, done_id)
    db_client.post(f"/bluebeam/review-sets/{done_id}/approve", json={"approved_by": "CB"})

    approved = db_client.get("/bluebeam/review-sets", params={"status": "approved"}).json()
    assert [r["id"] for r in approved] == [done_id]

    everything = db_client.get("/bluebeam/review-sets").json()
    assert {r["id"] for r in everything} == {open_id, done_id}


def test_missing_review_set_is_404_everywhere(db_client):
    assert db_client.get("/bluebeam/review-sets/rev-nope").status_code == 404
    assert db_client.post("/bluebeam/review-sets/rev-nope/consolidate").status_code == 404
    assert db_client.get("/bluebeam/review-sets/rev-nope/approval").status_code == 404
    assert db_client.delete("/bluebeam/review-sets/rev-nope").status_code == 404


# ---------------------------------------------------------------------------
# outbound stamping (local-path in, local-path out)
# ---------------------------------------------------------------------------


def test_stamp_writes_callouts_to_a_local_output_path(db_client, tmp_path):
    from app.bluebeam_markup_io import extract_markups

    source = tmp_path / "plans.pdf"
    source.write_bytes(blank_set(2))
    out = tmp_path / "plans_stamped.pdf"

    response = db_client.post(
        "/bluebeam/stamp",
        json={
            "pdf_path": str(source),
            "output_path": str(out),
            "control_points": [
                {"model_x": 0, "model_y": 0, "page_x": 100, "page_y": 100},
                {"model_x": 1000, "model_y": 0, "page_x": 600, "page_y": 100},
            ],
            "items": [
                {"label": "INV-2-18", "tag": "INV-2-18", "model_x": 400, "model_y": 200,
                 "detail": ["3/C 500 kcmil Cu XHHW-2"]},
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stamped_count"] == 1
    assert body["calibration"]["scale"] == 0.5
    assert body["placements"][0]["page_x"] == 300.0
    assert "validation_gate" in body

    assert out.exists()
    markups, _ = extract_markups(out.read_bytes())
    assert markups[0].custom["/SolarCalcTag"] == "INV-2-18"


def test_stamp_requires_exactly_two_control_points(db_client, tmp_path):
    source = tmp_path / "plans.pdf"
    source.write_bytes(blank_set(1))
    response = db_client.post(
        "/bluebeam/stamp",
        json={
            "pdf_path": str(source),
            "output_path": str(tmp_path / "out.pdf"),
            "control_points": [{"model_x": 0, "model_y": 0, "page_x": 1, "page_y": 1}],
            "items": [{"label": "X", "model_x": 1, "model_y": 1}],
        },
    )
    assert response.status_code == 422
    assert "exactly 2 control points" in response.json()["detail"]


def test_stamp_reports_a_missing_input_file(db_client, tmp_path):
    response = db_client.post(
        "/bluebeam/stamp",
        json={
            "pdf_path": str(tmp_path / "nope.pdf"),
            "output_path": str(tmp_path / "out.pdf"),
            "items": [{"label": "X", "page_x": 1, "page_y": 1}],
        },
    )
    assert response.status_code == 400
    assert "file not found" in response.json()["detail"]


def test_stamp_schedule_needs_no_calibration(db_client, tmp_path):
    source = tmp_path / "plans.pdf"
    source.write_bytes(blank_set(1))
    out = tmp_path / "plans_sched.pdf"

    response = db_client.post(
        "/bluebeam/stamp-schedule",
        json={
            "pdf_path": str(source),
            "output_path": str(out),
            "title": "CONDUCTOR SCHEDULE",
            "rows": [["CKT", "SIZE"], ["INV-2-18", "500 kcmil Cu"]],
            "corner": "bottom-left",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["row_count"] == 2
    assert out.exists()


def test_reasserting_the_same_disposition_does_not_pad_the_audit_trail(db_client):
    """A bulk action sweeps up rows already in the target disposition. Logging
    those as changes would bury the decisions that actually happened."""
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]
    for _ in range(2):
        db_client.patch(
            f"/bluebeam/markups/{item['id']}/disposition",
            json={"disposition": "accepted", "actor": "CB"},
        )
    audit = db_client.get(f"/bluebeam/markups/{item['id']}/audit").json()
    assert [(a["from_disposition"], a["to_disposition"]) for a in audit] == [("open", "accepted")]


def test_editing_the_response_alone_is_still_recorded(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "deferred", "actor": "CB", "response": "next revision"},
    )
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "deferred", "actor": "CB", "response": "next revision — confirmed with EOR"},
    )
    audit = db_client.get(f"/bluebeam/markups/{item['id']}/audit").json()
    assert len(audit) == 2
    assert audit[1]["note"] == "next revision — confirmed with EOR"


def test_reasserting_a_terminal_disposition_keeps_the_original_resolved_time(db_client):
    set_id = _seeded_set(db_client)
    item = _markups(db_client, set_id)[0]
    db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition", json={"disposition": "accepted", "actor": "CB"}
    )
    first = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "incorporated", "actor": "CB"},
    ).json()
    again = db_client.patch(
        f"/bluebeam/markups/{item['id']}/disposition",
        json={"disposition": "incorporated", "actor": "someone else"},
    ).json()
    assert again["resolved_at"] == first["resolved_at"]
    assert again["resolved_by"] == "CB"
