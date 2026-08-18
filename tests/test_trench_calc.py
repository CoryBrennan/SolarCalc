"""Tests for the trench integration seam (app/trench_calc.py) -- the part
that pulls real conductor, conduit, and current data out of this project's
raceway schedule and hands it to the numerical solver.

The physics itself is covered in tests/test_trench_thermal.py; what matters
here is that the right numbers cross the seam: no re-entered inputs, no
re-applied NEC derating, and no silently-accepted wrong-units soil data.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ProjectInput, RacewayRun
from app.raceway_calc import compute_raceway_run
from app.trench_calc import (
    TrenchConditions,
    TrenchDesignRequest,
    TrenchFixedLayout,
    TrenchInputError,
    TrenchRunSettings,
    compute_trench_design,
    conductor_bare_diameter_in,
    conductor_insulated_diameter_in,
    conduit_inner_diameter_in,
    conduit_outer_diameter_in,
    conduits_from_run,
    validate_conditions,
)

client = TestClient(app)


def _run(tag="AC-1", **overrides):
    base = dict(
        tag=tag, raceway_type="conduit", circuit_type="ac", current_a=253.0,
        conductor_count=3, insulation_rating=90, conductor_insulation="THHN_THWN2",
        length_ft=400.0, voltage_v=800.0, conduit_material="PVC_SCH40",
    )
    base.update(overrides)
    return RacewayRun(**base)


def _project(runs):
    project = ProjectInput()
    project.raceway_runs = runs
    return project


def _fast_conditions(**overrides):
    """Fixed-layout mode: one solve instead of a full search, so the seam's
    behaviour can be tested without paying for the optimizer."""
    base = dict(
        soil_resistivity_source="astm_d5334_thermal_probe",
        fixed_layout=TrenchFixedLayout(rows=1, spacing_horizontal_in=12.0),
        include_drawing=False,
    )
    base.update(overrides)
    return TrenchConditions(**base)


# ---------------------------------------------------------------------------
# Geometry derived from the app's own NEC tables
# ---------------------------------------------------------------------------
def test_conductor_diameters_match_published_nec_chapter_9_values():
    """Bare diameter comes from circular mils plus a stranding factor, and
    insulated diameter from the Table 5 areas raceway_calc already fills
    conduits with -- both must land on the published dimensions, or every
    downstream thermal resistance is built on the wrong geometry."""
    # Chapter 9 Table 8: 500 kcmil stranded copper is 0.813 in overall.
    assert conductor_bare_diameter_in("500 kcmil") == pytest.approx(0.813, abs=0.01)
    assert conductor_bare_diameter_in("4/0 AWG") == pytest.approx(0.528, abs=0.01)
    # Chapter 9 Table 5: 500 kcmil THHN is 0.928 in over the nylon jacket.
    assert conductor_insulated_diameter_in("500 kcmil", "THHN_THWN2") == pytest.approx(0.928, abs=0.01)


def test_insulated_diameter_exceeds_bare_diameter():
    for size in ("6 AWG", "1/0 AWG", "350 kcmil", "750 kcmil"):
        assert (conductor_insulated_diameter_in(size, "THHN_THWN2")
                > conductor_bare_diameter_in(size))


def test_thicker_insulation_type_gives_a_larger_diameter():
    """USE-2/RHW-2 is the thicker-jacket PV cable; raceway_calc already
    accounts for that in its fill areas, so the thermal side inherits it."""
    assert (conductor_insulated_diameter_in("1/0 AWG", "USE2_RHW2")
            > conductor_insulated_diameter_in("1/0 AWG", "THHN_THWN2"))


def test_conduit_diameters_match_published_dimensions():
    # 2 in PVC Sch 40: 2.067 in ID, 2.375 in OD (IPS).
    assert conduit_inner_diameter_in("2", "PVC_SCH40") == pytest.approx(2.067, abs=0.03)
    assert conduit_outer_diameter_in("2", "PVC_SCH40") == pytest.approx(2.375, abs=0.03)
    # Sch 80 is the same OD with a thicker wall, so a smaller bore.
    assert conduit_inner_diameter_in("2", "PVC_SCH80") < conduit_inner_diameter_in("2", "PVC_SCH40")


# ---------------------------------------------------------------------------
# What crosses the seam
# ---------------------------------------------------------------------------
def test_conduit_uses_the_schedule_current_undereated():
    """The NEC derate factors change which conductor is selected, not how much
    current physically flows. Applying them to the I^2*R heat term would be
    double-counting -- the handoff's explicit 'do NOT reapply' requirement."""
    run = _run(current_a=253.0)
    conduits, sizing = conduits_from_run(run, TrenchRunSettings(), ambient_c=35.0)
    assert conduits[0].I_design == 253.0
    # The derates are still reported -- they're just not folded into current.
    assert sizing["temp_correction_factor"] < 1.0
    assert sizing["required_ampacity_a"] > run.current_a


