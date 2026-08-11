"""Raceway sizing for a set of conductors sharing one conduit, cable tray, or
messenger (catenary) cable run — NEC Chapter 9 (Tables 1, 4, 5) for conduit
fill, Article 392 (392.22) for cable tray fill, and a messenger-cable
tension/strength check for Article 396 messenger-supported wiring.

Reuses ampacity_calc's temp-correction and conductor-count fill-derate chain
so a raceway run's conductor is sized the same way the Ampacity & Conductors
panel sizes one — the run's actual conductor_count (not just the "how many
conductors share this ampacity derate" input on that panel) is what drives
both the ampacity derate AND the raceway fill % here, so the two can't drift
apart for a given run.

All tables below are common-size subsets, not the full interpolated NEC
tables — same posture as conductor_tables.py and ampacity_calc.py. Verify
against the current NEC edition and manufacturer cable data before issue.
"""

from __future__ import annotations

import math

from app.ampacity_calc import fill_adjustment_factor, temp_correction_factor
from app.conductor_tables import CIRCULAR_MILS, CONDUCTOR_ORDER, select_conductor
from app.models import RacewayRun

# ---------------------------------------------------------------------------
# NEC Chapter 9, Table 5 (subset) — THHN/THWN-2 conductor cross-sectional
# area, sq in. USE-2/RHW-2 (thicker insulation, typical for PV source-circuit
# single-conductor cable) is approximated as 28% larger per size rather than
# a second hand-keyed table — flagged, not a Table 5 lookup.
# ---------------------------------------------------------------------------
THHN_THWN2_AREA_IN2: dict[str, float] = {
    "10 AWG": 0.0211, "8 AWG": 0.0366, "6 AWG": 0.0507, "4 AWG": 0.0824,
    "3 AWG": 0.0973, "2 AWG": 0.1158, "1 AWG": 0.1562, "1/0 AWG": 0.1855,
    "2/0 AWG": 0.2223, "3/0 AWG": 0.2679, "4/0 AWG": 0.3237,
    "250 kcmil": 0.3970, "300 kcmil": 0.4536, "350 kcmil": 0.5111,
    "400 kcmil": 0.5623, "500 kcmil": 0.6759, "600 kcmil": 0.7896,
    "700 kcmil": 0.9028, "750 kcmil": 0.9605,
}
_USE2_RHW2_FACTOR = 1.28

# ---------------------------------------------------------------------------
# NEC Chapter 9, Table 1 — allowable raceway fill vs. conductor count.
# ---------------------------------------------------------------------------
def fill_percent_allowed(conductor_count: int, is_nipple: bool) -> float:
    if is_nipple:  # raceways <= 24 in (Chapter 9, Table 1, Note to Table 1)
        return 60.0
    if conductor_count <= 1:
        return 53.0
    if conductor_count == 2:
        return 31.0
    return 40.0


def conductor_area_in2(conductor: str, insulation: str) -> float:
    base = THHN_THWN2_AREA_IN2.get(conductor)
    if base is None:
        return 0.0
    return round(base * _USE2_RHW2_FACTOR, 4) if insulation == "USE2_RHW2" else base


def conductor_od_in(conductor: str, insulation: str) -> float:
    """Approximate single-conductor outside diameter from its area, treating
    the conductor as circular — used for cable-tray sum-of-diameters checks
    and messenger ice loading, not a manufacturer OD."""
    area = conductor_area_in2(conductor, insulation)
    return 2 * math.sqrt(area / math.pi) if area else 0.0


# ---------------------------------------------------------------------------
# NEC Chapter 9, Table 4 (subset) — total internal area (100%), sq in, common
# trade sizes 1/2 in - 4 in.
# ---------------------------------------------------------------------------
TRADE_SIZES: list[str] = ["1/2", "3/4", "1", "1-1/4", "1-1/2", "2", "2-1/2", "3", "3-1/2", "4"]

