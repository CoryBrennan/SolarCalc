"""Unit-level tests for the Bluebeam integration's four engines: markup
extraction, multi-reviewer consolidation, approval/revision logic, and
stamping design data onto a plan set.

The API-level walkthrough lives in test_bluebeam_api.py; this file is the
part that can fail for a reason worth reading, so the assertions here are
about PDF structure and rules rather than status codes.
"""

from __future__ import annotations

import io
import math

import pytest
from pypdf import PdfReader

from app.bluebeam_consolidate import (
    ConsolidationError,
    Submission,
    check_compatibility,
    consolidate,
)
from app.bluebeam_markup_io import (
    BluebeamMarkupError,
    extract_markups,
    markups_to_csv,
    page_geometry,
)
from app.bluebeam_review import (
    ACCEPTED,
    DEFERRED,
    INCORPORATED,
    OPEN,
    REJECTED,
    ReviewStateError,
    approval_gate,
    carry_forward_dispositions,
    diff_rounds,
    requires_response,
    rollup,
    validate_transition,
)
from app.bluebeam_stamp import (
    ControlPoint,
    PlanTransform,
    StampError,
    StampItem,
    stamp_markups,
    stamp_schedule,
)
from tests.bluebeam_pdf_fixtures import SHEET_H, SHEET_W, blank_set, marked_set, noise_set


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_extract_reads_author_subject_contents_and_page():
    pdf = marked_set(
        "J. Kowalski",
        [{"page": 1, "rect": [100, 100, 220, 170], "contents": "Conduit clash", "nm": "g1"}],
    )
    markups, pages = extract_markups(pdf, source_label="Reviewer A")

    assert pages == 3
    assert len(markups) == 1
    m = markups[0]
    assert m.author == "J. Kowalski"
    assert m.subject == "Cloud+"
    assert m.contents == "Conduit clash"
    assert m.page == 1 and m.page_label == 2
    assert m.source_label == "Reviewer A"
    assert m.colour == "#ff0000"


def test_extract_skips_popups_and_form_widgets():
    """Revu's Markups List shows neither; nor should the review table."""
    markups, _ = extract_markups(noise_set())
    assert markups == []


def test_extract_normalises_reversed_rect_corners():
    pdf = marked_set(
        "A",
        [{"page": 0, "rect": [100, 100, 220, 170], "contents": "x", "reversed_rect": True}],
    )
    markups, _ = extract_markups(pdf)
    assert markups[0].rect == [100.0, 100.0, 220.0, 170.0]


def test_status_comes_from_reply_chain_with_newest_winning():
    pdf = marked_set(
        "A. Ruiz",
        [{"page": 0, "rect": [10, 10, 60, 60], "contents": "check", "nm": "g1"}],
        status_replies=[
            {"page": 0, "parent_nm": "g1", "state": "None", "when": "D:20260815100000-05'00'"},
            {
                "page": 0,
                "parent_nm": "g1",
                "state": "Accepted",
                "author": "C. Brennan",
                "when": "D:20260816110000-05'00'",
            },
        ],
    )
    markups, _ = extract_markups(pdf)

    assert len(markups) == 1, "reply annotations must not be listed as markups"
    assert markups[0].status == "Accepted"
    assert markups[0].status_author == "C. Brennan"


def test_unknown_bluebeam_keys_are_preserved_rather_than_dropped():
    pdf = marked_set(
        "A",
        [{
            "page": 0, "rect": [1, 1, 2, 2], "contents": "x",
            "custom": {"/BluebeamCustomColumn": "Discipline=Electrical"},
        }],
    )
    markups, _ = extract_markups(pdf)
    assert markups[0].custom["/BluebeamCustomColumn"] == "Discipline=Electrical"


def test_markup_without_nm_falls_back_to_content_hash_key():
    pdf = marked_set("A", [{"page": 0, "rect": [1, 1, 2, 2], "contents": "no guid"}])
    markups, _ = extract_markups(pdf)
    assert markups[0].nm is None
    assert markups[0].key.startswith("h:")


def test_extract_rejects_a_non_pdf():
    with pytest.raises(BluebeamMarkupError):
        extract_markups(b"this is not a PDF at all")