def test_conductor_and_trade_size_come_from_the_raceway_panel():
    """No second sizing algorithm: the trench calc must use exactly what
    Raceway (24) already selected for the run."""
    run = _run()
    expected = compute_raceway_run(run, ambient_c=35.0)
    _conduits, sizing = conduits_from_run(run, TrenchRunSettings(), ambient_c=35.0)
    assert sizing["selected_conductor"] == expected["selected_conductor"]
    assert sizing["trade_size_in"] == expected["raceway"]["selected_trade_size_in"]


def test_conduit_count_multiplies_physical_conduits():
    conduits, _ = conduits_from_run(_run(), TrenchRunSettings(conduit_count=3), ambient_c=35.0)
    assert [c.id for c in conduits] == ["AC-1-1", "AC-1-2", "AC-1-3"]


def test_single_conduit_keeps_the_bare_run_tag():
    conduits, _ = conduits_from_run(_run(), TrenchRunSettings(conduit_count=1), ambient_c=35.0)
    assert [c.id for c in conduits] == ["AC-1"]


def test_ccc_defaults_to_the_runs_own_conductor_count():
    conduits, _ = conduits_from_run(_run(conductor_count=4), TrenchRunSettings(), ambient_c=35.0)
    assert conduits[0].n_ccc == 4


def test_dc_run_gets_no_ac_effects():
    dc, _ = conduits_from_run(_run(circuit_type="dc"), TrenchRunSettings(), ambient_c=35.0)
    ac, _ = conduits_from_run(_run(circuit_type="ac"), TrenchRunSettings(), ambient_c=35.0)
    assert dc[0].ac_factor(1e-4) == (0.0, 0.0)
    assert ac[0].ac_factor(1e-4)[0] > 0


def test_metal_conduit_uses_metallic_air_space_constants():
    pvc, _ = conduits_from_run(_run(conduit_material="PVC_SCH40"), TrenchRunSettings(), 35.0)
    rmc, _ = conduits_from_run(_run(conduit_material="RMC"), TrenchRunSettings(), 35.0)
    assert pvc[0].duct_class == "nonmetallic"
    assert rmc[0].duct_class == "metallic"
    assert rmc[0].r_duct_wall < pvc[0].r_duct_wall / 100


def test_aluminum_run_reselects_from_the_aluminum_column():
    """raceway_calc sizes copper; an aluminum trench run re-selects against the
    SAME required ampacity rather than inventing a new derate chain."""
    _conduits, sizing = conduits_from_run(
        _run(), TrenchRunSettings(conductor_material="AL"), ambient_c=35.0)
    cu = compute_raceway_run(_run(), ambient_c=35.0)
    assert sizing["required_ampacity_a"] == cu["required_ampacity_a"]
    assert sizing["selected_conductor"] != cu["selected_conductor"]
    assert sizing["schedule_conductor_cu"] == cu["selected_conductor"]


def test_unsizable_run_is_rejected_rather_than_guessed():
    with pytest.raises(TrenchInputError, match="Raceway"):
        conduits_from_run(_run(current_a=5000.0), TrenchRunSettings(), ambient_c=35.0)


# ---------------------------------------------------------------------------
# Soil resistivity validation
# ---------------------------------------------------------------------------
def test_electrical_resistivity_source_is_rejected():
    """Thermal (ASTM D5334, degC*cm/W) and electrical (Wenner, ohm*m) soil
    resistivity overlap numerically, so a wrong-test value can't be caught by
    range-checking -- the declared source is the only defence."""
    with pytest.raises(TrenchInputError, match="ELECTRICAL"):
        validate_conditions(TrenchConditions(soil_resistivity_source="wenner_electrical"))


def test_assumed_soil_resistivity_warns():
    warnings = validate_conditions(TrenchConditions(soil_resistivity_source="assumed_default"))
    assert any("assumed default" in w for w in warnings)


def test_implausible_resistivity_magnitude_warns():
    warnings = validate_conditions(TrenchConditions(
        soil_resistivity_source="astm_d5334_thermal_probe", native_soil_rho_cm=2000.0))
    assert any("outside the" in w for w in warnings)


def test_backfill_worse_than_native_warns():
    warnings = validate_conditions(TrenchConditions(
        soil_resistivity_source="astm_d5334_thermal_probe",
        native_soil_rho_cm=60.0, backfill_rho_cm=120.0))
    assert any("swapped" in w for w in warnings)


