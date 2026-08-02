"""Project data models — the Pydantic equivalent of the HMI draft's client-side
project-state shape (what "Save project" downloads and "Load project" restores).

Every calc module below takes one or more of these as input rather than loose
positional args, so the shape stays in one place as the project grows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SiteConfig(BaseModel):
    project_name: str = "REE ESTL Landfill"
    e911_address: str = ""
    target_ac_capacity_w: float = 5_000_000
    inverter_ac_rating_w: float = 350_000
    dc_ac_ratio_target: float = 1.30

    @property
    def num_inverters(self) -> int:
        import math

        return math.ceil(self.target_ac_capacity_w / self.inverter_ac_rating_w)

    @property
    def dc_capacity_per_inverter_w(self) -> float:
        return self.inverter_ac_rating_w * self.dc_ac_ratio_target

    def modules_per_inverter(self, module_pmax_w: float) -> int:
        return int(self.dc_capacity_per_inverter_w // module_pmax_w)


class ModuleSpec(BaseModel):
    sku: str = "720"
    max_series_fuse_rating_a: float = 35


class InverterSpec(BaseModel):
    model: str = "Chint Power Systems CPS SCH350KTL-DO/US-800"
    nominal_ac_voltage_v: float = 800.0
    phases: int = 3
    max_output_current_a: float = 253
    manufacturer_max_ocpd_a: float = 400
    dc_topology: Literal["direct", "combiner"] = "combiner"
    # Only meaningful when dc_topology == "direct" — number of MPPTs and a
    # representative string count per MPPT, since we don't model a full
    # per-MPPT string breakdown yet.
    mppt_count: int = 15
    strings_per_mppt_direct: int = 2
    max_dc_voltage_v: float = 1500


class CombinerRow(BaseModel):
    inputs: int = 2
    bus_rating_a: float = 200
    module_sku: str = "720"


class ASHRAESiteData(BaseModel):
    station_id: str = "TBD — nearest: St. Louis Downtown-Parks, IL"
    min_design_temp_c: float = -10.0
    max_design_temp_c: float = 35.0


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
    ocpd: OcpdInput = Field(default_factory=OcpdInput)
    etap: EtapAssumptions = Field(default_factory=EtapAssumptions)
    jurisdiction: JurisdictionInput = Field(default_factory=JurisdictionInput)
    iv_curve_conditions: IvCurveConditions = Field(default_factory=IvCurveConditions)
    iv_curve_reading: IvCurveReading = Field(default_factory=IvCurveReading)
    client_info: ClientInfo = Field(default_factory=ClientInfo)
    directory_contacts: dict[str, DirectoryContact] = Field(default_factory=dict)
    aux_panelboard: AuxPanelboardConfig = Field(default_factory=AuxPanelboardConfig)
    mv_recloser: MvRecloserConfig = Field(default_factory=MvRecloserConfig)
    mv_goab: MvGoabConfig = Field(default_factory=MvGoabConfig)
    mv_meter: MvMeterConfig = Field(default_factory=MvMeterConfig)
