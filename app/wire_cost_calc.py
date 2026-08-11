"""Value-engineering cost comparison for a feeder/branch conductor run —
copper vs. aluminum, and a single large conductor set vs. multiple smaller
parallel sets, ranked by total installed cost ($/ft of route length) among
only the candidates that actually clear NEC ampacity, voltage-drop, and
raceway-fill checks.

This does NOT introduce a second sizing algorithm — every candidate reuses
the same primitives the rest of the app already sizes conductors with
(app.ampacity_calc's temp/fill derate chain, app.conductor_tables.select_conductor,
app.voltage_drop_calc's VD formula, app.raceway_calc's conduit-fill area
tables), just run once per (material, parallel-set-count, conduit-material)
combination instead of once for a single predetermined design. Two things a
single-candidate sizer doesn't need to get right that this module does:

1. Ampacity derate count vs. physical fill count are NOT the same number.
   A 3-CCC + neutral + ground run only has 3 (or 4, if the neutral carries
   harmonics) conductors counted for the Table 310.15(C)(1) derate, but all
   5 conductors physically occupy the conduit for Chapter 9 Table 1 fill.
   app.raceway_calc.size_conduit() assumes one uniform conductor size AND one
   conductor_count driving both — fine for its callers, but this module's
   phase/neutral vs. ground sizes usually differ, so conduit fill here is
   computed directly from summed per-conductor areas instead.

2. Equipment grounding conductors on parallel sets are NOT downsized —
   NEC 250.122(F) requires a full-size EGC (per Table 250.122, off the FULL
   OCPD rating) in EVERY parallel raceway, not one shared/divided EGC. Two
   parallel sets means two full #3 (or whatever Table 250.122 calls for),
   not one upsized one. See conductor_tables.equipment_grounding_conductor_size.

Cost model: conductor $/ft is derived from physical weight (circular mils x
a density-derived lb/cmil/1000ft constant, separately for copper and
aluminum) x a market metal price, plus an insulation/manufacturing $ adder
that scales with the conductor's jacketed cross-sectional area, times a
markup multiplier for finished-goods/distributor margin. This is deliberately
NOT a giant hardcoded $/ft-per-size table — copper and aluminum commodity
prices move week to week (and diverged sharply in recent years), so the
volatile input is exposed as MarketPricing.copper_usd_per_lb /
aluminum_usd_per_lb (2 numbers to look up and update) rather than baked into
40+ stale per-size prices. Conduit $/ft and labor-hour tables ARE flat
lookup tables (conduit material cost isn't a simple function of the data
already in this codebase) — every number in DEFAULT_PRICING is a
representative placeholder, not a live quote. Recalibrate against a couple
of current distributor quotes before using this for a real bid; the whole
MarketPricing object is caller-overridable for exactly that reason.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.ampacity_calc import fill_adjustment_factor, temp_correction_factor
from app.conductor_tables import (
    CIRCULAR_MILS,
    CONDUCTOR_ORDER,
    equipment_grounding_conductor_size,
    select_conductor,
)
from app.models import ProjectInput, RacewayRun
from app.ocpd_calc import size_ocpd
from app.raceway_calc import CONDUIT_AREA_IN2, TRADE_SIZES, conductor_area_in2, fill_percent_allowed
from app.voltage_drop_calc import K_OHM_CMIL_PER_FT
import math

# ---------------------------------------------------------------------------
# Physical constants — conductor weight from circular mils x density.
# lb/(cmil x 1000ft) = 7.854e-7 in²/cmil x 12,000 in/1000ft x density(lb/in³).
# Copper density 0.323 lb/in³ (8.94 g/cm³); aluminum (EC-grade, the alloy
# building-wire conductors use) 0.098 lb/in³ (2.70 g/cm³). These reproduce
# NEC Chapter 9 Table 8's bare-conductor weights to within ~1-2%.
# ---------------------------------------------------------------------------
_WEIGHT_LB_PER_CMIL_PER_1000FT: dict[str, float] = {"CU": 0.003044, "AL": 0.0009194}


def conductor_weight_lb_per_ft(conductor: str, material: str) -> float:
    cm = CIRCULAR_MILS.get(conductor, 0)
    k = _WEIGHT_LB_PER_CMIL_PER_1000FT.get(material, _WEIGHT_LB_PER_CMIL_PER_1000FT["CU"])
    return (cm * k) / 1000


class MarketPricing(BaseModel):
    """Every field here is a placeholder — override with current numbers
    before using this for a real bid. copper_usd_per_lb / aluminum_usd_per_lb
    are the two values that actually need refreshing regularly; the rest
    (conduit $/ft, labor hours) are slower-moving and calibrated to be
    order-of-magnitude representative, not exact."""

    copper_usd_per_lb: float = 4.25
    aluminum_usd_per_lb: float = 1.15
    insulation_usd_per_in2_ft: float = 4.0
    insulation_base_usd_per_ft: float = 0.05
    conductor_markup_multiplier: float = 1.35  # manufacturer + distributor margin over raw metal+jacket

    conduit_usd_per_ft: dict[str, dict[str, float]] = Field(default_factory=lambda: {
        "PVC_SCH40": {"1/2": 0.55, "3/4": 0.70, "1": 1.05, "1-1/4": 1.55, "1-1/2": 1.95,
                      "2": 2.70, "2-1/2": 4.60, "3": 6.20, "3-1/2": 7.60, "4": 9.20},
        "PVC_SCH80": {"1/2": 0.95, "3/4": 1.25, "1": 1.85, "1-1/4": 2.70, "1-1/2": 3.40,
                      "2": 4.70, "2-1/2": 7.90, "3": 10.60, "3-1/2": 13.00, "4": 15.80},
        "EMT": {"1/2": 1.10, "3/4": 1.45, "1": 2.30, "1-1/4": 3.30, "1-1/2": 4.10,
                "2": 5.60, "2-1/2": 10.80, "3": 13.90, "3-1/2": 16.90, "4": 19.90},
        "IMC": {"1/2": 3.00, "3/4": 3.80, "1": 5.40, "1-1/4": 7.30, "1-1/2": 8.90,
                "2": 11.90, "2-1/2": 20.40, "3": 26.30, "3-1/2": 32.00, "4": 38.20},
        "RMC": {"1/2": 4.20, "3/4": 5.30, "1": 7.60, "1-1/4": 10.20, "1-1/2": 12.40,
                "2": 16.60, "2-1/2": 28.50, "3": 36.80, "3-1/2": 44.90, "4": 53.60},
    })

    conduit_labor_hours_per_100ft: dict[str, float] = Field(default_factory=lambda: {
        "1/2": 2.0, "3/4": 2.3, "1": 2.8, "1-1/4": 3.4, "1-1/2": 4.0,
        "2": 5.0, "2-1/2": 6.5, "3": 8.0, "3-1/2": 9.5, "4": 11.0,
    })

    pull_base_hours_per_100ft: float = 0.8
    pull_area_coefficient: float = 6.0    # hours per 100ft per in² of jacketed conductor area
    pull_weight_coefficient: float = 0.02  # hours per 100ft per lb-per-100ft of conductor weight

    termination_base_hours: float = 0.35
    termination_area_coefficient: float = 0.6
    aluminum_termination_multiplier: float = 1.4  # NEC 110.14(C): listed AL lugs, anti-ox compound, torque/inspection

    labor_rate_usd_per_hr: float = 95.0


DEFAULT_PRICING = MarketPricing()


def conductor_cost_per_ft(conductor: str, material: str, pricing: MarketPricing) -> float:
    metal_price = pricing.copper_usd_per_lb if material == "CU" else pricing.aluminum_usd_per_lb
    weight_lb_per_ft = conductor_weight_lb_per_ft(conductor, material)
    area_in2 = conductor_area_in2(conductor, "THHN_THWN2")
    metal_cost = weight_lb_per_ft * metal_price
    insulation_cost = pricing.insulation_base_usd_per_ft + area_in2 * pricing.insulation_usd_per_in2_ft
    return (metal_cost + insulation_cost) * pricing.conductor_markup_multiplier


def pull_labor_hours_per_100ft(conductor: str, material: str, pricing: MarketPricing) -> float:
    area_in2 = conductor_area_in2(conductor, "THHN_THWN2")
    weight_lb_per_100ft = conductor_weight_lb_per_ft(conductor, material) * 100
    return (
        pricing.pull_base_hours_per_100ft
        + area_in2 * pricing.pull_area_coefficient
        + weight_lb_per_100ft * pricing.pull_weight_coefficient
    )


def termination_labor_hours(conductor: str, material: str, pricing: MarketPricing) -> float:
    area_in2 = conductor_area_in2(conductor, "THHN_THWN2")
    hours = pricing.termination_base_hours + area_in2 * pricing.termination_area_coefficient
    return hours * (pricing.aluminum_termination_multiplier if material == "AL" else 1.0)


def _conduit_cost_per_ft(trade_size: str, conduit_material: str, pricing: MarketPricing) -> float:
    table = pricing.conduit_usd_per_ft.get(conduit_material, pricing.conduit_usd_per_ft["PVC_SCH40"])
    return table.get(trade_size, 0.0)


def _mixed_fill_trade_size(
    conductor_specs: list[tuple[str, int]],  # (conductor_size, count)
    conduit_material: str,
    physical_fill_count: int,
) -> tuple[str | None, float | None, float]:
    """Smallest standard trade size that fits a raceway containing conductors
    of DIFFERENT sizes (phase/neutral vs. a smaller ground) — summed area,
    same Chapter 9 Table 1 % allowance raceway_calc uses, but not routed
    through raceway_calc.size_conduit() itself since that helper assumes one
    uniform conductor size for the whole run."""
    total_area = sum(conductor_area_in2(size, "THHN_THWN2") * count for size, count in conductor_specs)
    allowed_pct = fill_percent_allowed(physical_fill_count, is_nipple=False)
    sizes = CONDUIT_AREA_IN2.get(conduit_material, CONDUIT_AREA_IN2["PVC_SCH40"])

    for size in TRADE_SIZES:
        area_100 = sizes[size]
        if area_100 * (allowed_pct / 100) >= total_area:
            return size, round((total_area / area_100) * 100, 2), total_area
    return None, None, total_area


class FeederScenario(BaseModel):
    """Electrical/physical description of one feeder or branch run to
    value-engineer. Mirrors the user's example: continuous_current_a is the
    NEC-continuous-load-adjusted (125%) current the OCPD/conductors must
    carry — same convention app.ampacity_calc.size_conductor uses for
    base_current, so this can be fed straight from that result."""

    tag: str = "Feeder-1"
    continuous_current_a: float = 800.0
    voltage_v: float = 480.0
    length_ft: float = 250.0
    ccc_per_set: int = 3  # current-carrying conductors per parallel set (e.g. 3 for a 3-phase feeder)
    neutral_present: bool = True
    neutral_counts_as_ccc: bool = False  # NEC 310.15(E): balanced 3-phase 4-wire neutral usually doesn't count
    insulation_rating: int = 90  # 75 or 90 — the column used for sizing; verify actual terminal rating per 110.14(C)
    ambient_c: float = 35.0
    voltage_drop_limit_pct: float = 2.0
    ground_ocpd_a: float | None = None  # None -> derived via ocpd_calc.size_ocpd(continuous_current_a)
    materials: list[str] = Field(default_factory=lambda: ["CU", "AL"])
    ground_material: str | None = None  # None -> matches each candidate's phase material
    max_parallel_sets: int = 2
    conduit_materials: list[str] = Field(default_factory=lambda: ["PVC_SCH40"])


def _evaluate_candidate(
    scenario: FeederScenario, pricing: MarketPricing, material: str, ground_material: str,
    n_sets: int, conduit_material: str,
) -> dict:
    fail_reasons: list[str] = []
    notes: list[str] = []
    if material == "AL":
        notes.append("Verify OCPD/switchgear lugs are listed for aluminum conductors and use an approved antioxidant compound (NEC 110.14).")
    if ground_material == "AL":
        notes.append("Aluminum equipment grounding conductor — verify termination hardware is AL-listed (NEC 110.14).")

    current_per_set = scenario.continuous_current_a / n_sets
    temp_factor = temp_correction_factor(scenario.ambient_c, scenario.insulation_rating)
    derate_count = scenario.ccc_per_set + (1 if scenario.neutral_present and scenario.neutral_counts_as_ccc else 0)
    fill_factor = fill_adjustment_factor(derate_count)
    combined = temp_factor * fill_factor
    required_ampacity_per_set = current_per_set / combined if combined > 0 else float("inf")

    phase_conductor = select_conductor(required_ampacity_per_set, scenario.insulation_rating, material)
    if phase_conductor is None:
        fail_reasons.append(
            f"No {material} conductor up to {CONDUCTOR_ORDER[-1]} clears "
            f"{required_ampacity_per_set:.1f} A required ampacity per set."
        )

    ground_ocpd_a = scenario.ground_ocpd_a or size_ocpd(scenario.continuous_current_a, circuit="feeder")["standard_size_a"]
    ground_conductor = equipment_grounding_conductor_size(ground_ocpd_a, ground_material)

    vd_result = None
    final_phase_conductor = phase_conductor
    if phase_conductor is not None:
        k = K_OHM_CMIL_PER_FT.get(material, K_OHM_CMIL_PER_FT["CU"])

        def _vd_pct(conductor: str) -> tuple[float, float]:
            cm = CIRCULAR_MILS[conductor]
            volts = (math.sqrt(3) * k * current_per_set * scenario.length_ft) / cm
            return volts, (volts / scenario.voltage_v) * 100 if scenario.voltage_v else 0.0

        volts, pct = _vd_pct(phase_conductor)
        upsized = False
        if pct > scenario.voltage_drop_limit_pct:
            idx = CONDUCTOR_ORDER.index(phase_conductor)
            for candidate in CONDUCTOR_ORDER[idx + 1:]:
                cand_volts, cand_pct = _vd_pct(candidate)
                if cand_pct <= scenario.voltage_drop_limit_pct:
                    volts, pct, final_phase_conductor, upsized = cand_volts, cand_pct, candidate, True
                    break
        vd_result = {
            "starting_conductor": phase_conductor,
            "final_conductor": final_phase_conductor,
            "upsized": upsized,
            "voltage_drop_v": round(volts, 2),
            "voltage_drop_pct": round(pct, 2),
            "passes": pct <= scenario.voltage_drop_limit_pct,
        }
        if not vd_result["passes"]:
            fail_reasons.append(
                f"Voltage drop {pct:.2f}% exceeds {scenario.voltage_drop_limit_pct:.2f}% limit even at "
                f"{CONDUCTOR_ORDER[-1]} (largest size checked)."
            )

    neutral_conductor = final_phase_conductor if scenario.neutral_present else None

    trade_size = fill_pct = None
    if final_phase_conductor is not None:
        specs = [(final_phase_conductor, scenario.ccc_per_set)]
        if scenario.neutral_present:
            specs.append((final_phase_conductor, 1))
        specs.append((ground_conductor, 1))
        physical_fill_count = scenario.ccc_per_set + (1 if scenario.neutral_present else 0) + 1
        trade_size, fill_pct, _ = _mixed_fill_trade_size(specs, conduit_material, physical_fill_count)
        if trade_size is None:
            fail_reasons.append(f"No standard trade size up to {TRADE_SIZES[-1]} in fits this conductor set in {conduit_material}.")

    passes = not fail_reasons

    costs = None
    if passes:
        phase_cost_ft = conductor_cost_per_ft(final_phase_conductor, material, pricing)
        ground_cost_ft = conductor_cost_per_ft(ground_conductor, ground_material, pricing)
        conductor_material_usd = scenario.length_ft * n_sets * (
            phase_cost_ft * scenario.ccc_per_set
            + (phase_cost_ft if scenario.neutral_present else 0.0)
            + ground_cost_ft
        )
        conduit_material_usd = scenario.length_ft * n_sets * _conduit_cost_per_ft(trade_size, conduit_material, pricing)

        phase_pull_hr = pull_labor_hours_per_100ft(final_phase_conductor, material, pricing)
        ground_pull_hr = pull_labor_hours_per_100ft(ground_conductor, ground_material, pricing)
        pull_hours = (scenario.length_ft / 100) * n_sets * (
            phase_pull_hr * scenario.ccc_per_set
            + (phase_pull_hr if scenario.neutral_present else 0.0)
            + ground_pull_hr
        )
        conduit_install_hours = (scenario.length_ft / 100) * n_sets * pricing.conduit_labor_hours_per_100ft.get(trade_size, 0.0)

        conductor_ends_per_set = scenario.ccc_per_set + (1 if scenario.neutral_present else 0) + 1
        phase_term_hr = termination_labor_hours(final_phase_conductor, material, pricing)
        ground_term_hr = termination_labor_hours(ground_conductor, ground_material, pricing)
        termination_hours = 2 * n_sets * (
            phase_term_hr * scenario.ccc_per_set
            + (phase_term_hr if scenario.neutral_present else 0.0)
            + ground_term_hr
        )

        total_labor_hours = pull_hours + conduit_install_hours + termination_hours
        labor_usd = total_labor_hours * pricing.labor_rate_usd_per_hr
        total_installed_usd = conductor_material_usd + conduit_material_usd + labor_usd

        costs = {
            "conductor_material_usd": round(conductor_material_usd, 2),
            "conduit_material_usd": round(conduit_material_usd, 2),
            "labor_hours": round(total_labor_hours, 2),
            "labor_usd": round(labor_usd, 2),
            "total_installed_usd": round(total_installed_usd, 2),
            "cost_per_ft_usd": round(total_installed_usd / scenario.length_ft, 2) if scenario.length_ft else None,
            "raceway_count": n_sets,
        }

    return {
        "material": material,
        "ground_material": ground_material,
        "parallel_sets": n_sets,
        "conduit_material": conduit_material,
        "phase_conductor": final_phase_conductor,
        "neutral_conductor": neutral_conductor,
        "ground_conductor": ground_conductor,
        "ampacity": {
            "temp_correction_factor": temp_factor,
            "fill_adjustment_factor": fill_factor,
            "current_per_set_a": round(current_per_set, 2),
            "required_ampacity_per_set_a": round(required_ampacity_per_set, 2) if math.isfinite(required_ampacity_per_set) else None,
        },
        "voltage_drop": vd_result,
        "raceway": {"trade_size_in": trade_size, "fill_pct": fill_pct, "raceway_count": n_sets},
        "passes": passes,
        "fail_reasons": fail_reasons,
        "notes": notes,
        "costs": costs,
    }


class FeederValueEngineeringRequest(BaseModel):
    """API request wrapper — pricing is optional so a caller can lean on
    DEFAULT_PRICING and only override the couple of fields (usually
    copper_usd_per_lb / aluminum_usd_per_lb) that actually need refreshing."""

    scenario: FeederScenario = Field(default_factory=FeederScenario)
    pricing: MarketPricing | None = None


def evaluate_feeder_value_engineering(scenario: FeederScenario, pricing: MarketPricing | None = None) -> dict:
    pricing = pricing or DEFAULT_PRICING
    candidates: list[dict] = []

    for material in scenario.materials:
        ground_material = scenario.ground_material or material
        for n_sets in range(1, scenario.max_parallel_sets + 1):
            for conduit_material in scenario.conduit_materials:
                candidates.append(
                    _evaluate_candidate(scenario, pricing, material, ground_material, n_sets, conduit_material)
                )

    passing = [c for c in candidates if c["passes"]]
    passing_sorted = sorted(passing, key=lambda c: c["costs"]["total_installed_usd"])
    failing = [c for c in candidates if not c["passes"]]

    recommended = passing_sorted[0] if passing_sorted else None
    most_expensive_passing = passing_sorted[-1] if passing_sorted else None

    savings_vs_most_expensive = None
    if recommended is not None and most_expensive_passing is not None and recommended is not most_expensive_passing:
        delta = most_expensive_passing["costs"]["total_installed_usd"] - recommended["costs"]["total_installed_usd"]
        savings_vs_most_expensive = {
            "cheapest": f"{recommended['material']} x{recommended['parallel_sets']} set(s)",
            "most_expensive": f"{most_expensive_passing['material']} x{most_expensive_passing['parallel_sets']} set(s)",
            "savings_usd": round(delta, 2),
            "savings_pct": round((delta / most_expensive_passing["costs"]["total_installed_usd"]) * 100, 1),
        }

    return {
        "scenario": scenario.model_dump(),
        "candidates": passing_sorted + failing,
        "candidate_count": len(candidates),
        "passing_count": len(passing),
        "recommended": recommended,
        "savings_vs_most_expensive_passing": savings_vs_most_expensive,
    }


# ---------------------------------------------------------------------------
# Project integration — run this against a project's already-defined raceway
# run schedule (app.models.RacewayRun / ProjectInput.raceway_runs, the same
# rows the "Conduit, Tray & Messenger" panel and app.raceway_calc size)
# instead of re-entering current/voltage/length by hand. A RacewayRun already
# carries almost everything a FeederScenario needs (current, voltage, length,
# insulation rating, its own vd_limit_pct, conductor_insulation) -- what it
# doesn't carry is CCC-vs-neutral-vs-ground structure or which materials/
# parallel-set counts to compare, since a raceway run is "N conductors" with
# no phase/neutral/ground breakdown. FeederVeSettings supplies just that,
# keyed by the run's own tag so the UI can attach one settings row per
# raceway row without duplicating its electrical inputs.
# ---------------------------------------------------------------------------
class FeederVeSettings(BaseModel):
    ccc_per_set: int = 3
    neutral_present: bool = True
    neutral_counts_as_ccc: bool = False
    ground_ocpd_a: float | None = None
    materials: list[str] = Field(default_factory=lambda: ["CU", "AL"])
    ground_material: str | None = None
    max_parallel_sets: int = 2
    conduit_materials: list[str] | None = None  # None -> just the raceway run's own conduit_material


def feeder_scenario_from_raceway_run(run: RacewayRun, settings: FeederVeSettings, ambient_c: float) -> FeederScenario:
    return FeederScenario(
        tag=run.tag,
        continuous_current_a=run.current_a,
        voltage_v=run.voltage_v,
        length_ft=run.length_ft,
        ccc_per_set=settings.ccc_per_set,
        neutral_present=settings.neutral_present,
        neutral_counts_as_ccc=settings.neutral_counts_as_ccc,
        insulation_rating=run.insulation_rating,
        ambient_c=ambient_c,
        voltage_drop_limit_pct=run.vd_limit_pct,
        ground_ocpd_a=settings.ground_ocpd_a,
        materials=settings.materials,
        ground_material=settings.ground_material,
        max_parallel_sets=settings.max_parallel_sets,
        conduit_materials=settings.conduit_materials or [run.conduit_material],
    )


class ProjectFeederVeRequest(BaseModel):
    """Runs value engineering across every raceway run on a saved project.
    `settings` is keyed by RacewayRun.tag -- runs with no entry fall back to
    FeederVeSettings() defaults (3 CCC, neutral present, both materials,
    up to 2 parallel sets), so this is usable on an unconfigured project,
    not just one that's had every row hand-tuned first."""

    project: ProjectInput
    settings: dict[str, FeederVeSettings] = Field(default_factory=dict)
    pricing: MarketPricing | None = None


def evaluate_project_feeders(request: ProjectFeederVeRequest) -> dict:
    pricing = request.pricing or DEFAULT_PRICING
    ambient_c = request.project.ashrae.max_design_temp_c

    results: dict[str, dict] = {}
    for run in request.project.raceway_runs:
        ve_settings = request.settings.get(run.tag, FeederVeSettings())
        scenario = feeder_scenario_from_raceway_run(run, ve_settings, ambient_c)
        results[run.tag] = evaluate_feeder_value_engineering(scenario, pricing)

    return {"runs": results, "run_count": len(results)}
