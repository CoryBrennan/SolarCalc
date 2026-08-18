"""Physics-level regression tests for the trench thermal solver
(app/trench_thermal/), independent of this app's data model.

The headline test is `test_isolated_conduit_matches_kennelly_closed_form`:
an isolated buried conduit in homogeneous soil is the one case where a closed
form (Kennelly) is exactly right, so it isolates pure grid/discretization
error from everything else. Run it after ANY change to grid construction or
solver assembly -- solver.EQUIV_SOURCE_RADIUS_RATIO is empirically calibrated
against this specific grid, and without that correction the error here is
~42%, not ~0.4%.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.sparse import lil_matrix

from app.trench_thermal.conduit import Conduit, ampacity_scale_search, solve_temperatures
from app.trench_thermal.grid import Grid2D, graded_faces
from app.trench_thermal.materials import (
    SoilZones,
    ac_resistance_factors,
    conductor_dc_resistance_per_m,
    k_from_rho_cm,
    thermal_resistance_air_space,
    thermal_resistance_duct_wall,
    thermal_resistance_insulation,
)
from app.trench_thermal.optimizer import INCREMENT, optimize
from app.trench_thermal.render import render_cross_section
from app.trench_thermal.solver import ThermalSolver, corrected_wall_temperature

RHO_CM = 90.0
RHO_M = RHO_CM * 0.01
K = 1.0 / RHO_M
R_DUCT = 0.03
Q_TEST = 20.0
T_AMBIENT = 20.0


def kennelly_r_ext(rho_m: float, depth_m: float, radius_m: float) -> float:
    """Closed-form external thermal resistance of an isolated buried cylinder."""
    u = 2.0 * depth_m / radius_m
    return rho_m / (2.0 * math.pi) * math.log(u + math.sqrt(u * u - 1.0))


def _isolated_conduit_error(depth_m: float, h_fine: float = R_DUCT / 3.0) -> float:
    x_faces = graded_faces(-0.6, 0.6, -6.0, 6.0, h_fine, growth=1.3)
    # Fine resolution must extend up to the grade surface, not just around the
    # conduit -- Kennelly assumes an exactly isothermal surface, and a coarse
    # near-surface cell silently adds spurious resistance to that boundary.
    y_faces = graded_faces(0.02, depth_m + 0.6, 0.0, 12.0, h_fine, growth=1.3)
    grid = Grid2D(x_faces, y_faces)
    k_field = np.full((grid.ny, grid.nx), K)

    # Very large h approximates the isothermal (Dirichlet) ground surface the
    # closed form assumes.
    solver = ThermalSolver(grid, k_field, h_conv=1.0e6, T_air_C=T_AMBIENT, T_deep_C=T_AMBIENT)
    i, j = grid.nearest_cell(0.0, depth_m)
    q = np.zeros(solver.n)
    q[grid.idx(i, j)] = Q_TEST
    field = solver.solve(q)

    dt_numeric = corrected_wall_temperature(field, grid, k_field, i, j, Q_TEST, R_DUCT) - T_AMBIENT
    dt_analytic = Q_TEST * kennelly_r_ext(RHO_M, depth_m, R_DUCT)
    return abs(dt_numeric - dt_analytic) / dt_analytic


@pytest.mark.parametrize("depth_m", [0.3, 0.6, 0.9, 1.5, 2.5])
def test_isolated_conduit_matches_kennelly_closed_form(depth_m):
    """<2% against the closed form across a realistic burial-depth range."""
    assert _isolated_conduit_error(depth_m) < 0.02


@pytest.mark.parametrize("h_fine", [R_DUCT / 1.5, R_DUCT / 3.0, R_DUCT / 6.0])
def test_source_radius_correction_is_grid_resolution_independent(h_fine):
    """The whole point of the equivalent-source-radius correction: a raw point
    source on a finite grid does NOT converge to the physical duct-wall
    temperature as the grid refines (a 2D point source has a logarithmic
    singularity), so the correction must hold across grid resolutions rather
    than being tuned to one."""
    assert _isolated_conduit_error(0.9, h_fine=h_fine) < 0.02


def test_uncorrected_point_source_is_badly_wrong():
    """Guards against anyone 'simplifying away' the correction: without it the
    same case is off by tens of percent, not a fraction of one."""
    depth_m = 0.9
    x_faces = graded_faces(-0.6, 0.6, -6.0, 6.0, R_DUCT / 3.0, growth=1.3)
    y_faces = graded_faces(0.02, depth_m + 0.6, 0.0, 12.0, R_DUCT / 3.0, growth=1.3)
    grid = Grid2D(x_faces, y_faces)
    k_field = np.full((grid.ny, grid.nx), K)
    solver = ThermalSolver(grid, k_field, h_conv=1.0e6, T_air_C=T_AMBIENT, T_deep_C=T_AMBIENT)
    i, j = grid.nearest_cell(0.0, depth_m)
    q = np.zeros(solver.n)
    q[grid.idx(i, j)] = Q_TEST
    field = solver.solve(q)

    dt_raw = field[j, i] - T_AMBIENT
    dt_analytic = Q_TEST * kennelly_r_ext(RHO_M, depth_m, R_DUCT)
    assert abs(dt_raw - dt_analytic) / dt_analytic > 0.2


def _reference_assemble(grid, k, h, t_air, t_deep):
    """Direct cell-by-cell assembly -- the readable statement of the same
    matrix solver._assemble() builds array-at-a-time for speed."""
    nx, ny = grid.nx, grid.ny
    a = lil_matrix((nx * ny, nx * ny))
    b = np.zeros(nx * ny)
    harmonic = lambda k1, k2: 2.0 * k1 * k2 / (k1 + k2)  # noqa: E731

    for j in range(ny):
        for i in range(nx):
            p = grid.idx(i, j)
            k_p, dx_p, dy_p = k[j, i], grid.dx[i], grid.dy[j]
            diag = 0.0
            for di, dj, face, half in ((-1, 0, dy_p, dx_p), (1, 0, dy_p, dx_p),
                                       (0, -1, dx_p, dy_p), (0, 1, dx_p, dy_p)):
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < ny:
                    span = grid.dx[ii] if di else grid.dy[jj]
                    cond = harmonic(k_p, k[jj, ii]) * face / (0.5 * half + 0.5 * span)
                    a[p, grid.idx(ii, jj)] = -cond
                elif (di, dj) == (0, -1):   # grade surface: Robin
                    cond = dx_p / ((0.5 * dy_p) / k_p + 1.0 / h)
                    b[p] += cond * t_air
                else:                       # far field: Dirichlet
                    cond = k_p * face / (0.5 * half)
                    b[p] += cond * t_deep
                diag += cond
            a[p, p] = diag
    return a.tocsc(), b


def test_vectorized_assembly_matches_cell_by_cell_reference():
    """An assembly bug would be invisible in the physics but wrong everywhere,
    so the fast path is pinned to the obvious slow one -- on a heterogeneous
    conductivity field, so the harmonic-mean face terms actually vary."""
    grid = Grid2D(graded_faces(-0.5, 0.5, -3.0, 3.0, 0.06, growth=1.4),
                  graded_faces(0.02, 1.2, 0.0, 5.0, 0.06, growth=1.4))
    k = 0.8 + np.random.default_rng(0).random((grid.ny, grid.nx))

    solver = ThermalSolver(grid, k, h_conv=15.0, T_air_C=25.0, T_deep_C=20.0)
    a_ref, b_ref = _reference_assemble(grid, k, 15.0, 25.0, 20.0)

    assert abs(solver.A - a_ref).max() < 1e-9
    assert np.abs(solver.b_fixed - b_ref).max() < 1e-9


def test_backfill_polygon_and_rectangle_agree_when_polygon_is_that_rectangle():
    """Irregular-envelope support must not perturb the ordinary rectangular
    case -- a polygon tracing the rectangle has to give the same k field."""
    grid = Grid2D(graded_faces(-0.5, 0.5, -3.0, 3.0, 0.05, growth=1.4),
                  graded_faces(0.02, 1.2, 0.0, 5.0, 0.05, growth=1.4))
    rect = SoilZones(60.0, 90.0, -0.3, 0.3, 0.5, 0.9)
    poly = SoilZones(60.0, 90.0,
                     polygon=[(-0.3, 0.5), (0.3, 0.5), (0.3, 0.9), (-0.3, 0.9)])
    assert np.array_equal(rect.k_field(grid), poly.k_field(grid))


def test_polygon_envelope_excludes_points_outside_it():
    """A notched (non-convex) envelope: the notch must read as native soil."""
    zones = SoilZones(60.0, 90.0, polygon=[
        (-0.4, 0.4), (0.4, 0.4), (0.4, 1.0), (0.1, 1.0), (0.1, 0.6), (-0.1, 0.6),
        (-0.1, 1.0), (-0.4, 1.0),
    ])
    assert zones.contains(-0.3, 0.9)          # inside a leg
    assert not zones.contains(0.0, 0.9)       # inside the notch -> native soil
    assert zones.k_at(0.0, 0.9) == pytest.approx(k_from_rho_cm(90.0))
    assert zones.k_at(-0.3, 0.9) == pytest.approx(k_from_rho_cm(60.0))


def test_backfill_improves_conductor_temperature():
    """Sanity direction check: better (lower-resistivity) backfill must run the
    conductors cooler, never hotter."""
    def solve(backfill_rho):
        result = optimize([_demo_conduit("C1"), _demo_conduit("C2")],
                          native_rho_cm=120.0, backfill_rho_cm=backfill_rho,
                          T_target_C=90.0, verbose=False)
        return result["max_T"]

    assert solve(50.0) < solve(110.0)


def _demo_conduit(cid: str, current: float = 150.0) -> Conduit:
    return Conduit(
        id=cid, I_design=current, n_ccc=3, area_mm2=127.0, material="CU", is_ac=True,
        conductor_diameter_m=0.0132, over_insulation_diameter_m=0.0165,
        bundle_diameter_m=0.0356, duct_inner_diameter_m=0.0520,
        duct_outer_diameter_m=0.0603, rho_insulation_km_per_w=5.0,
        rho_duct_km_per_w=6.0, duct_class="nonmetallic",
    )


def test_more_conductors_in_a_conduit_run_hotter():
    """n_ccc must actually drive heat: the prototype modelled one conductor per
    conduit, and a 3-CCC feeder puts three conductors' losses into the soil."""
    one = optimize([_demo_conduit("C1")], 90.0, 60.0, 90.0, verbose=False)
    three = _demo_conduit("C1")
    three.n_ccc = 9
    hot = optimize([three], 90.0, 60.0, 90.0, verbose=False)
    assert hot["max_T"] > one["max_T"]


