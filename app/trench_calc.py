"""Trench thermal design — the seam between this project's conduit/wire
schedule and the numerical solver in app/trench_thermal/.

What this module is for: NEC Table 310.16 ampacities assume a conductor in
free air or in a raceway in air at a reference ambient. A group of conduits
direct-buried in one trench mutually heat each other through the soil, and
neither the ambient-temperature correction (Table 310.15(B)(1)) nor the
conduit-fill adjustment (Table 310.15(C)(1)) models that: fill adjustment is
about conductors crowding each other INSIDE one conduit, this is about
conduits crowding each other OUTSIDE, competing to reject heat into the same
soil. The two are different mechanisms and stack multiplicatively -- they are
reported here as separate rows, never folded together.

Where the inputs come from, and what is deliberately NOT recomputed:

- Current comes from each RacewayRun's own `current_a`, unchanged. The NEC
  derating factors do not scale the current that physically flows; they
  change which conductor gets selected to carry it. Reapplying them to the
  I^2*R heat term would be double-counting.
- The conductor is whatever app.raceway_calc.compute_raceway_run() selects
  for that run -- the same size the Ampacity (13) and Raceway (24) panels
  show -- so a trench calc can never be run against a different conductor
  than the schedule says.
- Conduit trade size likewise comes from that run's own Chapter 9 fill
  calculation, not a separate entry.

Everything the solver needs beyond that is real physical geometry derived
from the tables already in this codebase (conductor circular mils, Chapter 9
Table 4 conduit areas, Table 5 insulated conductor areas) plus the conduit
wall-thickness and insulation thermal-resistivity data below.

Scope for v1: direct-buried conduits only. Concrete-encased duct banks are
explicitly out (a third material zone, and the concrete envelope's thermal
resistivity and geometry are a different input set) -- runs marked as
anything but `raceway_type == "conduit"` are reported as skipped, not
silently included.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from app.conductor_tables import CIRCULAR_MILS, select_conductor
from app.models import ProjectInput, RacewayRun
from app.raceway_calc import (
    METAL_CONDUIT_MATERIALS,
    compute_raceway_run,
    conductor_area_in2,
    size_conduit,
)
from app.trench_thermal import optimizer as trench_optimizer
from app.trench_thermal import render as trench_render
from app.trench_thermal.conduit import Conduit, ampacity_scale_search
from app.trench_thermal.materials import skin_effect_within_validity

INCH_M = 0.0254
CMIL_TO_MM2 = 5.067075e-4

# ---------------------------------------------------------------------------
# Conduit wall thickness, in — nominal manufacturer/standard dimensions
# (PVC: ASTM D1785 IPS Sch 40/80; EMT: ANSI C80.3; IMC: C80.6; RMC: C80.1),
# common trade sizes only, same subset as app/raceway_calc.py's Table 4 areas.
# Inner diameter is DERIVED from those Table 4 areas rather than re-keyed, so
# the fill calc and the thermal calc can't drift apart; only the wall (which
# Table 4 doesn't carry) is tabulated here. Verify against the manufacturer
# cut sheet before issue.
# ---------------------------------------------------------------------------
CONDUIT_WALL_IN: dict[str, dict[str, float]] = {
    "PVC_SCH40": {"1/2": 0.109, "3/4": 0.113, "1": 0.133, "1-1/4": 0.140, "1-1/2": 0.145,
                  "2": 0.154, "2-1/2": 0.203, "3": 0.216, "3-1/2": 0.226, "4": 0.237},
    "PVC_SCH80": {"1/2": 0.147, "3/4": 0.154, "1": 0.179, "1-1/4": 0.191, "1-1/2": 0.200,
                  "2": 0.218, "2-1/2": 0.276, "3": 0.300, "3-1/2": 0.318, "4": 0.337},
    "EMT": {"1/2": 0.042, "3/4": 0.049, "1": 0.057, "1-1/4": 0.065, "1-1/2": 0.065,
            "2": 0.065, "2-1/2": 0.072, "3": 0.072, "3-1/2": 0.083, "4": 0.083},
    "IMC": {"1/2": 0.070, "3/4": 0.075, "1": 0.085, "1-1/4": 0.085, "1-1/2": 0.090,
            "2": 0.095, "2-1/2": 0.140, "3": 0.145, "3-1/2": 0.150, "4": 0.150},
    "RMC": {"1/2": 0.104, "3/4": 0.107, "1": 0.126, "1-1/4": 0.133, "1-1/2": 0.138,
            "2": 0.146, "2-1/2": 0.193, "3": 0.205, "3-1/2": 0.215, "4": 0.225},
}

# Thermal resistivity of insulating materials, K*m/W — IEC 60287-2-1 Table 1.
# THHN/THWN-2 is PVC with a nylon jacket; USE-2/RHW-2 is XLPE.
INSULATION_RHO_KM_PER_W: dict[str, float] = {"THHN_THWN2": 5.0, "USE2_RHW2": 3.5}

# Duct wall thermal resistivity, K*m/W. Rigid PVC ~6.0. Steel conduit is
# ~2000x more conductive than PVC, so its wall contributes essentially
# nothing — the value below makes that explicit rather than special-casing
# it to zero.
DUCT_RHO_PVC_KM_PER_W = 6.0
DUCT_RHO_STEEL_KM_PER_W = 0.022

# Concentric-stranded conductors occupy a larger circle than the same area of
# solid metal, because of the interstices between strands. 1/sqrt(0.76) for a
# typical 0.76 stranding fill factor — reproduces NEC Chapter 9 Table 8's
# stranded overall diameters to within ~1% from 8 AWG up.
STRANDING_DIAMETER_FACTOR = 1.147

# Diameter of the circle circumscribing n equal touching circles, as a
# multiple of one circle's diameter (optimal circle-packing ratios). This is
# the bundle diameter D_e that IEC 60287-2-1's air-space term wants — e.g.
# 2.155 for three cables in trefoil, its own stated value.
_BUNDLE_DIAMETER_RATIO: dict[int, float] = {
    1: 1.000, 2: 2.000, 3: 2.155, 4: 2.414, 5: 2.701, 6: 3.000,
    7: 3.000, 8: 3.304, 9: 3.613, 10: 3.813, 11: 3.923, 12: 4.029,
}


def bundle_diameter_ratio(n: int) -> float:
    if n <= 0:
        return 0.0
    if n in _BUNDLE_DIAMETER_RATIO:
        return _BUNDLE_DIAMETER_RATIO[n]
    # Beyond the tabulated packings, fall back to an area-based estimate at a
    # ~0.70 packing fraction (matches the tabulated values at n=10-12 to ~1%).
    return math.sqrt(n / 0.70)


def conductor_bare_diameter_in(conductor: str) -> float:
    """Overall diameter of the (stranded) metallic conductor, in."""
    cm = CIRCULAR_MILS.get(conductor, 0)
    return (math.sqrt(cm) / 1000.0) * STRANDING_DIAMETER_FACTOR if cm else 0.0


def conductor_insulated_diameter_in(conductor: str, insulation: str) -> float:
    """Overall diameter over the insulation/jacket, in — derived from the
    same Chapter 9 Table 5 areas app/raceway_calc.py fills conduits with."""
    area = conductor_area_in2(conductor, insulation)
    return 2.0 * math.sqrt(area / math.pi) if area else 0.0


def conduit_inner_diameter_in(trade_size: str, material: str) -> float:
    """Derived from the Chapter 9 Table 4 100%-area value already in
    app/raceway_calc.py, so fill and thermal share one source of truth."""
    from app.raceway_calc import CONDUIT_AREA_IN2

    sizes = CONDUIT_AREA_IN2.get(material, CONDUIT_AREA_IN2["PVC_SCH40"])
    area = sizes.get(trade_size)
    return 2.0 * math.sqrt(area / math.pi) if area else 0.0


def conduit_outer_diameter_in(trade_size: str, material: str) -> float:
    wall = CONDUIT_WALL_IN.get(material, CONDUIT_WALL_IN["PVC_SCH40"]).get(trade_size, 0.0)
    return conduit_inner_diameter_in(trade_size, material) + 2.0 * wall


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
SoilResistivitySource = Literal[
    "astm_d5334_thermal_probe",
    "ieee_442_thermal_probe",
    "manufacturer_backfill_spec",
    "assumed_default",
    "wenner_electrical",
]

# Soil THERMAL resistivity (degC*cm/W, ASTM D5334 / IEEE 442 needle probe) and
# soil ELECTRICAL resistivity (ohm*m, Wenner four-point, ASTM G57) are
# different measurements from different field tests, and a geotech report
# often contains both. Their numeric ranges overlap almost completely -- a
# 100 ohm*m Wenner reading typed in here reads as a perfectly plausible
# 100 degC*cm/W thermal value and silently produces a wrong answer that no
# magnitude check can catch. Hence the explicit source declaration below:
# declaring a Wenner/electrical value is rejected outright rather than
# warned about.
_PLAUSIBLE_THERMAL_RHO_CM = (20.0, 300.0)


class TrenchRunSettings(BaseModel):
    """Per-raceway-run settings the run itself can't carry. A RacewayRun is
    "N conductors in one raceway" with no notion of which trench it shares,
    how many physical conduits the run represents, or what metal it is."""

    include: bool = True
    trench_group: str = "TRENCH-1"
    conduit_count: int = 1              # physical conduits this run puts in the trench
    ccc_per_conduit: int | None = None  # None -> the run's own conductor_count
    conductor_material: Literal["CU", "AL"] = "CU"


class TrenchFixedLayout(BaseModel):
    """Check an already-drawn trench detail instead of searching for one.
    Far faster than the optimizer (one solve instead of a few hundred) and
    usually what's wanted once a detail exists."""

    rows: int = 1
    spacing_horizontal_in: float = 7.5
    spacing_vertical_in: float = 7.5


