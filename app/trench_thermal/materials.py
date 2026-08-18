"""Material property helpers: unit conversions, conductor electrical
properties, the closed-form internal thermal-resistance primitives, and the
soil-zone (backfill vs. native) conductivity field.

Thermal resistivity is conventionally reported in the industry (geotech
reports, IEEE 835) as degC*cm/W. The solver works internally in SI:
thermal conductivity k = 1 / rho, with rho in K*m/W (equivalently degC*m/W).

Nothing here imports from the rest of `app` -- which insulation type maps to
which thermal resistivity, and what a given conduit trade size's inner and
outer diameters are, is app data and lives in app/trench_calc.py.
"""

from __future__ import annotations

import math

import numpy as np

# Volume resistivity at 20 degC [ohm*m] and temperature coefficient [1/degC].
# Copper: IACS annealed. Aluminum: EC-grade (1350), the alloy building-wire
# conductors use -- same material split app/wire_cost_calc.py prices against.
CONDUCTOR_RESISTIVITY_20C: dict[str, float] = {"CU": 1.724e-8, "AL": 2.826e-8}
CONDUCTOR_TEMP_COEFF: dict[str, float] = {"CU": 0.00393, "AL": 0.00403}

# Kept as the prototype's names so the physics reads the same as the
# validation write-up it was calibrated against.
CU_RESISTIVITY_20C = CONDUCTOR_RESISTIVITY_20C["CU"]
CU_TEMP_COEFF = CONDUCTOR_TEMP_COEFF["CU"]


def rho_cm_to_m(rho_degC_cm_per_W: float) -> float:
    """Convert thermal resistivity from degC*cm/W to K*m/W (SI)."""
    return rho_degC_cm_per_W * 0.01


def k_from_rho_cm(rho_degC_cm_per_W: float) -> float:
    """Thermal conductivity [W/(m*K)] from resistivity given in degC*cm/W."""
    return 1.0 / rho_cm_to_m(rho_degC_cm_per_W)


def conductor_dc_resistance_per_m(area_mm2: float, temp_C: float, material: str = "CU") -> float:
    """DC resistance per unit length [ohm/m] at temp_C, from cross-sectional
    area in mm^2. AC skin/proximity effects are NOT included here -- see
    ac_resistance_factors() for those."""
    area_m2 = area_mm2 * 1e-6
    rho20 = CONDUCTOR_RESISTIVITY_20C.get(material, CONDUCTOR_RESISTIVITY_20C["CU"])
    alpha = CONDUCTOR_TEMP_COEFF.get(material, CONDUCTOR_TEMP_COEFF["CU"])
    r20 = rho20 / area_m2
    return r20 * (1.0 + alpha * (temp_C - 20.0))


def copper_resistance_per_m(area_mm2: float, temp_C: float) -> float:
    """Backwards-compatible copper-only wrapper (the prototype's name)."""
    return conductor_dc_resistance_per_m(area_mm2, temp_C, "CU")


# ---------------------------------------------------------------------------
# AC resistance: skin and proximity effect, IEC 60287-1-1 section 2.1.
#
# This closes the prototype's "AC skin/proximity effects not included" stub
# with the standard's own formulation rather than an assumption that they're
# negligible. They very nearly are for small PV feeders (<1% below ~250
# kcmil) but reach several percent at 750 kcmil, which is exactly the size
# range this app's larger AC feeders land in.
#
# k_s / k_p default to 1.0 (round stranded, non-compacted) -- the
# conservative choice, since compacted or segmental constructions have
# LOWER factors. Verify against manufacturer cable data before issue if a
# design is close to the thermal limit.
# ---------------------------------------------------------------------------
_SKIN_VALID_MAX_XS = 2.8   # IEC's stated validity bound for the y_s series form


def _iec_xi4_term(r_dc_per_m: float, frequency_hz: float, k: float) -> float:
    """The x^4/(192 + 0.8 x^4) group shared by the skin and proximity terms,
    with x^2 = (8*pi*f / R') * 1e-7 * k and R' in ohm/m."""
    x2 = (8.0 * math.pi * frequency_hz / r_dc_per_m) * 1e-7 * k
    x4 = x2 * x2
    return x4 / (192.0 + 0.8 * x4)


