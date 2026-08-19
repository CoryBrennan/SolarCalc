"""Project data models — the Pydantic equivalent of the HMI draft's client-side
project-state shape (what "Save project" downloads and "Load project" restores).

Every calc module below takes one or more of these as input rather than loose
positional args, so the shape stays in one place as the project grows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SiteAddress(BaseModel):
    street: str = ""
    city: str = ""
    state: str = "IL"
    zip: str = ""
    # Populated by POST /site/geocode (app/geocode_lookup.py) — not resolved
    # automatically on every save, since it calls out to external services.
    # None until an engineer explicitly runs the lookup and it succeeds; a
    # PVsyst .SIT export (or anything else needing coordinates) should treat
    # None as "not yet resolved," not "at 0,0."
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float | None = None
    timezone: str | None = None

    def full_address(self) -> str:
        city_state = " ".join(p for p in [self.state, self.zip] if p)
        city_state_zip = ", ".join(p for p in [self.city, city_state] if p)
        return ", ".join(p for p in [self.street, city_state_zip] if p)


class SiteConfig(BaseModel):
    project_name: str = "REE ESTL Landfill"
    utility_name: str = ""
    address: SiteAddress = Field(default_factory=SiteAddress)
    # Design goals, not as-built — they guide equipment selection on
    # ModuleSpec/InverterSpec but never override those quantities. See
    # ModuleSpec.quantity / InverterSpec.quantity for the actual counts that
    # actual_dc_capacity_w / actual_ac_capacity_w (computed in project_calc.py)
    # are derived from.
    target_ac_capacity_w: float = 5_000_000
    target_dc_capacity_w: float = 6_500_000

    @property
    def calculated_dc_ac_ratio(self) -> float:
        if self.target_ac_capacity_w <= 0:
            return 0.0
        return self.target_dc_capacity_w / self.target_ac_capacity_w


class ModuleSpec(BaseModel):
    sku: str = "720"
    manufacturer: str = "ReneSola"
    max_series_fuse_rating_a: float = 35
    quantity: int = 9465
    max_system_voltage_v: float = 1500
    first_year_degradation_pct: float = 1.0
    annual_degradation_pct: float = 0.4


class InverterSpec(BaseModel):
    model: str = "Chint Power Systems CPS SCH350KTL-DO/US-800"
    # manufacturer/catalog_number are the split form of `model`, needed because
    # the AC one-line block prints them on separate nameplate lines. `model` is
    # kept as-is for the HMI's single-line display.
    manufacturer: str = "Chint Power Systems"
    catalog_number: str = "CPS SCH350KTL-DO/US-800"
    ac_rating_w: float = 350_000
    quantity: int = 15
    nominal_ac_voltage_v: float = 800.0
    phases: int = 3
    # AC output conductor configuration, printed on the AC block's nameplate as
    # e.g. "3Ø 3W + PE". 3-wire = no distributed neutral (the usual case for a
    # utility-scale inverter feeding a delta-primary transformer); 4-wire adds
    # one. PE is the equipment grounding conductor, separate from that count.
    ac_wires: int = 3
    ac_equipment_ground: bool = True
    # Letter in the AC block's detail-callout triangle, pointing at the
    # construction detail the AC termination is built to. Engineer-set per
    # drawing set; empty renders an empty triangle rather than a placeholder.
    ac_detail_ref: str = ""
    max_output_current_a: float = 253
    manufacturer_max_ocpd_a: float = 400
    dc_topology: Literal["direct", "combiner"] = "combiner"
    # Only meaningful when dc_topology == "direct" — number of MPPTs and a
    # representative string count per MPPT, since we don't model a full
    # per-MPPT string breakdown yet.
    mppt_count: int = 15
    strings_per_mppt_direct: int = 2
    max_dc_voltage_v: float = 1500  # doubles as MPPT V max for string-length sizing
    mppt_v_min: float = 500.0


class CombinerRow(BaseModel):
    inputs: int = 2
    bus_rating_a: float = 200
    module_sku: str = "720"


class ASHRAESiteData(BaseModel):
    station_id: str = "TBD — nearest: St. Louis Downtown-Parks, IL"
    min_design_temp_c: float = -10.0
    max_design_temp_c: float = 35.0
    avg_high_temp_c: float = 28.0


class ClientVoltageDropLimits(BaseModel):
    string_to_combiner_pct: float = 2.0
    combiner_to_inverter_pct: float = 1.0
    inverter_to_switchboard_pct: float = 1.0
    total_dc_pct: float = 2.0
    total_ac_pct: float = 1.5


class TransformerConfig(BaseModel):
    kva: float = 500
    primary_v: float = 800
    secondary_v: float = 34500
    primary_winding: Literal["delta", "wye", "grounded_wye"] = "delta"
    secondary_winding: Literal["delta", "wye", "grounded_wye"] = "grounded_wye"


class SwitchboardConfig(BaseModel):
    busbar_rating_a: float = 1200
    main_rating_a: float = 800


class AmpacityInput(BaseModel):
    circuit_type: Literal["dc_source", "dc_output", "ac_output"] = "dc_source"
    parallel_strings: int = 14
    insulation_rating: Literal[75, 90] = 90
    conductor_count: int = 6


class RacewayRun(BaseModel):
    """One conduit, cable-tray, or messenger-cable run — a single set of
    conductors sharing one raceway. conductor_count drives both the
    ampacity-derate chain (NEC 310.15(C)(1)) and the raceway fill % here, so
    a run's ampacity and its physical fill can't be sized against different
    conductor counts."""

    tag: str = "RW-1"
    raceway_type: Literal["conduit", "cable_tray", "messenger"] = "conduit"
    circuit_type: Literal["dc", "ac"] = "dc"
    current_a: float = 30.0
    conductor_count: int = 2
    insulation_rating: Literal[75, 90] = 90
    conductor_insulation: Literal["THHN_THWN2", "USE2_RHW2"] = "USE2_RHW2"
    length_ft: float = 100.0
    voltage_v: float = 600.0
    vd_limit_pct: float = 2.0

    # conduit-specific
    conduit_material: Literal["EMT", "IMC", "RMC", "PVC_SCH40", "PVC_SCH80"] = "PVC_SCH40"
    is_nipple: bool = False

    # cable tray-specific
    tray_type: Literal["ladder", "ventilated_trough", "solid_bottom"] = "ladder"
    tray_width_in: float = 12.0

    # messenger-specific
    span_ft: float = 100.0
    ice_thickness_in: float = 0.0
    sag_ratio: float = 0.03
    safety_factor: float = 2.0
    wind_load_plf: float = 0.0


class OcpdInput(BaseModel):
    circuit: Literal["pv_source", "dc_combiner_output", "inverter_output"] = "inverter_output"
    combiner_index: int = 0
    continuous_current_override_a: float | None = None


class EtapAssumptions(BaseModel):
    conductor: str = "4/0 AWG"
    length_ft: float = 200
    ocpd_rating_a: float = 400
    ocpd_type: Literal["breaker", "fuse"] = "breaker"


class JurisdictionInput(BaseModel):
    state: str = "IL"
    county: str = ""
    ahj_override: str = ""


class IvCurveConditions(BaseModel):
    irradiance_w_m2: float = 850
    cell_temp_c: float = 42
    modules_per_string: int = 28
    tolerance_pct: float = 5.0


class IvCurveReading(BaseModel):
    string_id: str = "STR-01"
    measured_voc: float = 0
    measured_isc: float = 0
    measured_vmp: float = 0
    measured_imp: float = 0


class ContactEntry(BaseModel):
    name: str = ""
    role: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""


class ClientInfo(BaseModel):
    business_name: str = ""
    business_address: str = ""
    website: str = ""
    logo_data_uri: str | None = None
    contacts: list[ContactEntry] = Field(default_factory=list)


DIRECTORY_CATEGORY_KEYS: list[str] = [
    "azimuth", "electrical", "civil", "surveyor", "geotech", "racking",
    "modules", "inverters", "combiners", "switchboard", "transformer",
    "weather", "scada", "mv",
]


class DirectoryContact(BaseModel):
    company: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    certifications: str = ""


class AuxLoadCircuit(BaseModel):
    position: int
    circuit_tag: str
    breaker_rating_a: float = 20
    description: str = ""


class AuxPanelboardConfig(BaseModel):
    tag: str = "AUX-1"
    main_breaker_rating_a: float = 100
    voltage: str = "120/240V"
    phase: Literal["1PH", "3PH"] = "1PH"
    circuits: list[AuxLoadCircuit] = Field(
        default_factory=lambda: [
            AuxLoadCircuit(position=1, circuit_tag="CKT-1", breaker_rating_a=20, description="Lighting"),
            AuxLoadCircuit(position=2, circuit_tag="CKT-2", breaker_rating_a=20, description="Receptacles"),
        ]
    )


class MvRecloserConfig(BaseModel):
    tag: str = "MVR-1"
    voltage_class_kv: float = 38.0
    interrupting_rating_ka: float = 12.5
    control_type: str = "electronic"


class MvGoabConfig(BaseModel):
    tag: str = "GOAB-1"
    voltage_class_kv: float = 38.0
    load_break_rating_a: float = 600


class MvMeterConfig(BaseModel):
    tag: str = "MTR-1"
    metering_class: str = "revenue"
    ct_ratio: str = "400:5"
    pt_ratio: str = "200:1"


# Max instances a single terminal group can expand to. Keeps a "1-or-more"
# group (ground, comms, ...) from growing one side of the generated CAD
# block arbitrarily long relative to the others — see
# custom_device_block.build_custom_device_config.
MAX_TERMINALS_PER_GROUP = 12

TerminalType = Literal[
    "ac_phase", "neutral", "ground", "comms",
    "dc_positive", "dc_negative", "dc_generic", "generic",
]

ConnectableCategory = Literal[
    "breaker", "ground_bar", "neutral_bar", "comms", "other_device", "generic",
]


class TerminalGroupSpec(BaseModel):
    """One row in a device template: a named group of like terminals
    (e.g. "AC Input" = 3 ac_phase terminals, one per L1/L2/L3)."""

    id: str
    label: str
    terminal_type: TerminalType
    count: int = 1
    count_mode: Literal["fixed", "one_or_more"] = "fixed"
    optional: bool = False
    # Only meaningful for terminal_type == "ac_phase". The full candidate
    # set an instance can choose `count` of — e.g. a 3-phase group offers
    # all three and takes all three; a split-phase group offers all three
    # and an instance picks which 2.
    phase_labels: list[str] | None = None
    # Only meaningful for terminal_type == "comms" — selectable per terminal
    # on the instance (e.g. "RS485" vs "Ethernet").
    protocol_options: list[str] | None = None
    # Which connectable-target categories this group's terminals may point
    # at (see app/connectable_targets.py). Purely a picker filter/hint —
    # not enforced server-side beyond what the picker offers.
    connects_to_types: list[ConnectableCategory] = Field(default_factory=list)


class DeviceTemplate(BaseModel):
    """A reusable SLD device shape — a name plus its terminal groups.
    Stored independently of any one project (app/device_templates.py),
    since the same template (e.g. "Inverter w/ DC Combiner") gets
    instantiated many times across many projects."""

    id: str = ""
    name: str
    terminal_groups: list[TerminalGroupSpec] = Field(default_factory=list)


class CustomDeviceTerminalConnection(BaseModel):
    """One terminal's connection choice on a device instance. `index` is
    0-based within its group — the 2nd ground terminal on a device with 3
    grounds is (group_id="ground", index=1)."""

    group_id: str
    index: int = 0
    connects_to: str | None = None
    # Only set when the group's phase_labels offers more candidates than
    # its count (e.g. split-phase picking 2 of 3) — otherwise the group's
    # full phase_labels list is used in order.
    phase_label_override: str | None = None
    # Only set for comms terminals — must be one of the group's
    # protocol_options when present.
    protocol: str | None = None


class CustomDeviceInstance(BaseModel):
    """One physical device on the project's SLD, built from a
    DeviceTemplate. tag is the drawing tag (e.g. "INV-3", "LOAD-7") and
    must not collide with any other tag already in use on the project —
    enforced in app/device_template_routes.py, not here."""

    tag: str
    template_id: str
    # Actual count for each "one_or_more" group, keyed by group_id.
    # Groups not present here use the template's minimum count.
    group_counts: dict[str, int] = Field(default_factory=dict)
    connections: list[CustomDeviceTerminalConnection] = Field(default_factory=list)
    # Free nameplate key/value pairs, same convention as every other
    # block's Attributes dict (e.g. InverterAcConfig.Attributes).
    attributes: dict[str, str] = Field(default_factory=dict)


class ProjectInput(BaseModel):
    """The full payload the /calculate endpoint accepts — one project's worth
    of state, matching what the HMI's "Save project" button already produces."""

    site: SiteConfig = Field(default_factory=SiteConfig)
    module: ModuleSpec = Field(default_factory=ModuleSpec)
    inverter: InverterSpec = Field(default_factory=InverterSpec)
    combiner_rows: list[CombinerRow] = Field(
        default_factory=lambda: [
            CombinerRow(inputs=2, bus_rating_a=200, module_sku="720"),
            CombinerRow(inputs=3, bus_rating_a=200, module_sku="720"),
        ]
    )
    ashrae: ASHRAESiteData = Field(default_factory=ASHRAESiteData)
    voltage_drop_limits: ClientVoltageDropLimits = Field(default_factory=ClientVoltageDropLimits)
    transformer: TransformerConfig = Field(default_factory=TransformerConfig)
    switchboard: SwitchboardConfig = Field(default_factory=SwitchboardConfig)
    ampacity: AmpacityInput = Field(default_factory=AmpacityInput)
    raceway_runs: list[RacewayRun] = Field(default_factory=lambda: [RacewayRun()])
    ocpd: OcpdInput = Field(default_factory=OcpdInput)
    etap: EtapAssumptions = Field(default_factory=EtapAssumptions)
    jurisdiction: JurisdictionInput = Field(default_factory=JurisdictionInput)
    iv_curve_conditions: IvCurveConditions = Field(default_factory=IvCurveConditions)
    iv_curve_reading: IvCurveReading = Field(default_factory=IvCurveReading)
    client_info: ClientInfo = Field(default_factory=ClientInfo)
    directory_contacts: dict[str, list[DirectoryContact]] = Field(default_factory=dict)
    aux_panelboard: AuxPanelboardConfig = Field(default_factory=AuxPanelboardConfig)
    mv_recloser: MvRecloserConfig = Field(default_factory=MvRecloserConfig)
    mv_goab: MvGoabConfig = Field(default_factory=MvGoabConfig)
    mv_meter: MvMeterConfig = Field(default_factory=MvMeterConfig)
    custom_devices: list[CustomDeviceInstance] = Field(default_factory=list)