class TrenchConditions(BaseModel):
    """Site thermal conditions for one trench calc run.

    Defaults are deliberately conservative: 100% simultaneous loading on
    every conduit (no load-factor / diversity credit), which is the
    PE-reviewable posture the rest of this app's calc modules take. Add a
    load factor only with a documented basis for it.
    """

    native_soil_rho_cm: float = 90.0
    backfill_rho_cm: float = 60.0
    soil_resistivity_source: SoilResistivitySource = "assumed_default"

    depth_of_cover_in: float = 36.0     # grade to centre of the shallowest conduit row
    backfill_margin_in: float = 4.0     # engineered backfill beyond the outermost conduit
    min_clear_spacing_in: float = 3.0   # minimum clear between conduit walls

    air_temp_c: float | None = None     # None -> project ASHRAE max design temp
    deep_earth_temp_c: float = 20.0
    surface_h_conv_w_m2k: float = 15.0
    conductor_temp_limit_c: float | None = None  # None -> each run's insulation rating
    frequency_hz: float = 60.0

    # Irregular backfill envelope, [[x_in, depth_in], ...] with x measured
    # from the trench centreline and depth below grade. None -> the
    # rectangular envelope derived from the conduit extents plus margin.
    backfill_polygon_in: list[list[float]] | None = None

    fixed_layout: TrenchFixedLayout | None = None
    include_drawing: bool = True