def test_optimizer_reports_raw_and_snapped_spacing():
    """Both numbers, always -- the true computed minimum and the buildable
    1.5"-on-centre value it snaps up to."""
    result = optimize([_demo_conduit(f"C{i}") for i in range(3)],
                      90.0, 60.0, 90.0, verbose=False)
    assert result is not None
    raw = result["raw_min_spacing_m"]
    snapped = result["dy"] if result["spacing_axis"] == "vertical" else result["dx"]
    # The snapped value is on the increment grid, is buildable, and is never
    # tighter than the raw minimum (i.e. snapping is always conservative).
    assert snapped == pytest.approx(round(snapped / INCREMENT) * INCREMENT)
    assert snapped >= raw - 1e-9
    assert snapped - raw < INCREMENT


def test_evaluations_are_memoized():
    """The bisection revisits the same spacings repeatedly; each miss is a
    fresh grid + sparse factorization, so the cache is load-bearing."""
    specs = [_demo_conduit(f"C{i}") for i in range(3)]
    result = optimize(specs, 90.0, 60.0, 90.0, verbose=False)
    evaluator = result["evaluator"]
    assert evaluator.solve_count == len(evaluator._cache)

    before = evaluator.solve_count
    key = next(iter(evaluator._cache))
    evaluator.evaluate({c.id: (0, i) for i, c in enumerate(specs)}, key[0], key[1], key[2])
    assert evaluator.solve_count == before  # served from cache, no new solve