CONDUIT_AREA_IN2: dict[str, dict[str, float]] = {
    "EMT": {"1/2": 0.304, "3/4": 0.533, "1": 0.864, "1-1/4": 1.496, "1-1/2": 2.036,
            "2": 3.356, "2-1/2": 5.858, "3": 8.846, "3-1/2": 11.545, "4": 14.753},
    "IMC": {"1/2": 0.342, "3/4": 0.586, "1": 0.959, "1-1/4": 1.647, "1-1/2": 2.225,
            "2": 3.630, "2-1/2": 5.135, "3": 7.922, "3-1/2": 9.696, "4": 11.545},
    "RMC": {"1/2": 0.314, "3/4": 0.549, "1": 0.887, "1-1/4": 1.526, "1-1/2": 2.071,
            "2": 3.408, "2-1/2": 4.866, "3": 7.499, "3-1/2": 8.837, "4": 10.010},
    "PVC_SCH40": {"1/2": 0.285, "3/4": 0.508, "1": 0.832, "1-1/4": 1.453, "1-1/2": 1.986,
                  "2": 3.291, "2-1/2": 4.695, "3": 7.268, "3-1/2": 9.737, "4": 12.554},
    "PVC_SCH80": {"1/2": 0.217, "3/4": 0.409, "1": 0.688, "1-1/4": 1.237, "1-1/2": 1.711,
                  "2": 2.874, "2-1/2": 4.119, "3": 6.442, "3-1/2": 8.688, "4": 11.258},
}

# Magnetic (steel) conduit adds a small inductive-reactance voltage-drop
# adder for AC circuits vs. nonmagnetic PVC — see VD_CONDUIT_MULTIPLIER.
METAL_CONDUIT_MATERIALS = {"EMT", "IMC", "RMC"}


def size_conduit(conductor: str, insulation: str, conductor_count: int, material: str, is_nipple: bool) -> dict:
    area_per_conductor = conductor_area_in2(conductor, insulation)
    total_conductor_area = round(area_per_conductor * conductor_count, 4)
    allowed_pct = fill_percent_allowed(conductor_count, is_nipple)
    sizes = CONDUIT_AREA_IN2.get(material, CONDUIT_AREA_IN2["PVC_SCH40"])

    selected_size = None
    actual_fill_pct = None
    for size in TRADE_SIZES:
        area_100 = sizes[size]
        if area_100 * (allowed_pct / 100) >= total_conductor_area:
            selected_size = size
            actual_fill_pct = round((total_conductor_area / area_100) * 100, 2) if area_100 else None
            break

    return {
        "material": material,
        "is_metal": material in METAL_CONDUIT_MATERIALS,
        "area_per_conductor_in2": area_per_conductor,
        "total_conductor_area_in2": total_conductor_area,
        "allowed_fill_pct": allowed_pct,
        "selected_trade_size_in": selected_size,
        "actual_fill_pct": actual_fill_pct,
        "fits": selected_size is not None,
    }


# ---------------------------------------------------------------------------
# NEC 392.22(B) — single-conductor cable tray fill (the common PV case: bare
# USE-2/RHW-2 or THHN/THWN-2 home-run conductors laid in tray, not a
# manufactured multiconductor cable). Ratios below are derived from the
# published Table 392.22(B)(1)/(B)(2) allowable-fill-area-per-tray-width
# entries (e.g. 6 in -> 5.5 sq in for 250-900 kcmil, 6 in -> 4 sq in for
# 1/0-4/0 AWG) expressed as area-per-inch-of-width so any tray width works,
# not just the table's listed widths.
# ---------------------------------------------------------------------------
STANDARD_TRAY_WIDTHS_IN: list[float] = [6, 9, 12, 18, 24, 30, 36]

_AREA_RATIO_SUB_1_0 = 2 / 3      # smaller than 1/0 AWG (392.22(B)(2)-equivalent)
_AREA_RATIO_250_900 = 11 / 12    # 250-900 kcmil (392.22(B)(1)-equivalent)
_MIN_AWG_FOR_LADDER_WITHOUT_NOTE = "1/0 AWG"