def ac_resistance_factors(
    r_dc_per_m: float,
    conductor_diameter_m: float,
    axial_spacing_m: float,
    frequency_hz: float = 60.0,
    k_s: float = 1.0,
    k_p: float = 1.0,
) -> tuple[float, float]:
    """Returns (y_s, y_p): the skin- and proximity-effect increments, such
    that R_ac = R_dc * (1 + y_s + y_p).

    axial_spacing_m is the centre-to-centre distance between adjacent
    conductors -- for single conductors bundled in one conduit, that is
    essentially their overall insulated diameter (touching).
    """
    if r_dc_per_m <= 0 or frequency_hz <= 0:
        return 0.0, 0.0

    y_s = _iec_xi4_term(r_dc_per_m, frequency_hz, k_s)

    y_p = 0.0
    if axial_spacing_m > 0 and conductor_diameter_m > 0:
        group = _iec_xi4_term(r_dc_per_m, frequency_hz, k_p)
        ratio = conductor_diameter_m / axial_spacing_m
        y_p = group * ratio**2 * (0.312 * ratio**2 + 1.18 / (group + 0.27))

    return y_s, y_p


def skin_effect_within_validity(r_dc_per_m: float, frequency_hz: float = 60.0, k_s: float = 1.0) -> bool:
    """True when x_s <= 2.8, IEC 60287-1-1's stated validity bound for the
    series expression used above. Conductors this app sizes stay well inside
    it; a caller that hits False is outside the formula's range and should
    fall back to manufacturer AC/DC ratio data."""
    if r_dc_per_m <= 0:
        return False
    x2 = (8.0 * math.pi * frequency_hz / r_dc_per_m) * 1e-7 * k_s
    return math.sqrt(x2) <= _SKIN_VALID_MAX_XS


# ---------------------------------------------------------------------------
# Internal thermal-resistance chain (conductor -> duct outer wall).
#
# The FV solver computes everything OUTSIDE the duct wall. These three
# closed-form pieces cover everything inside it, and together replace the
# prototype's single lumped `default_R_int_estimate()` placeholder:
#
#   T_conductor = T_duct_wall
#               + W_per_conductor * R_insulation      (each conductor's own loss
#                                                      crosses only its own jacket)
#               + Q_conduit_total * (R_air_space + R_duct_wall)
#                                                     (all conductors' loss crosses
#                                                      the shared air gap and duct)
#
# which is the standard Neher-McGrath R_ca = R_i + n*(R_sd + R_d + R_e)
# decomposition rearranged: with Q_total = n * W_per_conductor, the two forms
# are identical. R_e (external) is what the numerical solve replaces.
# ---------------------------------------------------------------------------
def thermal_resistance_insulation(rho_insulation_km_per_w: float,
                                  conductor_diameter_m: float,
                                  over_insulation_diameter_m: float) -> float:
    """Radial conduction through one conductor's own insulation [K*m/W].
    IEC 60287-2-1 T_1 for a single-core cable."""
    if conductor_diameter_m <= 0 or over_insulation_diameter_m <= conductor_diameter_m:
        return 0.0
    return (rho_insulation_km_per_w / (2.0 * math.pi)) * math.log(
        over_insulation_diameter_m / conductor_diameter_m
    )


# IEC 60287-2-1 Table 5 constants for the air space between a cable (or
# bundle) and the duct it sits in: T'_4 = U / (1 + 0.1*(V + Y*theta_m)*D_e),
# with D_e the bundle external diameter in MILLIMETRES and theta_m the mean
# temperature of the medium filling the space in degC. This is the SI
# statement of the same empirical relation Neher-McGrath gives in
# thermal-ohm-feet.
AIR_SPACE_CONSTANTS: dict[str, tuple[float, float, float]] = {
    "metallic": (5.2, 1.4, 0.011),      # metal conduit (EMT / IMC / RMC)
    "nonmetallic": (1.87, 0.28, 0.0036),  # PVC / PE duct
    "fibre": (5.2, 0.83, 0.006),          # fibre duct in air
}