def test_page_geometry_reports_real_sheet_size():
    assert page_geometry(blank_set(2)) == [(SHEET_W, SHEET_H), (SHEET_W, SHEET_H)]


def test_csv_includes_the_columns_revu_has_no_concept_of():
    pdf = marked_set("A", [{"page": 0, "rect": [1, 1, 2, 2], "contents": "note"}])
    markups, _ = extract_markups(pdf, source_label="Reviewer A")
    row = markups[0].to_dict()
    row.update({"review_disposition": "accepted", "assigned_to": "CB", "response": "will revise"})
    csv_text = markups_to_csv([row])

    header, first = csv_text.splitlines()[:2]
    assert "review_disposition" in header and "assigned_to" in header
    assert first.startswith("1,")  # page is 1-based in the export
    assert "accepted" in first and "Reviewer A" in first


# ---------------------------------------------------------------------------
# consolidation
# ---------------------------------------------------------------------------


def _three_reviewers():
    a = marked_set(
        "A. Ruiz",
        [
            {"page": 0, "rect": [100, 100, 220, 170], "contents": "shared prior comment", "nm": "shared"},
            {"page": 1, "rect": [300, 300, 400, 380], "contents": "conduit clashes with pile", "nm": "a2"},
        ],
        status_replies=[{"page": 0, "parent_nm": "shared", "state": "Accepted"}],
    )
    b = marked_set(
        "B. Chen",
        [
            {"page": 0, "rect": [100, 100, 220, 170], "contents": "shared prior comment", "nm": "shared"},
            {"page": 1, "rect": [350, 340, 470, 420], "contents": "reroute instead", "nm": "b2"},
        ],
    )
    c = marked_set("C. Okafor", [{"page": 2, "rect": [50, 600, 200, 700], "contents": "missing ground bar"}])
    return [
        Submission("A. Ruiz", a),
        Submission("B. Chen", b),
        Submission("C. Okafor", c),
    ]


def test_consolidate_merges_every_reviewer_onto_one_set():
    result = consolidate(blank_set(), _three_reviewers())

    assert result.page_count == 3
    assert len(result.markups) == 4  # 5 submitted, 1 is a duplicate of another
    assert result.summary()["markups_by_source"] == {
        "A. Ruiz": 2, "B. Chen": 1, "C. Okafor": 1
    }
    assert result.skipped == []


def test_consolidated_markups_survive_as_live_annotations_with_appearances():
    """Nothing is flattened: the merged file's markups re-extract cleanly and
    keep the appearance streams that make them visible."""
    result = consolidate(blank_set(), _three_reviewers())
    remerged, pages = extract_markups(result.pdf)

    assert pages == 3
    assert len(remerged) == 4
    assert {m.contents for m in remerged} == {
        "shared prior comment", "conduit clashes with pile",
        "reroute instead", "missing ground bar",
    }
    reader = PdfReader(io.BytesIO(result.pdf))
    with_appearance = sum(
        1 for p in reader.pages for a in (p.get("/Annots") or []) if "/AP" in a.get_object()
    )
    assert with_appearance == 4, "appearance streams lost in the cross-document clone"


def test_each_reviewer_becomes_a_toggleable_pdf_layer():
    result = consolidate(blank_set(), _three_reviewers())
    assert result.layers == ["A. Ruiz", "B. Chen", "C. Okafor"]

    reader = PdfReader(io.BytesIO(result.pdf))
    ocgs = reader.trailer["/Root"]["/OCProperties"]["/OCGs"]
    assert [str(o.get_object()["/Name"]) for o in ocgs] == ["A. Ruiz", "B. Chen", "C. Okafor"]

    layered = sum(
        1 for p in reader.pages for a in (p.get("/Annots") or []) if "/OC" in a.get_object()
    )
    assert layered == 5, "every cloned annotation (markups + the status reply) needs a layer"


def test_source_attribution_is_stamped_onto_each_merged_markup():
    result = consolidate(blank_set(), _three_reviewers())
    remerged, _ = extract_markups(result.pdf)
    by_contents = {m.contents: m for m in remerged}
    assert by_contents["reroute instead"].custom["/SolarCalcSource"] == "B. Chen"
    assert by_contents["missing ground bar"].custom["/SolarCalcSource"] == "C. Okafor"