def test_ampacity_scale_search_brackets_the_target():
    specs = [_demo_conduit(f"C{i}") for i in range(2)]
    result = optimize(specs, 90.0, 60.0, 90.0, verbose=False)
    evaluator = result["evaluator"]
    grid, solver, placed, _env = evaluator.build(result["assignment"], result["dx"],
                                                 result["dy"] or INCREMENT)
    scale, temps = ampacity_scale_search(solver, grid, placed, 90.0)
    assert 0.5 < scale < 3.0
    assert max(temps.values()) <= 90.0 + 0.5

    hotter, _, _ = solve_temperatures(solver, grid, placed, current_scale=scale * 1.2)
    assert max(hotter.values()) > 90.0


# ---------------------------------------------------------------------------
# Closed-form internal-resistance chain and AC effects
# ---------------------------------------------------------------------------
def test_insulation_resistance_grows_with_wall_thickness():
    thin = thermal_resistance_insulation(3.5, 0.020, 0.022)
    thick = thermal_resistance_insulation(3.5, 0.020, 0.026)
    assert 0 < thin < thick


def test_air_space_resistance_falls_with_bundle_diameter_and_temperature():
    """IEC 60287-2-1 T'_4 = U / (1 + 0.1(V + Y*theta_m)*D_e): both a bigger
    bundle and a hotter air space reduce it."""
    base = thermal_resistance_air_space(0.035, 60.0)
    assert thermal_resistance_air_space(0.055, 60.0) < base
    assert thermal_resistance_air_space(0.035, 90.0) < base