# ---------------------------------------------------------------------------
# Grouping and whole-project orchestration
# ---------------------------------------------------------------------------
def test_runs_group_into_separate_trenches():
    request = TrenchDesignRequest(
        project=_project([_run("A"), _run("B"), _run("C")]),
        settings={
            "A": TrenchRunSettings(trench_group="TR-1"),
            "B": TrenchRunSettings(trench_group="TR-1"),
            "C": TrenchRunSettings(trench_group="TR-2"),
        },
        conditions=_fast_conditions(),
    )
    result = compute_trench_design(request)
    assert set(result["trenches"]) == {"TR-1", "TR-2"}
    assert result["trenches"]["TR-1"]["conduit_count"] == 2
    assert result["trenches"]["TR-2"]["conduit_count"] == 1


def test_non_conduit_runs_are_skipped_not_silently_included():
    """v1 is direct-buried conduit only -- a tray or messenger run must be
    reported as skipped, never quietly treated as buried conduit."""
    request = TrenchDesignRequest(
        project=_project([_run("A"), _run("TRAY", raceway_type="cable_tray")]),
        conditions=_fast_conditions(),
    )
    result = compute_trench_design(request)
    assert [s["tag"] for s in result["skipped_runs"]] == ["TRAY"]
    assert "cable_tray" in result["skipped_runs"][0]["reason"]
    assert result["trenches"]["TRENCH-1"]["conduit_count"] == 1


def test_excluded_run_is_skipped():
    request = TrenchDesignRequest(
        project=_project([_run("A"), _run("B")]),
        settings={"B": TrenchRunSettings(include=False)},
        conditions=_fast_conditions(),
    )
    result = compute_trench_design(request)
    assert [s["tag"] for s in result["skipped_runs"]] == ["B"]


def test_no_trench_runs_returns_an_empty_report_not_an_error():
    request = TrenchDesignRequest(
        project=_project([_run("TRAY", raceway_type="cable_tray")]),
        conditions=_fast_conditions(),
    )
    result = compute_trench_design(request)
    assert result["trench_count"] == 0
    assert any("No raceway run" in w for w in result["warnings"])


def test_ambient_defaults_to_project_ashrae_temperature():
    project = _project([_run("A")])
    project.ashrae.max_design_temp_c = 44.0
    result = compute_trench_design(
        TrenchDesignRequest(project=project, conditions=_fast_conditions()))
    assert result["ambient_air_temp_c"] == 44.0


# ---------------------------------------------------------------------------
# The derating stack -- the reporting requirement that started this
# ---------------------------------------------------------------------------
def _single_trench(**condition_overrides):
    request = TrenchDesignRequest(
        project=_project([_run("A"), _run("B")]),
        settings={"A": TrenchRunSettings(trench_group="TR-1"),
                  "B": TrenchRunSettings(trench_group="TR-1")},
        conditions=_fast_conditions(**condition_overrides),
    )
    return compute_trench_design(request)["trenches"]["TR-1"]


def test_derating_mechanisms_are_reported_as_separate_multiplying_rows():
    """The two NEC factors and the trench factor are different physical
    mechanisms and must never be folded into one another -- a reviewer has to
    see each one and its product."""
    stack = _single_trench()["derating_stack"]["A"]
    factors = [row["factor"] for row in stack["rows"]]
    assert len(factors) == 3
    assert stack["combined_factor"] == pytest.approx(factors[0] * factors[1] * factors[2], abs=1e-3)
    assert stack["nec_only_factor"] == pytest.approx(factors[0] * factors[1], abs=1e-6)
    mechanisms = " ".join(row["mechanism"] for row in stack["rows"])
    assert "310.15(B)(1)" in mechanisms and "310.15(C)(1)" in mechanisms
    assert "Trench" in mechanisms


def test_trench_factor_never_credits_extra_ampacity():
    """Headroom above the design current is reported, but the derate factor is
    capped at 1.0 -- a roomy trench must not be used to justify a conductor
    smaller than the NEC chain already requires."""
    trench = _single_trench(fixed_layout=TrenchFixedLayout(rows=1, spacing_horizontal_in=48.0))
    assert trench["thermal_headroom_scale"] > 1.0
    assert trench["trench_derate_factor"] == 1.0
    stack = trench["derating_stack"]["A"]
    assert stack["conductor_with_trench"] == stack["conductor_nec_only"]
    assert stack["trench_governs"] is False