class TrenchDesignRequest(BaseModel):
    project: ProjectInput
    settings: dict[str, TrenchRunSettings] = Field(default_factory=dict)
    conditions: TrenchConditions = Field(default_factory=TrenchConditions)


class TrenchInputError(ValueError):
    """Raised for an input that would silently produce a wrong answer."""


# ---------------------------------------------------------------------------
# Building solver conduits from schedule rows
# ---------------------------------------------------------------------------
def _duct_properties(material: str) -> tuple[float, str]:
    """(duct wall thermal resistivity, air-space constant class)."""
    if material in METAL_CONDUIT_MATERIALS:
        return DUCT_RHO_STEEL_KM_PER_W, "metallic"
    return DUCT_RHO_PVC_KM_PER_W, "nonmetallic"


def conduits_from_run(
    run: RacewayRun,
    settings: TrenchRunSettings,
    ambient_c: float,
    frequency_hz: float = 60.0,
) -> tuple[list[Conduit], dict]:
    """Turn one raceway run into its physical conduits, sized by the same
    app.raceway_calc pass the Raceway (24) panel shows. Returns
    (conduits, sizing) -- `sizing` carries the run's own NEC derate factors
    for the reporting stack, and its selected conductor and trade size."""
    sized = compute_raceway_run(run, ambient_c)
    conductor = sized["selected_conductor"]
    raceway = sized.get("raceway") or {}
    trade_size = raceway.get("selected_trade_size_in")

    # app.raceway_calc sizes copper (the schedule panel's own assumption). An
    # aluminum trench run re-selects from the aluminum column against the SAME
    # required ampacity that panel computed -- so the derate chain is still the
    # schedule's, only the metal changes -- and re-sizes the conduit for it.
    if settings.conductor_material != "CU" and conductor is not None:
        conductor = select_conductor(
            sized["required_ampacity_a"], run.insulation_rating, settings.conductor_material
        )
        if conductor is not None:
            raceway = size_conduit(
                conductor, run.conductor_insulation, run.conductor_count,
                run.conduit_material, run.is_nipple,
            )
            trade_size = raceway.get("selected_trade_size_in")

    sizing = {
        "tag": run.tag,
        "selected_conductor": conductor,
        "conductor_material": settings.conductor_material,
        "schedule_conductor_cu": sized["selected_conductor"],
        "trade_size_in": trade_size,
        "conduit_material": run.conduit_material,
        "temp_correction_factor": sized["temp_correction_factor"],
        "fill_adjustment_factor": sized["fill_adjustment_factor"],
        "required_ampacity_a": sized["required_ampacity_a"],
        "current_a": run.current_a,
        "circuit_type": run.circuit_type,
        "insulation_rating": run.insulation_rating,
        "conductor_insulation": run.conductor_insulation,
    }

    if conductor is None:
        raise TrenchInputError(
            f"Run {run.tag}: no conductor up to 750 kcmil clears its required ampacity "
            f"({sized['required_ampacity_a']} A) -- size the run on the Raceway (24) panel first."
        )
    if trade_size is None:
        raise TrenchInputError(
            f"Run {run.tag}: no standard trade size fits its conductors in "
            f"{run.conduit_material} -- resolve the fill failure on the Raceway (24) panel first."
        )

    n_ccc = settings.ccc_per_conduit or run.conductor_count
    d_conductor_m = conductor_bare_diameter_in(conductor) * INCH_M
    d_insulated_m = conductor_insulated_diameter_in(conductor, run.conductor_insulation) * INCH_M
    d_bundle_m = d_insulated_m * bundle_diameter_ratio(n_ccc)
    duct_id_m = conduit_inner_diameter_in(trade_size, run.conduit_material) * INCH_M
    duct_od_m = conduit_outer_diameter_in(trade_size, run.conduit_material) * INCH_M
    rho_duct, duct_class = _duct_properties(run.conduit_material)

    # The bundle can't be wider than the duct it's in; when Chapter 9 fill
    # says it fits but the circumscribed-circle model says otherwise, the
    # packing is tighter than the idealized circle (real cables aren't
    # perfectly round-packed), so clamp rather than report a negative gap.
    d_bundle_m = min(d_bundle_m, duct_id_m)

    conduits = []
    for n in range(settings.conduit_count):
        suffix = f"-{n + 1}" if settings.conduit_count > 1 else ""
        conduits.append(Conduit(
            id=f"{run.tag}{suffix}",
            I_design=run.current_a,
            n_ccc=n_ccc,
            area_mm2=CIRCULAR_MILS[conductor] * CMIL_TO_MM2,
            material=settings.conductor_material,
            is_ac=(run.circuit_type == "ac"),
            frequency_hz=frequency_hz,
            conductor_diameter_m=d_conductor_m,
            over_insulation_diameter_m=d_insulated_m,
            bundle_diameter_m=d_bundle_m,
            duct_inner_diameter_m=duct_id_m,
            duct_outer_diameter_m=duct_od_m,
            rho_insulation_km_per_w=INSULATION_RHO_KM_PER_W.get(run.conductor_insulation, 3.5),
            rho_duct_km_per_w=rho_duct,
            duct_class=duct_class,
        ))
    return conduits, sizing


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_conditions(conditions: TrenchConditions) -> list[str]:
    """Raises TrenchInputError on an input that would be silently wrong;
    returns warnings for inputs that are merely worth a second look."""
    if conditions.soil_resistivity_source == "wenner_electrical":
        raise TrenchInputError(
            "Soil resistivity source is a Wenner four-point (ASTM G57) ELECTRICAL "
            "resistivity in ohm*m — that is a different measurement from the THERMAL "
            "resistivity (degC*cm/W, ASTM D5334 / IEEE 442 needle probe) this calc needs, "
            "and the two overlap numerically, so no plausibility check can catch the "
            "substitution. Pull the thermal probe result from the geotech report, or run "
            "this against an assumed thermal value declared as such."
        )

    warnings: list[str] = []
    lo, hi = _PLAUSIBLE_THERMAL_RHO_CM
    for label, value in (("native soil", conditions.native_soil_rho_cm),
                         ("backfill", conditions.backfill_rho_cm)):
        if not (lo <= value <= hi):
            warnings.append(
                f"{label.capitalize()} thermal resistivity {value:g} degC*cm/W is outside the "
                f"{lo:g}-{hi:g} range typical of soils — confirm the units and that this is a "
                f"thermal, not electrical, resistivity."
            )
    if conditions.backfill_rho_cm > conditions.native_soil_rho_cm:
        warnings.append(
            "Engineered backfill is specified as a WORSE thermal conductor than the native "
            "soil it replaces — check the two values aren't swapped."
        )
    if conditions.soil_resistivity_source == "assumed_default":
        warnings.append(
            "Soil thermal resistivity is an assumed default, not a measured value. A "
            "PE-reviewable trench calc needs the project's own ASTM D5334 / IEEE 442 thermal "
            "probe result, at the design moisture condition."
        )
    if conditions.backfill_polygon_in and len(conditions.backfill_polygon_in) < 3:
        raise TrenchInputError("backfill_polygon_in needs at least 3 vertices.")
    return warnings


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
def _derating_stack(sizing: dict, trench_factor: float) -> dict:
    """The three derating mechanisms as separate rows, plus their product.

    They are separate physical effects and multiply; the point of showing
    them apart is that a reader can see the trench contribution has NOT been
    folded into the NEC factors, and vice versa.
    """
    temp_factor = sizing["temp_correction_factor"]
    fill_factor = sizing["fill_adjustment_factor"]
    combined = temp_factor * fill_factor * trench_factor
    current = sizing["current_a"]

    rows = [
        {
            "mechanism": "NEC 310.15(B)(1) — ambient temperature correction",
            "factor": temp_factor,
            "applied_by": "Ampacity & Conductors (13) / Raceway (24)",
            "acts_on": "conductor rating vs. ambient at the raceway",
        },
        {
            "mechanism": "NEC 310.15(C)(1) — conduit fill adjustment",
            "factor": fill_factor,
            "applied_by": "Ampacity & Conductors (13) / Raceway (24)",
            "acts_on": "conductors crowding each other inside one conduit",
        },
        {
            "mechanism": "Trench mutual heating (this module, numerical)",
            "factor": round(trench_factor, 4),
            "applied_by": "Trench Thermal Design",
            "acts_on": "conduits competing to reject heat into the same soil",
        },
    ]

    material = sizing["conductor_material"]
    required_with_trench = current / combined if combined > 0 else float("inf")
    conductor_with_trench = (
        select_conductor(required_with_trench, sizing["insulation_rating"], material)
        if math.isfinite(required_with_trench) else None
    )
    return {
        "rows": rows,
        "conductor_material": material,
        "combined_factor": round(combined, 4),
        "nec_only_factor": round(temp_factor * fill_factor, 4),
        "required_ampacity_nec_only_a": sizing["required_ampacity_a"],
        "required_ampacity_with_trench_a": (
            round(required_with_trench, 2) if math.isfinite(required_with_trench) else None
        ),
        "conductor_nec_only": sizing["selected_conductor"],
        "conductor_with_trench": conductor_with_trench,
        # True when the trench, not the NEC derate chain, is what forces a
        # bigger conductor -- the headline a reviewer is looking for.
        "trench_governs": conductor_with_trench != sizing["selected_conductor"],
    }