def test_steel_conduit_wall_resistance_is_negligible_vs_pvc():
    pvc = thermal_resistance_duct_wall(6.0, 0.052, 0.060)
    steel = thermal_resistance_duct_wall(0.022, 0.052, 0.060)
    assert steel < pvc / 100


def test_skin_effect_grows_with_conductor_size():
    """AC/DC ratio is genuinely negligible for small PV feeders and several
    percent at 750 kcmil -- which is exactly why it isn't assumed away."""
    r_small = conductor_dc_resistance_per_m(53.5, 90.0)    # ~1/0 AWG
    r_large = conductor_dc_resistance_per_m(380.0, 90.0)   # ~750 kcmil
    ys_small, _ = ac_resistance_factors(r_small, 0.0093, 0.0125)
    ys_large, _ = ac_resistance_factors(r_large, 0.0250, 0.0290)
    assert ys_small < 0.005
    assert 0.01 < ys_large < 0.10
    assert ys_large > ys_small


def test_aluminum_has_higher_resistance_than_copper_at_equal_area():
    cu = conductor_dc_resistance_per_m(253.0, 75.0, "CU")
    al = conductor_dc_resistance_per_m(253.0, 75.0, "AL")
    assert al > cu * 1.5


def test_resistance_rises_with_temperature():
    assert (conductor_dc_resistance_per_m(127.0, 90.0)
            > conductor_dc_resistance_per_m(127.0, 25.0))


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------
def test_renderer_flags_a_failing_trench():
    """A trench over the conductor limit must not render as if it passed: the
    colour scale spans the solved temperatures, and the overage is stated."""
    result = optimize([_demo_conduit(f"C{i}") for i in range(2)], 90.0, 60.0, 90.0, verbose=False)
    conduits = result["conduits"]
    hot = {c.id: 130.0 for c in conduits}
    svg = render_cross_section(conduits, hot, result["env"], 90.0, result["dx"], result["dy"])
    assert "EXCEEDS 90&#176;C CONDUCTOR LIMIT" in svg
    assert "130&#176;C (hottest conduit)" in svg


def test_renderer_scales_each_conduit_to_its_own_diameter():
    """A schedule can mix trade sizes; drawing them all at the first one's
    radius would misrepresent the section."""
    small, large = _demo_conduit("SMALL"), _demo_conduit("LARGE")
    large.duct_outer_diameter_m = 0.114
    result = optimize([small, large], 90.0, 60.0, 90.0, verbose=False)
    radii = {float(part.split('r="')[1].split('"')[0])
             for part in render_cross_section(result["conduits"], result["T_cond"], result["env"],
                                              90.0, result["dx"], result["dy"]).split("<circle")[1:]}
    assert len(radii) == 2