def _tray_area_ratio(conductor: str) -> float:
    idx = CONDUCTOR_ORDER.index(conductor)
    return _AREA_RATIO_250_900 if idx >= CONDUCTOR_ORDER.index("250 kcmil") else _AREA_RATIO_SUB_1_0


def size_cable_tray(conductor: str, insulation: str, conductor_count: int, tray_type: str, tray_width_in: float) -> dict:
    area_per_conductor = conductor_area_in2(conductor, insulation)
    total_conductor_area = round(area_per_conductor * conductor_count, 4)
    ratio = _tray_area_ratio(conductor)

    max_allowed_area_in2 = round(tray_width_in * ratio, 4)
    actual_fill_pct = round((total_conductor_area / max_allowed_area_in2) * 100, 2) if max_allowed_area_in2 else None
    passes = total_conductor_area <= max_allowed_area_in2

    required_width_in = total_conductor_area / ratio if ratio else 0.0
    min_required_width_in = next((w for w in STANDARD_TRAY_WIDTHS_IN if w >= required_width_in), STANDARD_TRAY_WIDTHS_IN[-1])

    idx = CONDUCTOR_ORDER.index(conductor)
    ladder_rung_note = (
        tray_type == "ladder"
        and idx < CONDUCTOR_ORDER.index(_MIN_AWG_FOR_LADDER_WITHOUT_NOTE)
    )

    return {
        "tray_type": tray_type,
        "area_per_conductor_in2": area_per_conductor,
        "total_conductor_area_in2": total_conductor_area,
        "max_allowed_area_in2": max_allowed_area_in2,
        "actual_fill_pct": actual_fill_pct,
        "passes": passes,
        "min_required_width_in": min_required_width_in,
        "ladder_rung_spacing_note": ladder_rung_note,
    }


# ---------------------------------------------------------------------------
# Messenger / catenary support cable sizing (NEC Article 396) — not a fill
# calc: this is the mechanical strength of the steel strand carrying the
# conductors' weight (plus ice) over a span. NEC 396 requires the messenger
# be "of sufficient strength" but doesn't itself give a sizing formula; this
# is the standard sag-tension approach (parabolic catenary approximation)
# against representative EHS galvanized steel strand breaking strengths —
# verify against the manufacturer cut sheet before issue.
# ---------------------------------------------------------------------------
_BARE_COPPER_LB_PER_FT_PER_CM = 0.000003117  # bare-copper weight vs. circular mils
_INSULATION_WEIGHT_FACTOR = 1.35             # jacket/insulation allowance over bare copper
_ICE_DENSITY_COEFFICIENT = 1.24              # NESC radial-ice weight formula coefficient

MESSENGER_STRAND_TABLE: list[tuple[str, float]] = [
    ("1/4 in EHS", 6650.0),
    ("5/16 in EHS", 11200.0),
    ("3/8 in EHS", 15400.0),
    ("7/16 in EHS", 20800.0),
    ("1/2 in EHS", 26900.0),
]


def size_messenger_cable(
    conductor: str,
    insulation: str,
    conductor_count: int,
    span_ft: float,
    ice_thickness_in: float,
    sag_ratio: float,
    safety_factor: float,
    wind_load_plf: float = 0.0,
) -> dict:
    cm = CIRCULAR_MILS.get(conductor, 0)
    bare_cu_weight_plf = cm * _BARE_COPPER_LB_PER_FT_PER_CM
    insulated_weight_plf = bare_cu_weight_plf * _INSULATION_WEIGHT_FACTOR

    od_in = conductor_od_in(conductor, insulation)
    ice_weight_plf_per_cable = (
        _ICE_DENSITY_COEFFICIENT * ice_thickness_in * (od_in + ice_thickness_in) if ice_thickness_in > 0 else 0.0
    )
    per_cable_weight_plf = insulated_weight_plf + ice_weight_plf_per_cable
    total_load_plf = round(per_cable_weight_plf * conductor_count + wind_load_plf, 3)

    sag_ft = sag_ratio * span_ft
    max_tension_lb = (total_load_plf * span_ft ** 2) / (8 * sag_ft) if sag_ft > 0 else float("inf")
    required_breaking_strength_lb = round(max_tension_lb * safety_factor, 1) if math.isfinite(max_tension_lb) else None

    selected = None
    selected_strength = None
    if required_breaking_strength_lb is not None:
        for label, strength in MESSENGER_STRAND_TABLE:
            if strength >= required_breaking_strength_lb:
                selected = label
                selected_strength = strength
                break

    return {
        "per_cable_weight_plf": round(per_cable_weight_plf, 3),
        "total_load_plf": total_load_plf,
        "sag_ft": round(sag_ft, 2),
        "max_tension_lb": round(max_tension_lb, 1) if math.isfinite(max_tension_lb) else None,
        "required_breaking_strength_lb": required_breaking_strength_lb,
        "selected_messenger": selected,
        "selected_breaking_strength_lb": selected_strength,
        "fits": selected is not None,
    }