def test_same_markup_in_two_copies_is_merged_once_and_reported():
    result = consolidate(blank_set(), _three_reviewers())
    assert len(result.duplicates) == 1
    dup = result.duplicates[0]
    assert dup["key"] == "shared"
    assert dup["first_from"] == "A. Ruiz"
    assert dup["also_from"] == "B. Chen"


def test_status_replies_survive_the_merge_and_stay_attached():
    result = consolidate(blank_set(), _three_reviewers())
    remerged, _ = extract_markups(result.pdf)
    accepted = [m for m in remerged if m.status == "Accepted"]
    assert len(accepted) == 1
    assert accepted[0].contents == "shared prior comment"


def test_overlapping_markups_from_different_reviewers_are_flagged_not_merged():
    """Two people circling the same conduit are two opinions — both are kept."""
    result = consolidate(blank_set(), _three_reviewers())
    assert len(result.overlap_clusters) == 1
    cluster = result.overlap_clusters[0]
    assert cluster["page_label"] == 2
    assert sorted(cluster["sources"]) == ["A. Ruiz", "B. Chen"]
    assert len(cluster["markups"]) == 2
    # and both are still present in the merged output
    remerged, _ = extract_markups(result.pdf)
    assert sum(1 for m in remerged if m.page == 1) == 2


def test_overlap_within_one_reviewer_is_not_flagged():
    """A cloud plus its own callout overlaps by design."""
    solo = marked_set(
        "A. Ruiz",
        [
            {"page": 0, "rect": [100, 100, 200, 200], "contents": "cloud", "nm": "x1"},
            {"page": 0, "rect": [150, 150, 250, 250], "contents": "callout", "nm": "x2"},
        ],
    )
    result = consolidate(blank_set(), [Submission("A. Ruiz", solo)])
    assert result.overlap_clusters == []


def test_page_count_mismatch_is_refused():
    stale = marked_set("D. Stale", [{"page": 0, "rect": [1, 1, 2, 2], "contents": "x"}], pages=2)
    with pytest.raises(ConsolidationError, match="3 pages, this set has 2"):
        consolidate(blank_set(3), [Submission("D. Stale", stale)])


def test_page_size_mismatch_is_refused():
    wrong = marked_set(
        "E. Wrong", [{"page": 0, "rect": [1, 1, 2, 2], "contents": "x"}],
        width=1224, height=792,
    )
    with pytest.raises(ConsolidationError, match="differ in size"):
        consolidate(blank_set(3), [Submission("E. Wrong", wrong)])


def test_check_compatibility_reports_without_raising():
    stale = marked_set("D", [{"page": 0, "rect": [1, 1, 2, 2], "contents": "x"}], pages=2)
    problems = check_compatibility(blank_set(3), [Submission("D. Stale", stale)])
    assert len(problems) == 1
    assert problems[0]["issue"] == "page_count_mismatch"
    assert problems[0]["label"] == "D. Stale"
    assert check_compatibility(blank_set(3), []) == []


def test_consolidate_with_no_submissions_is_an_error():
    with pytest.raises(ConsolidationError, match="no submissions"):
        consolidate(blank_set(), [])


# ---------------------------------------------------------------------------
# review: transitions, diffing, approval gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,target",
    [(OPEN, ACCEPTED), (OPEN, REJECTED), (OPEN, DEFERRED),
     (ACCEPTED, INCORPORATED), (DEFERRED, ACCEPTED), (INCORPORATED, OPEN), (REJECTED, OPEN)],
)
def test_legal_disposition_moves(start, target):
    validate_transition(start, target)


@pytest.mark.parametrize(
    "start,target",
    [(OPEN, INCORPORATED), (REJECTED, INCORPORATED), (REJECTED, ACCEPTED), (DEFERRED, INCORPORATED)],
)
def test_illegal_disposition_moves_are_refused(start, target):
    with pytest.raises(ReviewStateError):
        validate_transition(start, target)


def test_open_cannot_skip_straight_to_incorporated():
    """The whole point of the two-step: 'accepted' and 'drawn' are different facts."""
    with pytest.raises(ReviewStateError, match="legal moves"):
        validate_transition(OPEN, INCORPORATED)


