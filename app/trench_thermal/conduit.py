"""Conduit/conductor representation and the outer nonlinear iteration that
couples the closed-form internal thermal-resistance chain to the numerical
external FV solve.

The prototype this came from carried a single lumped `R_int` placeholder and
modelled one conductor per conduit. Both are gone: a Conduit here holds real
conductor and duct geometry (supplied by app/trench_calc.py from the
project's own wire and raceway data) and carries `n_ccc` current-carrying
conductors, so a 3-CCC AC feeder injects three conductors' worth of heat into
the soil rather than one.

Temperature chain, per conduit:

    W_c   = I^2 * R_ac(T_conductor)          per-conductor loss  [W/m]
    Q     = n_ccc * W_c                      what the soil sees  [W/m]
    T_wall= FV solve at the conduit's cell, corrected for the point-source
            discretization artifact (see solver.corrected_wall_temperature)
    T_cond= T_wall + W_c * R_insulation + Q * (R_air_space + R_duct_wall)

R_ac, and R_air_space through its mean-temperature term, both depend on
T_cond -- hence the outer iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.trench_thermal.materials import (
    ac_resistance_factors,
    conductor_dc_resistance_per_m,
    thermal_resistance_air_space,
    thermal_resistance_duct_wall,
    thermal_resistance_insulation,
)
from app.trench_thermal.solver import corrected_wall_temperature


@dataclass
class Conduit:
    """One buried conduit and the conductors in it.

    Position (x, y) is assigned by the optimizer; every other field is real
    design data supplied by the integration seam. Lengths are metres.
    """

    id: str
    # --- electrical ---
    I_design: float          # A per current-carrying conductor. Already includes
                             # 690.8 / continuous-duty / NEC 310.15(C)(1) conduit-fill
                             # adjustment from the wire & breaker module -- those are
                             # NOT reapplied here (different mechanism: internal
                             # conduit crowding vs. external soil heat rejection).
    n_ccc: int               # current-carrying conductors in this conduit
    area_mm2: float          # per-conductor cross-sectional area
    material: str = "CU"     # "CU" | "AL"
    is_ac: bool = True
    frequency_hz: float = 60.0

    # --- geometry ---
    conductor_diameter_m: float = 0.0          # bare conductor OD
    over_insulation_diameter_m: float = 0.0    # one insulated conductor's OD
    bundle_diameter_m: float = 0.0             # effective OD of all conductors together
    duct_inner_diameter_m: float = 0.0
    duct_outer_diameter_m: float = 0.0

    # --- materials ---
    rho_insulation_km_per_w: float = 3.5       # XLPE default; PVC ~5.0
    rho_duct_km_per_w: float = 6.0             # rigid PVC default; 0 for steel
    duct_class: str = "nonmetallic"            # keys materials.AIR_SPACE_CONSTANTS

    # --- assigned by the optimizer ---
    x: float = 0.0
    y: float = 0.0

    # cached, position-independent
    _r_insulation: float = field(default=0.0, init=False, repr=False)
    _r_duct_wall: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self):
        self._r_insulation = thermal_resistance_insulation(
            self.rho_insulation_km_per_w,
            self.conductor_diameter_m,
            self.over_insulation_diameter_m,
        )
        self._r_duct_wall = thermal_resistance_duct_wall(
            self.rho_duct_km_per_w,
            self.duct_inner_diameter_m,
            self.duct_outer_diameter_m,
        )

    @property
    def r_duct(self) -> float:
        """Conduit outer radius [m] -- the radius the source-radius correction
        resolves the FV point source to."""
        return self.duct_outer_diameter_m / 2.0

    @property
    def r_insulation(self) -> float:
        return self._r_insulation

    @property
    def r_duct_wall(self) -> float:
        return self._r_duct_wall

    def r_air_space(self, mean_temp_C: float) -> float:
        return thermal_resistance_air_space(self.bundle_diameter_m, mean_temp_C, self.duct_class)

    def ac_factor(self, r_dc_per_m: float) -> tuple[float, float]:
        """(y_s, y_p) for this conduit's conductors -- zero on a DC circuit,
        where neither effect exists."""
        if not self.is_ac:
            return 0.0, 0.0
        # Single conductors bundled in one duct sit essentially touching, so
        # the centre-to-centre spacing is one insulated conductor's diameter.
        return ac_resistance_factors(
            r_dc_per_m,
            self.conductor_diameter_m,
            self.over_insulation_diameter_m,
            self.frequency_hz,
        )

    def resistance_per_m(self, temp_C: float) -> tuple[float, float, float]:
        """(R_ac, y_s, y_p) [ohm/m] at the given conductor temperature."""
        r_dc = conductor_dc_resistance_per_m(self.area_mm2, temp_C, self.material)
        y_s, y_p = self.ac_factor(r_dc)
        return r_dc * (1.0 + y_s + y_p), y_s, y_p

    @property
    def loss_metric(self) -> float:
        """Total I^2*R at a representative reference temp -- used ONLY to rank
        conduits by heat output when assigning grid positions, never as the
        solved loss (that comes from the outer nonlinear iteration)."""
        r_ref, _, _ = self.resistance_per_m(75.0)
        return self.n_ccc * self.I_design**2 * r_ref


def solve_temperatures(solver, grid, conduits, current_scale=1.0,
                       T_init_C=60.0, max_iter=30, tol_C=0.05):
    """
    Outer nonlinear iteration: conductor AC resistance depends on conductor
    temperature, conductor temperature depends on the FV solve, which
    depends on the heat generated, which depends on resistance. Iterate to
    self-consistency. The FV matrix itself is already factorized once
    inside `solver` -- each outer iteration here is just a cheap
    forward/backward substitution, not a full re-solve.

    Returns (T_conductor: dict[id -> degC], T_field: (ny,nx) array,
             detail: dict[id -> per-conduit breakdown]).
    """
    T_cond = {c.id: T_init_C for c in conduits}
    cells = {c.id: grid.idx(*grid.nearest_cell(c.x, c.y)) for c in conduits}

    detail: dict[str, dict] = {c.id: {} for c in conduits}
    T_field = None
    for _ in range(max_iter):
        Q = np.zeros(solver.n)
        for c in conduits:
            r_ac, y_s, y_p = c.resistance_per_m(T_cond[c.id])
            current = c.I_design * current_scale
            w_conductor = current * current * r_ac
            q_total = c.n_ccc * w_conductor
            Q[cells[c.id]] += q_total
            detail[c.id].update(
                r_ac_ohm_per_m=r_ac, skin_effect_y_s=y_s, proximity_effect_y_p=y_p,
                loss_per_conductor_w_per_m=w_conductor, loss_total_w_per_m=q_total,
                current_a=current,
            )

        T_field = solver.solve(Q)

        max_delta = 0.0
        new_T = {}
        for c in conduits:
            p = cells[c.id]
            j, i = divmod(p, grid.nx)
            q_total = detail[c.id]["loss_total_w_per_m"]
            w_conductor = detail[c.id]["loss_per_conductor_w_per_m"]

            t_wall = corrected_wall_temperature(T_field, grid, solver.k, i, j, q_total, c.r_duct)
            # Mean air-space temperature sits between the duct wall and the
            # conductor; seed it from the previous iteration's conductor temp.
            r_air = c.r_air_space(0.5 * (t_wall + T_cond[c.id]))
            t_conductor = t_wall + w_conductor * c.r_insulation + q_total * (r_air + c.r_duct_wall)

            detail[c.id].update(
                duct_wall_temp_c=float(t_wall), r_air_space_km_per_w=r_air,
                r_insulation_km_per_w=c.r_insulation, r_duct_wall_km_per_w=c.r_duct_wall,
            )
            t_conductor = float(t_conductor)
            max_delta = max(max_delta, abs(t_conductor - T_cond[c.id]))
            new_T[c.id] = t_conductor
        T_cond = new_T
        if max_delta < tol_C:
            break

    for c in conduits:
        detail[c.id]["conductor_temp_c"] = T_cond[c.id]
    return T_cond, T_field, detail


def ampacity_scale_search(solver, grid, conduits, T_target_C,
                          lo=0.0, hi=3.0, iters=22):
    """
    Bisection on a current-scale factor applied uniformly to every conduit's
    I_design, finding the largest scale such that the hottest conductor
    just reaches T_target_C. Monotonic (more current -> hotter), so plain
    bisection is safe and fast.

    A scale of 1.0 means the layout carries exactly the design current at the
    temperature limit; above 1.0 is headroom, below 1.0 means the trench --
    not the conductor's own NEC derating -- is the binding constraint.

    Returns (scale, T_conductor dict at that scale).
    """
    T_hi, _, _ = solve_temperatures(solver, grid, conduits, current_scale=hi)
    tries = 0
    while max(T_hi.values()) < T_target_C and tries < 6:
        hi *= 1.5
        T_hi, _, _ = solve_temperatures(solver, grid, conduits, current_scale=hi)
        tries += 1

    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        T_mid, _, _ = solve_temperatures(solver, grid, conduits, current_scale=mid)
        if max(T_mid.values()) > T_target_C:
            hi = mid
        else:
            lo = mid

    T_final, _, _ = solve_temperatures(solver, grid, conduits, current_scale=lo)
    return lo, T_final
