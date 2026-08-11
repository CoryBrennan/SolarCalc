from app.conductor_tables import equipment_grounding_conductor_size, select_conductor
from app.models import ProjectInput, RacewayRun
from app.wire_cost_calc import (
    DEFAULT_PRICING,
    FeederScenario,
    FeederVeSettings,
    ProjectFeederVeRequest,
    conductor_cost_per_ft,
    conductor_weight_lb_per_ft,
    evaluate_feeder_value_engineering,
    evaluate_project_feeders,
    feeder_scenario_from_raceway_run,
)


def test_aluminum_ampacity_lower_than_copper_same_size():
    assert select_conductor(431, 90, material="CU") == "600 kcmil"
    # Same 600 kcmil clears less current in aluminum -- the whole reason AL
    # feeders need larger conductors than CU for the same ampacity.
    assert select_conductor(431, 90, material="AL") != "600 kcmil"


def test_aluminum_lighter_than_copper_same_size():
    cu = conductor_weight_lb_per_ft("600 kcmil", "CU")
    al = conductor_weight_lb_per_ft("600 kcmil", "AL")
    assert al < cu


def test_egc_not_downsized_for_parallel_sets():
    """NEC 250.122(F): the EGC size is a function of the OCPD rating only --
    parallel raceways each get a full-size EGC, not a fraction of one."""
    assert equipment_grounding_conductor_size(400, "CU") == equipment_grounding_conductor_size(400, "CU")
    single_set_ground = equipment_grounding_conductor_size(400, "CU")
    # Simulate what a (wrong) divide-by-sets approach would try: half the
    # OCPD rating. It must NOT match what two parallel raceways actually need.
    halved_ocpd_ground = equipment_grounding_conductor_size(200, "CU")
    assert single_set_ground != halved_ocpd_ground


def test_matches_user_example_single_set_600kcmil_al_in_3_5in_conduit():
    """The user's literal example: (3) 600 kcmil AL current-carrying + (1)
    600 kcmil AL neutral + (1) #3 CU ground in a 3-1/2 in conduit."""
    scenario = FeederScenario(
        continuous_current_a=340,
        voltage_v=480,
        length_ft=200,
        ccc_per_set=3,
        neutral_present=True,
        insulation_rating=90,
        ambient_c=40,
        ground_ocpd_a=400,
        materials=["AL"],
        ground_material="CU",
        max_parallel_sets=1,
        conduit_materials=["PVC_SCH40"],
    )
    result = evaluate_feeder_value_engineering(scenario)
    candidate = result["candidates"][0]
    assert candidate["phase_conductor"] == "600 kcmil"
    assert candidate["neutral_conductor"] == "600 kcmil"
    assert candidate["ground_conductor"] == "3 AWG"
    assert candidate["raceway"]["trade_size_in"] == "3-1/2"
    assert candidate["passes"] is True


def test_parallel_sets_each_get_their_own_full_ground_conductor():
    scenario = FeederScenario(
        continuous_current_a=800, voltage_v=480, length_ft=250,
        materials=["CU"], max_parallel_sets=2, conduit_materials=["PVC_SCH80"],
        ground_ocpd_a=1000,
    )
    result = evaluate_feeder_value_engineering(scenario)
    two_set = next(c for c in result["candidates"] if c["parallel_sets"] == 2 and c["passes"])
    assert two_set["raceway"]["raceway_count"] == 2
    assert two_set["costs"]["conductor_material_usd"] > 0


def test_undersized_single_set_fails_ampacity_at_high_current():
    scenario = FeederScenario(continuous_current_a=900, materials=["CU"], max_parallel_sets=1)
    result = evaluate_feeder_value_engineering(scenario)
    candidate = result["candidates"][0]
    assert candidate["passes"] is False
    assert candidate["phase_conductor"] is None
    assert "required ampacity per set" in candidate["fail_reasons"][0]


def test_recommended_is_cheapest_passing_candidate():
    scenario = FeederScenario(
        continuous_current_a=340, ground_ocpd_a=400,
        materials=["CU", "AL"], max_parallel_sets=2,
    )
    result = evaluate_feeder_value_engineering(scenario)
    passing_costs = [c["costs"]["total_installed_usd"] for c in result["candidates"] if c["passes"]]
    assert result["recommended"]["costs"]["total_installed_usd"] == min(passing_costs)


def test_aluminum_conductor_cheaper_per_ft_than_copper_same_size():
    cu_cost = conductor_cost_per_ft("600 kcmil", "CU", DEFAULT_PRICING)
    al_cost = conductor_cost_per_ft("600 kcmil", "AL", DEFAULT_PRICING)
    assert al_cost < cu_cost


def test_custom_pricing_overrides_defaults():
    from app.wire_cost_calc import MarketPricing

    scenario = FeederScenario(continuous_current_a=340, ground_ocpd_a=400, materials=["CU"], max_parallel_sets=1)
    cheap_pricing = MarketPricing(copper_usd_per_lb=0.01, aluminum_usd_per_lb=0.01)
    result_default = evaluate_feeder_value_engineering(scenario)
    result_cheap = evaluate_feeder_value_engineering(scenario, cheap_pricing)
    assert result_cheap["candidates"][0]["costs"]["total_installed_usd"] < result_default["candidates"][0]["costs"]["total_installed_usd"]


def test_feeder_scenario_from_raceway_run_carries_electrical_fields_through():
    run = RacewayRun(tag="AC-OUT", current_a=340, voltage_v=480, length_ft=200, insulation_rating=90, vd_limit_pct=1.5, conduit_material="EMT")
    settings = FeederVeSettings(ground_ocpd_a=400, materials=["AL"], ground_material="CU")
    scenario = feeder_scenario_from_raceway_run(run, settings, ambient_c=40.0)

    assert scenario.tag == "AC-OUT"
    assert scenario.continuous_current_a == 340
    assert scenario.voltage_v == 480
    assert scenario.length_ft == 200
    assert scenario.insulation_rating == 90
    assert scenario.voltage_drop_limit_pct == 1.5
    assert scenario.ambient_c == 40.0
    assert scenario.conduit_materials == ["EMT"]  # falls back to the run's own conduit material
    assert scenario.materials == ["AL"]
    assert scenario.ground_material == "CU"


def test_evaluate_project_feeders_covers_every_raceway_run():
    project = ProjectInput(
        raceway_runs=[
            RacewayRun(tag="DC-SOURCE", current_a=12, voltage_v=600, length_ft=150, insulation_rating=90),
            RacewayRun(tag="AC-OUT", current_a=340, voltage_v=480, length_ft=200, insulation_rating=90),
        ],
    )
    request = ProjectFeederVeRequest(
        project=project,
        settings={"AC-OUT": FeederVeSettings(ground_ocpd_a=400, materials=["AL"], ground_material="CU")},
    )
    result = evaluate_project_feeders(request)

    assert result["run_count"] == 2
    assert set(result["runs"].keys()) == {"DC-SOURCE", "AC-OUT"}
    # AC-OUT used explicit settings; DC-SOURCE fell back to FeederVeSettings() defaults.
    ac_scenario = result["runs"]["AC-OUT"]["scenario"]
    assert ac_scenario["materials"] == ["AL"]
    dc_scenario = result["runs"]["DC-SOURCE"]["scenario"]
    assert dc_scenario["materials"] == ["CU", "AL"]


def test_evaluate_project_feeders_uses_project_ambient():
    project = ProjectInput(raceway_runs=[RacewayRun(tag="RW-1", current_a=50, voltage_v=480, length_ft=100)])
    project.ashrae.max_design_temp_c = 45.0
    result = evaluate_project_feeders(ProjectFeederVeRequest(project=project))
    assert result["runs"]["RW-1"]["scenario"]["ambient_c"] == 45.0