def test_unknown_disposition_is_refused():
    with pytest.raises(ReviewStateError, match="unknown disposition"):
        validate_transition(OPEN, "probably-fine")


def test_only_rejection_and_deferral_demand_a_written_reason():
    assert requires_response(REJECTED) and requires_response(DEFERRED)
    assert not requires_response(ACCEPTED)
    assert not requires_response(INCORPORATED)


def _round(specs, author="A. Ruiz"):
    markups, _ = extract_markups(marked_set(author, specs), source_label=author)
    return markups


def test_round_diff_classifies_added_modified_unchanged_withdrawn():
    r1 = _round([
        {"page": 0, "rect": [10, 10, 60, 60], "contents": "keep me", "nm": "k1"},
        {"page": 0, "rect": [70, 70, 90, 90], "contents": "edit me", "nm": "k2"},
        {"page": 1, "rect": [10, 10, 60, 60], "contents": "drop me", "nm": "k3"},
    ])
    r2 = _round([
        {"page": 0, "rect": [10, 10, 60, 60], "contents": "keep me", "nm": "k1"},
        {"page": 0, "rect": [70, 70, 90, 90], "contents": "edited text", "nm": "k2"},
        {"page": 2, "rect": [10, 10, 60, 60], "contents": "brand new", "nm": "k4"},
    ])
    d = diff_rounds(r1, r2).to_dict()

    assert d["counts"] == {"added": 1, "modified": 1, "unchanged": 1, "withdrawn": 1}
    assert d["added"][0]["contents"] == "brand new"
    assert d["modified"][0]["previous"]["contents"] == "edit me"
    assert d["withdrawn"][0]["contents"] == "drop me"
    assert d["unmatchable_count"] == 0


def test_moving_a_markup_registers_as_modified_not_replaced():
    r1 = _round([{"page": 0, "rect": [10, 10, 60, 60], "contents": "same", "nm": "k1"}])
    r2 = _round([{"page": 0, "rect": [400, 400, 450, 450], "contents": "same", "nm": "k1"}])
    d = diff_rounds(r1, r2).to_dict()
    assert d["counts"]["modified"] == 1 and d["counts"]["added"] == 0


def test_missing_guids_are_reported_as_an_explicit_diff_limitation():
    r1 = _round([{"page": 0, "rect": [10, 10, 60, 60], "contents": "no guid"}])
    r2 = _round([{"page": 0, "rect": [400, 400, 450, 450], "contents": "no guid"}])
    d = diff_rounds(r1, r2).to_dict()

    assert d["counts"] == {"added": 1, "modified": 0, "unchanged": 0, "withdrawn": 1}
    assert d["unmatchable_count"] == 2
    assert "no Revu /NM GUID" in d["unmatchable_note"]


def test_unchanged_markups_keep_their_disposition_across_rounds():
    r1 = _round([{"page": 0, "rect": [10, 10, 60, 60], "contents": "same", "nm": "k1"}])
    r2 = _round([{"page": 0, "rect": [10, 10, 60, 60], "contents": "same", "nm": "k1"}])
    carried = carry_forward_dispositions(
        diff_rounds(r1, r2), {"k1": {"disposition": ACCEPTED, "response": "agreed"}}
    )
    assert carried["k1"]["disposition"] == ACCEPTED
    assert carried["k1"]["carried_from_previous_round"] is True


def test_an_edited_markup_reopens_and_records_what_it_was():
    r1 = _round([{"page": 0, "rect": [10, 10, 60, 60], "contents": "original ask", "nm": "k1"}])
    r2 = _round([{"page": 0, "rect": [10, 10, 60, 60], "contents": "different ask", "nm": "k1"}])
    carried = carry_forward_dispositions(
        diff_rounds(r1, r2), {"k1": {"disposition": ACCEPTED, "response": "agreed"}}
    )
    assert carried["k1"]["disposition"] == OPEN
    assert carried["k1"]["reopened_from"] == ACCEPTED
    assert "changed this markup" in carried["k1"]["reset_reason"]


def test_rollup_counts_only_terminal_dispositions_as_resolved():
    summary = rollup([
        {"disposition": INCORPORATED}, {"disposition": REJECTED},
        {"disposition": ACCEPTED}, {"disposition": DEFERRED}, {},
    ])
    assert summary["total"] == 5
    assert summary["resolved"] == 2
    assert summary["outstanding"] == 3
    assert summary["percent_resolved"] == 40.0
    assert summary["by_disposition"][OPEN] == 1


def test_approval_blocked_by_accepted_but_not_yet_drawn():
    gate = approval_gate([
        {"key": "a", "disposition": INCORPORATED},
        {"key": "b", "disposition": ACCEPTED, "contents": "not drawn yet"},
    ])
    assert gate["ready_to_approve"] is False
    blocker = next(b for b in gate["blockers"] if b["disposition"] == ACCEPTED)
    assert "hasn't been confirmed" in blocker["reason"]
    assert blocker["markups"][0]["contents"] == "not drawn yet"


def test_approval_blocked_by_open_and_deferred_items():
    gate = approval_gate([
        {"key": "a", "disposition": DEFERRED}, {"key": "b"},
    ])
    assert {b["disposition"] for b in gate["blockers"]} == {DEFERRED, OPEN}
    assert gate["blocker_count"] == 2


def test_approval_clears_when_everything_is_terminal():
    gate = approval_gate([
        {"key": "a", "disposition": INCORPORATED}, {"key": "b", "disposition": REJECTED},
    ])
    assert gate["ready_to_approve"] is True
    assert gate["blockers"] == []


def test_an_empty_review_set_does_not_auto_approve():
    gate = approval_gate([])
    assert gate["ready_to_approve"] is False
    assert gate["empty"] is True


# ---------------------------------------------------------------------------
# stamping design data onto a plan set
# ---------------------------------------------------------------------------


def test_two_control_points_recover_scale_rotation_and_offset():
    t = PlanTransform.from_control_points(
        ControlPoint(model_x=1000, model_y=500, page_x=400, page_y=300),
        ControlPoint(model_x=5000, model_y=500, page_x=400, page_y=1900),
    )
    assert t.scale == pytest.approx(0.4)
    assert math.degrees(t.rotation_rad) == pytest.approx(90.0)
    assert t.residual_error_pt < 1e-6


def test_transform_places_a_third_point_consistently():
    t = PlanTransform.from_control_points(
        ControlPoint(0, 0, 100, 100), ControlPoint(1000, 0, 600, 100)
    )
    # 0.5x scale, no rotation: (400, 200) model -> (300, 200) page
    assert t.apply(400, 200) == pytest.approx((300.0, 200.0))


def test_degenerate_calibration_is_refused_rather_than_fudged():
    a = ControlPoint(1000, 500, 400, 300)
    with pytest.raises(StampError, match="model-space location"):
        PlanTransform.from_control_points(a, ControlPoint(1000, 500, 900, 900))
    with pytest.raises(StampError, match="page location"):
        PlanTransform.from_control_points(a, ControlPoint(5000, 500, 400, 300))


def test_stamps_land_as_readable_markups_tagged_to_their_equipment():
    t = PlanTransform.from_control_points(
        ControlPoint(0, 0, 100, 100), ControlPoint(1000, 0, 600, 100)
    )
    pdf, placements = stamp_markups(
        blank_set(2),
        [
            StampItem(label="INV-2-18", tag="INV-2-18", model_x=400, model_y=200,
                      detail=["3/C 500 kcmil Cu XHHW-2", '4" PVC Sch40']),
            StampItem(label="XFMR-2", tag="XFMR-2", page=1, page_x=500, page_y=700,
                      detail=["2500 kVA"]),
        ],
        transform=t,
    )

    assert placements[0]["page_x"] == pytest.approx(300.0)
    assert placements[0]["from_model_coords"] is True
    assert placements[1]["from_model_coords"] is False

    markups, _ = extract_markups(pdf)
    assert len(markups) == 2
    assert {m.custom["/SolarCalcTag"] for m in markups} == {"INV-2-18", "XFMR-2"}
    assert all(m.author == "Solar Calc Engine" for m in markups)
    assert all(m.custom["/SolarCalcOrigin"] == "solar-calc-engine" for m in markups)
    assert "500 kcmil" in markups[0].contents