# ---------------------------------------------------------------------------
# Voltage-drop effect of raceway material — steel (magnetic) conduit adds
# inductive reactance an AC circuit sees but PVC/tray/messenger don't; this
# is an approximate adder (not a full impedance calc), applied only to AC
# circuits, matching voltage_drop_calc.py's simplified single-point model.
# ---------------------------------------------------------------------------
VD_STEEL_CONDUIT_AC_MULTIPLIER = 1.05


def voltage_drop_multiplier(run: RacewayRun) -> float:
    if run.circuit_type != "ac":
        return 1.0
    if run.raceway_type == "conduit" and run.conduit_material in METAL_CONDUIT_MATERIALS:
        return VD_STEEL_CONDUIT_AC_MULTIPLIER
    return 1.0


def compute_raceway_run(run: RacewayRun, ambient_c: float) -> dict:
    temp_factor = temp_correction_factor(ambient_c, run.insulation_rating)
    fill_factor = fill_adjustment_factor(run.conductor_count)
    required_ampacity = run.current_a / (temp_factor * fill_factor) if temp_factor and fill_factor else 0.0
    conductor = select_conductor(required_ampacity, run.insulation_rating)

    result: dict = {
        "tag": run.tag,
        "raceway_type": run.raceway_type,
        "circuit_type": run.circuit_type,
        "temp_correction_factor": temp_factor,
        "fill_adjustment_factor": fill_factor,
        "required_ampacity_a": round(required_ampacity, 2),
        "selected_conductor": conductor,
    }

    if conductor is None:
        result["raceway"] = None
        result["voltage_drop"] = None
        return result

    if run.raceway_type == "conduit":
        result["raceway"] = size_conduit(
            conductor, run.conductor_insulation, run.conductor_count, run.conduit_material, run.is_nipple
        )
    elif run.raceway_type == "cable_tray":
        result["raceway"] = size_cable_tray(
            conductor, run.conductor_insulation, run.conductor_count, run.tray_type, run.tray_width_in
        )
    else:  # messenger
        result["raceway"] = size_messenger_cable(
            conductor, run.conductor_insulation, run.conductor_count,
            run.span_ft, run.ice_thickness_in, run.sag_ratio, run.safety_factor, run.wind_load_plf,
        )

    cm = CIRCULAR_MILS.get(conductor)
    vd_multiplier = voltage_drop_multiplier(run)
    if cm and run.voltage_v:
        vd_volts = (math.sqrt(3) * 12.9 * run.current_a * run.length_ft * vd_multiplier) / cm
        vd_pct = (vd_volts / run.voltage_v) * 100
        result["voltage_drop"] = {
            "multiplier": vd_multiplier,
            "voltage_drop_v": round(vd_volts, 2),
            "voltage_drop_pct": round(vd_pct, 2),
            "passes": vd_pct <= run.vd_limit_pct,
        }
    else:
        result["voltage_drop"] = None

    return result


def compute_raceway_runs(runs: list[RacewayRun], ambient_c: float) -> dict:
    rows = [compute_raceway_run(run, ambient_c) for run in runs]
    return {"rows": rows, "run_count": len(rows)}