def _conduit_report(c: Conduit, detail: dict, target_c: float) -> dict:
    return {
        "id": c.id,
        "x_in": round(c.x / INCH_M, 2),
        "depth_in": round(c.y / INCH_M, 2),
        "conduit_od_in": round(c.duct_outer_diameter_m / INCH_M, 3),
        "ccc": c.n_ccc,
        "current_a": round(detail["current_a"], 2),
        "conductor_temp_c": round(detail["conductor_temp_c"], 1),
        "duct_wall_temp_c": round(detail["duct_wall_temp_c"], 1),
        "margin_to_limit_c": round(target_c - detail["conductor_temp_c"], 1),
        "loss_per_conductor_w_per_m": round(detail["loss_per_conductor_w_per_m"], 3),
        "loss_total_w_per_m": round(detail["loss_total_w_per_m"], 3),
        "r_ac_ohm_per_km": round(detail["r_ac_ohm_per_m"] * 1000, 5),
        "skin_effect_y_s": round(detail["skin_effect_y_s"], 5),
        "proximity_effect_y_p": round(detail["proximity_effect_y_p"], 5),
        "ac_dc_ratio": round(1 + detail["skin_effect_y_s"] + detail["proximity_effect_y_p"], 4),
        "r_insulation_km_per_w": round(detail["r_insulation_km_per_w"], 4),
        "r_air_space_km_per_w": round(detail["r_air_space_km_per_w"], 4),
        "r_duct_wall_km_per_w": round(detail["r_duct_wall_km_per_w"], 4),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _solve_group(group_tag, conduit_specs, sizings, conditions, ambient_c):
    warnings: list[str] = []

    for tag, sizing in sizings.items():
        if sizing["conductor_material"] != "CU":
            warnings.append(
                f"Run {tag}: the Raceway (24) schedule sizes copper "
                f"({sizing['schedule_conductor_cu']}); this trench run is set to "
                f"{sizing['conductor_material']} and re-selected {sizing['selected_conductor']} "
                f"against the same required ampacity. Update the schedule if the design is "
                f"actually aluminum."
            )

    target_c = conditions.conductor_temp_limit_c
    if target_c is None:
        # The most restrictive insulation rating in the trench governs the
        # whole group, since one solve produces one shared temperature field.
        target_c = min(float(s["insulation_rating"]) for s in sizings.values())
        ratings = {s["insulation_rating"] for s in sizings.values()}
        if len(ratings) > 1:
            warnings.append(
                f"Trench {group_tag} mixes {sorted(ratings)} degC insulation ratings; the "
                f"layout is solved to the lowest ({target_c:.0f} degC)."
            )

    polygon_m = None
    if conditions.backfill_polygon_in:
        polygon_m = [(vx * INCH_M, vy * INCH_M) for vx, vy in conditions.backfill_polygon_in]

    common = dict(
        native_rho_cm=conditions.native_soil_rho_cm,
        backfill_rho_cm=conditions.backfill_rho_cm,
        top_depth=conditions.depth_of_cover_in * INCH_M,
        backfill_margin=conditions.backfill_margin_in * INCH_M,
        T_air_C=ambient_c,
        T_deep_C=conditions.deep_earth_temp_c,
        h_conv=conditions.surface_h_conv_w_m2k,
        grid_h_fine=max(c.r_duct for c in conduit_specs) / 2.2,
        backfill_polygon=polygon_m,
    )

    if conditions.fixed_layout is not None:
        best = _evaluate_fixed_layout(conduit_specs, conditions.fixed_layout, common)
        mode = "check"
    else:
        best = trench_optimizer.optimize(
            conduit_specs, T_target_C=target_c,
            min_clear_m=conditions.min_clear_spacing_in * INCH_M, **common,
        )
        mode = "optimize"

    if best is None:
        return {
            "trench_group": group_tag,
            "solved": False,
            "passes": False,
            "mode": mode,
            "target_temp_c": target_c,
            "conduit_count": len(conduit_specs),
            "message": (
                "No arrangement of these conduits keeps every conductor at or below "
                f"{target_c:.0f} degC at any spacing searched. The trench, not the conductor's "
                "NEC derating, is the binding constraint — upsize conductors, split the "
                "circuits across separate trenches, or improve the backfill."
            ),
            "warnings": warnings,
        }

    evaluator = best.pop("evaluator")
    grid, solver, placed, envelope = evaluator.build(
        best["assignment"], best["dx"], best["dy"] or trench_optimizer.INCREMENT)
    headroom_scale, _ = ampacity_scale_search(solver, grid, placed, target_c)
    trench_factor = min(1.0, headroom_scale)

    for c in placed:
        r_dc_ish, _, _ = c.resistance_per_m(target_c)
        if c.is_ac and not skin_effect_within_validity(r_dc_ish, c.frequency_hz):
            warnings.append(
                f"{c.id}: conductor is large enough that the IEC 60287-1-1 skin-effect series "
                f"form is outside its stated validity range — use manufacturer AC/DC ratio data."
            )

    max_t = float(best["max_T"])
    passes = max_t <= target_c
    if mode == "check" and not passes:
        warnings.append(
            f"The as-drawn arrangement runs the hottest conductor to {max_t:.1f} degC against a "
            f"{target_c:.0f} degC limit. Re-run without a fixed layout to have the optimizer find "
            f"a spacing that works."
        )

    dx_in = best["dx"] / INCH_M
    dy_in = (best["dy"] / INCH_M) if best["dy"] else None
    # In check mode nothing was minimized -- reporting the as-drawn spacing as
    # a "computed minimum" would be a fabricated result.
    raw_in = (best["raw_min_spacing_m"] / INCH_M) if mode == "optimize" else None
    governing_snapped_in = dy_in if best["spacing_axis"] == "vertical" else dx_in

    drawing = None
    if conditions.include_drawing:
        drawing = trench_render.render_cross_section(
            placed, best["T_cond"], envelope, target_c, best["dx"], best["dy"],
            title=f"Trench {group_tag} — Cross Section",
            backfill_polygon=polygon_m,
        )

    conduit_rows = [_conduit_report(c, best["detail"][c.id], target_c) for c in placed]
    conduit_rows.sort(key=lambda r: (r["depth_in"], r["x_in"]))

    derating = {
        tag: _derating_stack(sizing, trench_factor) for tag, sizing in sizings.items()
    }

    return {
        "trench_group": group_tag,
        "solved": True,
        "passes": passes,
        "mode": mode,
        "conduit_count": len(placed),
        "target_temp_c": target_c,
        "max_conductor_temp_c": round(max_t, 1),
        "margin_to_limit_c": round(target_c - max_t, 1),
        # >1.0 means the trench could carry more than the design current;
        # <1.0 means the trench -- not the conductor's NEC derating -- binds.
        "thermal_headroom_scale": round(headroom_scale, 4),
        "trench_derate_factor": round(trench_factor, 4),
        "layout": {
            "rows": best["rows"],
            "cols": best["cols"],
            "spacing_horizontal_in": round(dx_in, 2),
            "spacing_vertical_in": round(dy_in, 2) if dy_in else None,
            "governing_spacing_axis": best["spacing_axis"],
            # Both numbers, always: the raw solver minimum and the buildable
            # value it snaps up to on the 1.5"-on-centre grid. The snapped
            # number is the conservative one and the one to build to.
            "raw_computed_min_spacing_in": round(raw_in, 3) if raw_in else None,
            "recommended_snapped_spacing_in": round(governing_snapped_in, 2),
            "snap_increment_in": 1.5,
            "spacing_governed_by": best["spacing_governed_by"],
            "min_clear_spacing_in": round(best.get("min_clear_spacing_m", 0) / INCH_M, 2) or None,
            "depth_of_cover_in": conditions.depth_of_cover_in,
            "trench_width_in": round((envelope["x_max"] - envelope["x_min"]) / INCH_M, 1),
            "trench_depth_in": round(envelope["y_max"] / INCH_M, 1),
            "backfill_footprint_ft2": round(best["cost"] * 10.7639, 2),
        },
        "conduits": conduit_rows,
        "derating_stack": derating,
        "shapes_evaluated": best.get("shapes", []),
        "solver_runs": best.get("solve_count"),
        "drawing_svg": drawing,
        "warnings": warnings,
    }


def _evaluate_fixed_layout(specs, fixed: TrenchFixedLayout, common) -> dict:
    """One solve of an already-drawn arrangement, shaped like an optimizer
    result so everything downstream is identical."""
    from app.trench_thermal.layout import assign_positions

    n = len(specs)
    rows = max(1, min(fixed.rows, n))
    cols = -(-n // rows)
    assignment = assign_positions(specs, rows, cols)
    dx = fixed.spacing_horizontal_in * INCH_M
    dy = fixed.spacing_vertical_in * INCH_M

    evaluator = trench_optimizer.TrenchEvaluator(specs, **common)
    max_T, T_cond, envelope, conduits, detail = evaluator.evaluate(assignment, (rows, cols), dx, dy)

    return {
        "rows": rows, "cols": cols, "dx": dx, "dy": dy if rows > 1 else None,
        "max_T": max_T, "T_cond": T_cond, "env": envelope, "conduits": conduits,
        "detail": detail, "assignment": assignment, "evaluator": evaluator,
        "cost": (envelope["x_max"] - envelope["x_min"]) * (envelope["y_max"] - envelope["y_min"]),
        "raw_min_spacing_m": dy if rows > 1 else dx,
        "spacing_axis": "vertical" if rows > 1 else "horizontal",
        "spacing_governed_by": "as-drawn",
        "shapes": [], "solve_count": evaluator.solve_count,
    }


def compute_trench_design(request: TrenchDesignRequest) -> dict:
    """Group the project's raceway runs into trenches and solve each one."""
    conditions = request.conditions
    warnings = validate_conditions(conditions)
    ambient_c = conditions.air_temp_c
    if ambient_c is None:
        ambient_c = request.project.ashrae.max_design_temp_c

    groups: dict[str, list[Conduit]] = {}
    group_sizings: dict[str, dict] = {}
    skipped: list[dict] = []

    for run in request.project.raceway_runs:
        settings = request.settings.get(run.tag, TrenchRunSettings())
        if not settings.include:
            skipped.append({"tag": run.tag, "reason": "excluded by settings"})
            continue
        if run.raceway_type != "conduit":
            skipped.append({
                "tag": run.tag,
                "reason": f"raceway_type is {run.raceway_type!r} — v1 covers direct-buried conduit only",
            })
            continue
        if settings.conduit_count < 1:
            skipped.append({"tag": run.tag, "reason": "conduit_count is 0"})
            continue

        conduits, sizing = conduits_from_run(run, settings, ambient_c, conditions.frequency_hz)
        groups.setdefault(settings.trench_group, []).extend(conduits)
        group_sizings.setdefault(settings.trench_group, {})[run.tag] = sizing

    if not groups:
        return {
            "trenches": {},
            "trench_count": 0,
            "skipped_runs": skipped,
            "ambient_air_temp_c": ambient_c,
            "warnings": warnings + ["No raceway run is assigned to a trench."],
        }

    trenches = {
        tag: _solve_group(tag, specs, group_sizings[tag], conditions, ambient_c)
        for tag, specs in groups.items()
    }

    return {
        "trenches": trenches,
        "trench_count": len(trenches),
        "all_pass": all(t["passes"] for t in trenches.values()),
        "skipped_runs": skipped,
        "ambient_air_temp_c": ambient_c,
        "soil_resistivity_source": conditions.soil_resistivity_source,
        "load_factor_note": (
            "100% simultaneous loading on every conduit — no load-factor or diversity credit. "
            "Conservative by design; relaxing it needs a documented loading basis."
        ),
        "warnings": warnings,
    }