def test_stamps_carry_their_own_appearance_stream():
    """So the callouts render everywhere, not only after Revu regenerates them."""
    pdf, _ = stamp_markups(blank_set(1), [StampItem(label="X", page_x=10, page_y=10)])
    reader = PdfReader(io.BytesIO(pdf))
    annot = (reader.pages[0]["/Annots"])[0].get_object()
    assert "/AP" in annot
    assert b"Helv" in annot["/AP"]["/N"].get_data()


def test_a_stamp_with_no_coordinates_is_refused():
    with pytest.raises(StampError, match="no page or model coordinates"):
        stamp_markups(blank_set(1), [StampItem(label="orphan")])


def test_model_coordinates_without_a_calibration_are_refused():
    with pytest.raises(StampError, match="no calibration"):
        stamp_markups(blank_set(1), [StampItem(label="x", model_x=1, model_y=2)])


def test_a_stamp_off_the_end_of_the_set_is_refused():
    with pytest.raises(StampError, match="set has 2 page"):
        stamp_markups(blank_set(2), [StampItem(label="x", page=9, page_x=1, page_y=1)])


def test_schedule_needs_no_calibration_and_lands_in_the_named_corner():
    pdf, info = stamp_schedule(
        blank_set(1), "CONDUCTOR SCHEDULE",
        [["CKT", "SIZE", "RACEWAY"], ["INV-2-18", "500 kcmil Cu", '4" PVC']],
        corner="top-right",
    )
    assert info["row_count"] == 2
    assert info["page_x"] > SHEET_W / 2 and info["page_y"] > SHEET_H / 2

    markups, _ = extract_markups(pdf)
    assert len(markups) == 1
    assert markups[0].subject == "Design Schedule"
    assert "CONDUCTOR SCHEDULE" in markups[0].contents


def test_schedule_rejects_an_unknown_corner():
    with pytest.raises(StampError, match="corner must be"):
        stamp_schedule(blank_set(1), "T", [["a"]], corner="middle")


def test_stamped_set_can_be_consolidated_with_reviewer_markups():
    """The two halves of the integration have to compose: stamp design data,
    issue it, get it back marked up, merge."""
    stamped, _ = stamp_markups(
        blank_set(3), [StampItem(label="INV-2-18", tag="INV-2-18", page_x=100, page_y=100)]
    )
    review = marked_set("A. Ruiz", [{"page": 0, "rect": [500, 500, 600, 600],
                                     "contents": "conflicts with INV-2-18", "nm": "r1"}])
    result = consolidate(stamped, [Submission("A. Ruiz", review)])

    remerged, _ = extract_markups(result.pdf)
    assert len(remerged) == 2
    engine = [m for m in remerged if m.custom.get("/SolarCalcOrigin")]
    human = [m for m in remerged if m.custom.get("/SolarCalcSource") == "A. Ruiz"]
    assert len(engine) == 1 and len(human) == 1
    assert engine[0].custom["/SolarCalcTag"] == "INV-2-18"


def test_fingerprint_survives_a_storage_round_trip():
    """The review table stores subtype without its leading slash. A markup
    rebuilt from that row must fingerprint identically to a freshly extracted
    one, or an untouched comment reads as modified every round."""
    from dataclasses import replace

    markups, _ = extract_markups(
        marked_set("A", [{"page": 0, "rect": [10, 10, 60, 60], "contents": "same", "nm": "k1"}])
    )
    fresh = markups[0]
    rebuilt = replace(fresh, subtype=fresh.subtype.lstrip("/"))
    assert rebuilt.content_fingerprint() == fresh.content_fingerprint()


def test_fingerprint_ignores_which_reviewers_copy_it_arrived_in():
    """A comment inherited by several copies differs only by /T. Folding author
    into the fingerprint made upload order decide whether it looked modified."""
    spec = [{"page": 0, "rect": [10, 10, 60, 60], "contents": "shared", "nm": "k1"}]
    from_a, _ = extract_markups(marked_set("A. Ruiz", spec))
    from_b, _ = extract_markups(marked_set("B. Chen", spec))
    assert from_a[0].content_fingerprint() == from_b[0].content_fingerprint()
    assert diff_rounds(from_a, from_b).to_dict()["counts"]["unchanged"] == 1