def thermal_resistance_air_space(bundle_diameter_m: float,
                                 mean_temp_C: float,
                                 duct_class: str = "nonmetallic") -> float:
    """Thermal resistance of the air space between the conductor bundle and
    the duct's inner wall [K*m/W] -- IEC 60287-2-1 section 4.1.4.

    Crossed by the WHOLE conduit's heat, not one conductor's."""
    u, v, y = AIR_SPACE_CONSTANTS.get(duct_class, AIR_SPACE_CONSTANTS["nonmetallic"])
    d_e_mm = bundle_diameter_m * 1000.0
    denom = 1.0 + 0.1 * (v + y * mean_temp_C) * d_e_mm
    return u / denom if denom > 0 else 0.0


def thermal_resistance_duct_wall(rho_duct_km_per_w: float,
                                 inner_diameter_m: float,
                                 outer_diameter_m: float) -> float:
    """Radial conduction through the duct wall itself [K*m/W] -- IEC
    60287-2-1 section 4.1.5. Effectively zero for steel conduit (pass a
    conductivity high enough that the log term vanishes, or just 0.0)."""
    if inner_diameter_m <= 0 or outer_diameter_m <= inner_diameter_m:
        return 0.0
    return (rho_duct_km_per_w / (2.0 * math.pi)) * math.log(outer_diameter_m / inner_diameter_m)


# ---------------------------------------------------------------------------
# Soil zones
# ---------------------------------------------------------------------------
class SoilZones:
    """Two-zone soil model: a backfill envelope embedded in native soil.

    The envelope defaults to an axis-aligned rectangle (the common
    engineered-backfill trench section). Pass `polygon` -- a list of (x, y)
    vertices in trench cross-section coordinates -- for an irregular
    envelope; the FV solver is geometry-agnostic, so nothing else changes.
    The rectangle bounds are still used for grid sizing and for the drawing's
    envelope extents, so they must bound the polygon; when a polygon is
    given and bounds are omitted, they are derived from it.
    """

    def __init__(self, backfill_rho_cm, native_rho_cm, x_min=None, x_max=None,
                 y_min=None, y_max=None, polygon=None):
        self.backfill_rho_cm = backfill_rho_cm
        self.native_rho_cm = native_rho_cm
        self.k_backfill = k_from_rho_cm(backfill_rho_cm)
        self.k_native = k_from_rho_cm(native_rho_cm)
        self.polygon = [(float(px), float(py)) for px, py in polygon] if polygon else None

        if self.polygon:
            if len(self.polygon) < 3:
                raise ValueError("backfill polygon needs at least 3 vertices")
            xs = [p[0] for p in self.polygon]
            ys = [p[1] for p in self.polygon]
            x_min = min(xs) if x_min is None else min(x_min, min(xs))
            x_max = max(xs) if x_max is None else max(x_max, max(xs))
            y_min = min(ys) if y_min is None else min(y_min, min(ys))
            y_max = max(ys) if y_max is None else max(y_max, max(ys))
        if x_min is None or x_max is None or y_min is None or y_max is None:
            raise ValueError("SoilZones needs either rectangle bounds or a polygon")

        self.x_min, self.x_max = x_min, x_max
        self.y_min, self.y_max = y_min, y_max

    def contains(self, x, y) -> bool:
        if self.polygon is None:
            return (self.x_min <= x <= self.x_max) and (self.y_min <= y <= self.y_max)
        return _point_in_polygon(x, y, self.polygon)

    def k_at(self, x, y) -> float:
        return self.k_backfill if self.contains(x, y) else self.k_native

    def k_field(self, grid):
        """Vectorized conductivity field over all grid cells -> (ny, nx) array."""
        if self.polygon is None:
            # Rectangle: separable in x and y, so build it from two 1D masks
            # instead of nx*ny scalar calls.
            in_x = (grid.xc >= self.x_min) & (grid.xc <= self.x_max)
            in_y = (grid.yc >= self.y_min) & (grid.yc <= self.y_max)
            mask = np.outer(in_y, in_x)
            return np.where(mask, self.k_backfill, self.k_native)

        K = np.empty((grid.ny, grid.nx))
        for j in range(grid.ny):
            for i in range(grid.nx):
                K[j, i] = self.k_at(grid.xc[i], grid.yc[j])
        return K


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Standard even-odd ray-casting test. Points exactly on an edge are
    boundary cases and may land either way -- irrelevant here, since a cell
    centre landing on the envelope boundary differs by one cell's worth of
    conductivity either way."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside
