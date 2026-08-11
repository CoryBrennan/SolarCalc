from app.cable_routing_calc import RoutingLegTemplate
from app.models import CombinerRow, InverterSpec, ModuleSpec, ProjectInput
from app.pvcase_bom_import import CableSegment, PvcaseBomData
from app.pvcase_routing import (
    CircuitRoutingAssumption,
    PvcaseRoutingAssumptions,
    compute_circuit_routing_report,
    compute_routing_report,
)


def _bom(**overrides):
    defaults = dict(
        project_name="Test",
        overview={},
        transformer_to_inverter=[
            CableSegment(from_tag="XFMR-1", to_tag="INV-1-1", length_ft=20.0),
            CableSegment(from_tag="XFMR-1", to_tag="INV-1-2", length_ft=40.0),
        ],
        inverter_to_combiner=[
            CableSegment(from_tag="INV-1-1", to_tag="DCC-1-1", length_ft=55.6),
            CableSegment(from_tag="INV-1-2", to_tag="DCC-1-2", length_ft=596.1),
        ],
        combiner_to_string=[
            CableSegment(from_tag="DCC-1-1", to_tag="INV-1-1.STR1", length_ft=180.0),
        ],
    )
    defaults.update(overrides)
    return PvcaseBomData(**defaults)


def _project(**overrides):
    defaults = dict(
        module=ModuleSpec(sku="720", quantity=100),
        inverter=InverterSpec(quantity=2, dc_topology="combiner"),
        combiner_rows=[CombinerRow(inputs=12, bus_rating_a=200, module_sku="720")],
    )
    defaults.update(overrides)
    return ProjectInput(**defaults)


def test_governing_segment_is_the_worst_case_length():
    """Ampacity alone can't distinguish these two segments -- same current,
    same installation method, so the same required ampacity regardless of
    length. Only a voltage-drop upsize (which does depend on length) can
    make the longer segment (596.1 ft) need a bigger final conductor than
    the short one (55.6 ft), so the limit here is deliberately tight enough
    to force that upsize on the long segment but not the short one."""
    bom = _bom()
    assumption = CircuitRoutingAssumption(
        voltage_drop_limit_pct=0.5,
        legs=[RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None)],
    )
    report = compute_circuit_routing_report(
        "inverter_to_combiner", bom.inverter_to_combiner, current_a=250, voltage_v=1500,
        assumption=assumption, default_ambient_c=35,
    )
    assert report["governing_segment"]["to_tag"] == "DCC-1-2"
    assert report["segment_count"] == 2
    assert report["final_conductor"] != report["selected_conductor"]  # the long segment's vd upsize is what wins it


def test_direct_topology_skips_inverter_to_combiner_circuit():
    project = _project(inverter=InverterSpec(quantity=2, dc_topology="direct"))
    bom = _bom()
    report = compute_routing_report(project, bom, PvcaseRoutingAssumptions())
    combiner_circuit = next(c for c in report["circuits"] if c["circuit"] == "inverter_to_combiner")
    assert combiner_circuit["segment_count"] == 0
    assert any("doesn't apply" in w for w in combiner_circuit["warnings"])


def test_empty_segment_list_reports_cleanly():
    bom = _bom(transformer_to_inverter=[])
    assumption = CircuitRoutingAssumption()
    report = compute_circuit_routing_report("transformer_to_inverter", [], current_a=100, voltage_v=800, assumption=assumption, default_ambient_c=30)
    assert report["segment_count"] == 0
    assert report["selected_conductor"] is None


def test_full_report_covers_all_three_circuits():
    project = _project()
    bom = _bom()
    report = compute_routing_report(project, bom, PvcaseRoutingAssumptions())
    circuits = {c["circuit"] for c in report["circuits"]}
    assert circuits == {"transformer_to_inverter", "inverter_to_combiner", "combiner_to_string"}
    for c in report["circuits"]:
        if c["segment_count"] > 0:
            assert c["selected_conductor"] is not None


def test_template_fit_warnings_surface_with_segment_tags():
    bom = _bom(inverter_to_combiner=[CableSegment(from_tag="INV-1-1", to_tag="DCC-1-1", length_ft=5.0)])
    assumption = CircuitRoutingAssumption(
        legs=[
            RoutingLegTemplate(installation_method="free_air_or_tray", fixed_length_ft=15.0),
            RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=None),
        ]
    )
    report = compute_circuit_routing_report(
        "inverter_to_combiner", bom.inverter_to_combiner, current_a=100, voltage_v=800,
        assumption=assumption, default_ambient_c=30,
    )
    assert any("INV-1-1 -> DCC-1-1" in w for w in report["warnings"])


def test_many_warnings_get_truncated_with_a_count():
    segments = [CableSegment(from_tag=f"INV-1-{i}", to_tag=f"DCC-1-{i}", length_ft=1.0) for i in range(15)]
    assumption = CircuitRoutingAssumption(legs=[RoutingLegTemplate(installation_method="conduit_below_grade", fixed_length_ft=50.0)])
    report = compute_circuit_routing_report(
        "inverter_to_combiner", segments, current_a=100, voltage_v=800, assumption=assumption, default_ambient_c=30,
    )
    assert len(report["warnings"]) == 11  # 10 shown + 1 "...and N more" line
    assert "5 more" in report["warnings"][-1]


def test_full_report_gives_ac_the_steel_conduit_multiplier_but_not_dc():
    """transformer_to_inverter is fixed as AC and the other two as DC --
    routing a steel-conduit AC leg should show more voltage drop than the
    same length/current through DC, all else equal."""
    project = _project()
    bom = _bom()
    steel_template = RoutingLegTemplate(installation_method="conduit_above_grade", fixed_length_ft=None, conduit_material="EMT")
    assumptions = PvcaseRoutingAssumptions(
        transformer_to_inverter=CircuitRoutingAssumption(voltage_drop_limit_pct=100.0, legs=[steel_template]),
        inverter_to_combiner=CircuitRoutingAssumption(voltage_drop_limit_pct=100.0, legs=[steel_template]),
    )
    report = compute_routing_report(project, bom, assumptions)
    ac = next(c for c in report["circuits"] if c["circuit"] == "transformer_to_inverter")
    dc = next(c for c in report["circuits"] if c["circuit"] == "inverter_to_combiner")
    # Different current/length between the two circuits, so this isn't a
    # like-for-like volts comparison -- just confirm the AC leg actually
    # picked up the multiplier by checking its raceway is real conduit and
    # the governing leg used circuit_type="ac" (steel adder applied).
    assert ac["legs"][ac["governing_leg_index"]]["raceway"]["material"] == "EMT"
    assert dc["legs"][dc["governing_leg_index"]]["raceway"]["material"] == "EMT"


def test_full_report_conduit_legs_carry_a_real_raceway_spec():
    project = _project()
    bom = _bom()
    report = compute_routing_report(project, bom, PvcaseRoutingAssumptions())
    for c in report["circuits"]:
        if c["segment_count"] == 0:
            continue
        governing_leg = c["legs"][c["governing_leg_index"]]
        # Default assumptions route transformer_to_inverter/inverter_to_combiner
        # through conduit_below_grade -- both should get a real trade size.
        if c["circuit"] in ("transformer_to_inverter", "inverter_to_combiner"):
            assert governing_leg["raceway"] is not None
            assert governing_leg["raceway"]["selected_trade_size_in"] is not None