def test_crowded_trench_governs_over_the_nec_chain():
    """Six 253 A feeder conduits packed at 4 in on centre: the trench, not the
    NEC derate chain, is what forces a bigger conductor -- the case this whole
    module exists to catch, since no NEC table models it."""
    request = TrenchDesignRequest(
        project=_project([_run("A"), _run("B")]),
        settings={"A": TrenchRunSettings(trench_group="TR-1", conduit_count=3),
                  "B": TrenchRunSettings(trench_group="TR-1", conduit_count=3)},
        conditions=_fast_conditions(
            fixed_layout=TrenchFixedLayout(rows=1, spacing_horizontal_in=4.0)),
    )
    trench = compute_trench_design(request)["trenches"]["TR-1"]
    assert trench["conduit_count"] == 6
    assert trench["trench_derate_factor"] < 1.0
    stack = trench["derating_stack"]["A"]
    assert stack["required_ampacity_with_trench_a"] > stack["required_ampacity_nec_only_a"]
    assert stack["conductor_with_trench"] != stack["conductor_nec_only"]
    assert stack["trench_governs"] is True


def test_check_mode_reports_no_computed_minimum_spacing():
    """Nothing was minimized in check mode; reporting the as-drawn spacing as a
    'computed minimum' would be a fabricated result."""
    trench = _single_trench()
    assert trench["mode"] == "check"
    assert trench["layout"]["raw_computed_min_spacing_in"] is None
    assert trench["layout"]["spacing_governed_by"] == "as-drawn"


def test_failing_as_drawn_layout_is_reported_as_failing():
    trench = _single_trench(fixed_layout=TrenchFixedLayout(rows=1, spacing_horizontal_in=4.0))
    assert trench["solved"] is True
    assert trench["passes"] is False
    assert trench["max_conductor_temp_c"] > trench["target_temp_c"]
    assert any("as-drawn" in w for w in trench["warnings"])


def test_optimize_mode_reports_both_raw_and_snapped_spacing():
    result = compute_trench_design(TrenchDesignRequest(
        project=_project([_run("A", current_a=120.0), _run("B", current_a=120.0)]),
        settings={"A": TrenchRunSettings(trench_group="TR-1"),
                  "B": TrenchRunSettings(trench_group="TR-1")},
        conditions=TrenchConditions(soil_resistivity_source="astm_d5334_thermal_probe",
                                    include_drawing=False),
    ))
    layout = result["trenches"]["TR-1"]["layout"]
    assert layout["raw_computed_min_spacing_in"] is not None
    assert layout["recommended_snapped_spacing_in"] >= layout["raw_computed_min_spacing_in"]
    assert layout["snap_increment_in"] == 1.5
    assert layout["spacing_governed_by"] in {"thermal", "clearance"}


def test_mixed_insulation_ratings_solve_to_the_lowest():
    request = TrenchDesignRequest(
        project=_project([_run("A", insulation_rating=90), _run("B", insulation_rating=75)]),
        settings={"A": TrenchRunSettings(trench_group="TR-1"),
                  "B": TrenchRunSettings(trench_group="TR-1")},
        conditions=_fast_conditions(),
    )
    trench = compute_trench_design(request)["trenches"]["TR-1"]
    assert trench["target_temp_c"] == 75.0
    assert any("lowest" in w for w in trench["warnings"])


def test_drawing_is_optional():
    assert _single_trench()["drawing_svg"] is None
    assert _single_trench(include_drawing=True)["drawing_svg"].startswith("<svg")


# ---------------------------------------------------------------------------
# API route
# ---------------------------------------------------------------------------
def _api_body(**condition_overrides):
    conditions = {
        "soil_resistivity_source": "astm_d5334_thermal_probe",
        "fixed_layout": {"rows": 1, "spacing_horizontal_in": 12.0},
        "include_drawing": True,
    }
    conditions.update(condition_overrides)
    return {
        "project": {"raceway_runs": [
            {"tag": "AC-1", "raceway_type": "conduit", "circuit_type": "ac",
             "current_a": 253, "conductor_count": 3},
            {"tag": "AC-2", "raceway_type": "conduit", "circuit_type": "ac",
             "current_a": 253, "conductor_count": 3},
        ]},
        "settings": {"AC-1": {"trench_group": "TR-1"}, "AC-2": {"trench_group": "TR-1"}},
        "conditions": conditions,
    }


def test_trench_endpoint_returns_a_serializable_report():
    response = client.post("/trench/thermal-design", json=_api_body())
    assert response.status_code == 200
    body = response.json()
    trench = body["trenches"]["TR-1"]
    assert trench["conduit_count"] == 2
    assert isinstance(trench["max_conductor_temp_c"], float)
    assert trench["drawing_svg"].startswith("<svg")
    assert len(trench["conduits"]) == 2
    assert "all_pass" in body


def test_trench_endpoint_rejects_electrical_resistivity_with_422():
    response = client.post("/trench/thermal-design",
                           json=_api_body(soil_resistivity_source="wenner_electrical"))
    assert response.status_code == 422
    assert "ELECTRICAL" in response.json()["detail"]
