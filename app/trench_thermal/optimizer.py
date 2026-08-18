"""Layer-count + spacing optimizer. Wraps the FV thermal solver in two nested
searches:
  - inner: for a fixed (rows, cols) shape and fixed horizontal spacing,
    bisect the minimum vertical spacing that keeps every conductor at or
    below the target temperature (monotonic -> safe to bisect).
  - middle: sample horizontal spacing across a practical range, run the
    inner search at each, track the (dx, dy) pair with lowest trench
    footprint cost.
  - outer: repeat for each candidate (rows, cols) shape, pick the best
    overall.

Spacing is searched directly on the 1.5"-on-center grid (integer multiples
of INCREMENT), not continuous-then-snap. The raw (unsnapped) minimum is then
recovered by one extra continuous bisection inside the last increment, so a
report can show BOTH numbers -- the true computed minimum and the buildable
snapped value -- the same way the wire sizing comparisons show every option
with the conservative one highlighted.

Every trial layout is a fresh grid + sparse factorization, so evaluations are
memoized by (shape, dx, dy): the bisection revisits the same spacings often,
and the final re-solve of the winning candidate would otherwise repeat work
already done. Remaining lever if this needs to be faster: the shape and
spacing evaluations are independent of each other and parallelize cleanly.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from app.trench_thermal.conduit import solve_temperatures
from app.trench_thermal.grid import Grid2D, graded_faces
from app.trench_thermal.layout import assign_positions, feasible_shapes
from app.trench_thermal.materials import SoilZones
from app.trench_thermal.solver import ThermalSolver

INCH = 0.0254
INCREMENT = 1.5 * INCH   # m, 1.5" on-center snap grid
DEFAULT_MIN_CLEAR_M = 0.0762  # 3 in minimum clear spacing between conduit walls


def build_conduits(specs, assignment, dx, dy, top_depth):
    """Place unpositioned Conduit specs onto the (row, col) grid, centered
    horizontally on x=0. Returns new Conduit objects -- the caller's specs are
    left unpositioned so they can be reused across every trial layout."""
    cols = max(c for _, c in assignment.values()) + 1
    x_center_offset = (cols - 1) * dx / 2.0
    placed = []
    for spec in specs:
        row, col = assignment[spec.id]
        placed.append(dataclasses.replace(
            spec, x=col * dx - x_center_offset, y=top_depth + row * dy,
        ))
    return placed


class TrenchEvaluator:
    """Builds and solves one candidate layout, memoizing by (shape, dx, dy)."""

    def __init__(self, specs, native_rho_cm, backfill_rho_cm, top_depth,
                 backfill_margin, T_air_C, T_deep_C, h_conv, grid_h_fine,
                 backfill_polygon=None):
        self.specs = specs
        self.native_rho_cm = native_rho_cm
        self.backfill_rho_cm = backfill_rho_cm
        self.top_depth = top_depth
        self.backfill_margin = backfill_margin
        self.T_air_C = T_air_C
        self.T_deep_C = T_deep_C
        self.h_conv = h_conv
        self.grid_h_fine = grid_h_fine
        self.backfill_polygon = backfill_polygon
        self._cache: dict[tuple, tuple] = {}
        self.solve_count = 0

    def build(self, assignment, dx, dy):
        """Build the placed conduits, graded grid, soil field and factorized
        solver for one candidate layout. Uncached -- `evaluate` memoizes the
        *result*, and holding factorized solvers for every trial layout would
        cost far more memory than re-building the one winner."""
        conduits = build_conduits(self.specs, assignment, dx, dy, self.top_depth)

        xs = [c.x for c in conduits]
        ys = [c.y for c in conduits]
        r_max = max(c.r_duct for c in conduits)

        env_x_min = min(xs) - r_max - self.backfill_margin
        env_x_max = max(xs) + r_max + self.backfill_margin
        env_y_min = max(0.05, min(ys) - r_max - self.backfill_margin)
        env_y_max = max(ys) + r_max + self.backfill_margin

        x_half_domain = max(4.0, (env_x_max - env_x_min) * 3.0)
        y_max_domain = max(6.0, env_y_max + (env_y_max - env_y_min) * 4.0)

        x_faces = graded_faces(env_x_min - 0.05, env_x_max + 0.05,
                               -x_half_domain, x_half_domain, self.grid_h_fine, growth=1.4)
        y_faces = graded_faces(0.02, env_y_max + 0.05, 0.0, y_max_domain,
                               self.grid_h_fine, growth=1.4)
        grid = Grid2D(x_faces, y_faces)

        zones = SoilZones(self.backfill_rho_cm, self.native_rho_cm,
                          env_x_min, env_x_max, env_y_min, env_y_max,
                          polygon=self.backfill_polygon)
        k_field = zones.k_field(grid)

        solver = ThermalSolver(grid, k_field, self.h_conv, self.T_air_C, self.T_deep_C)
        envelope = dict(x_min=zones.x_min, x_max=zones.x_max,
                        y_min=zones.y_min, y_max=zones.y_max)
        return grid, solver, conduits, envelope

    def evaluate(self, assignment, shape, dx, dy):
        """Returns (max_T, T_cond, envelope, conduits, detail)."""
        key = (shape, round(dx, 7), round(dy, 7))
        if key in self._cache:
            return self._cache[key]

        grid, solver, conduits, envelope = self.build(assignment, dx, dy)
        T_cond, _T_field, detail = solve_temperatures(solver, grid, conduits)
        self.solve_count += 1

        # Plain floats, not numpy scalars: these end up in an API response,
        # and numpy types aren't JSON-serializable.
        T_cond = {cid: float(t) for cid, t in T_cond.items()}
        result = (max(T_cond.values()), T_cond, envelope, conduits, detail)
        self._cache[key] = result
        return result


def _footprint_cost(envelope) -> float:
    return (envelope["x_max"] - envelope["x_min"]) * (envelope["y_max"] - envelope["y_min"])


def _refine_raw_minimum(evaluator, assignment, shape, T_target_C, fixed_dx,
                        infeasible, feasible, axis, tol_m=0.0015):
    """Continuous bisection between a known-infeasible and known-feasible
    spacing, to recover the true computed minimum inside the last 1.5"
    increment. `axis` is "dx" or "dy"; the other dimension is held at
    fixed_dx. tol_m defaults to ~1/16 in, finer than anything a trench gets
    built to."""
    lo, hi = infeasible, feasible
    while hi - lo > tol_m:
        mid = 0.5 * (lo + hi)
        dx, dy = (mid, fixed_dx) if axis == "dx" else (fixed_dx, mid)
        max_T, *_ = evaluator.evaluate(assignment, shape, dx, dy)
        if max_T <= T_target_C:
            hi = mid
        else:
            lo = mid
    return hi


def optimize(specs, native_rho_cm, backfill_rho_cm, T_target_C, top_depth=0.6,
             backfill_margin=0.10, T_air_C=25.0, T_deep_C=20.0, h_conv=15.0,
             grid_h_fine=None, min_clear_m=DEFAULT_MIN_CLEAR_M,
             backfill_polygon=None, max_k_dy_steps=30, verbose=False):
    """
    Search over (rows, cols) shapes and 1.5"-increment spacing to find the
    lowest-footprint feasible layout for the given conduits.

    Returns the winning candidate dict (with `raw_min_spacing_m` alongside the
    snapped `dx`/`dy`) plus a `shapes` list of every shape tried, or None if
    no shape is feasible at any sampled spacing.
    """
    n = len(specs)
    if n == 0:
        return None
    if grid_h_fine is None:
        grid_h_fine = max(c.r_duct for c in specs) / 2.2

    r_max = max(c.r_duct for c in specs)
    min_spacing = 2 * r_max + min_clear_m
    k_min = int(np.ceil(min_spacing / INCREMENT))

    evaluator = TrenchEvaluator(
        specs, native_rho_cm, backfill_rho_cm, top_depth, backfill_margin,
        T_air_C, T_deep_C, h_conv, grid_h_fine, backfill_polygon,
    )

    results: list[dict] = []
    shape_log: list[dict] = []
    for rows, cols in feasible_shapes(n):
        shape = (rows, cols)
        assignment = assign_positions(specs, rows, cols)

        if rows == 1:
            # Single layer: only horizontal spacing to search.
            found = None
            k_dx = k_min
            for _ in range(20):
                dx = k_dx * INCREMENT
                max_T, T_cond, env, conduits, detail = evaluator.evaluate(
                    assignment, shape, dx, INCREMENT)
                if max_T <= T_target_C:
                    found = (k_dx, dx, max_T, T_cond, env, conduits, detail)
                    break
                k_dx += 1
            if found is None:
                shape_log.append(dict(rows=rows, cols=cols, feasible=False))
                if verbose:
                    print(f"shape {rows}x{cols}: infeasible")
                continue

            k_dx, dx, max_T, T_cond, env, conduits, detail = found
            if k_dx == k_min:
                # Feasible at the tightest physically-buildable spacing, so
                # clearance -- not heat -- is what sets it.
                raw_min = min_spacing
                governed_by = "clearance"
            else:
                raw_min = _refine_raw_minimum(
                    evaluator, assignment, shape, T_target_C,
                    fixed_dx=INCREMENT, infeasible=(k_dx - 1) * INCREMENT,
                    feasible=dx, axis="dx")
                governed_by = "thermal"
            candidate = dict(
                rows=rows, cols=cols, k_dx=k_dx, k_dy=None, dx=dx, dy=None,
                max_T=max_T, cost=_footprint_cost(env), T_cond=T_cond, env=env,
                conduits=conduits, detail=detail, raw_min_spacing_m=raw_min,
                spacing_axis="horizontal", spacing_governed_by=governed_by,
                assignment=assignment,
            )
            results.append(candidate)
            shape_log.append(dict(rows=rows, cols=cols, feasible=True, k_dx=k_dx,
                                  k_dy=None, cost=candidate["cost"], max_T=float(max_T)))
            if verbose:
                print(f"shape {rows}x{cols}: k_dx={k_dx}")
            continue

        # rows > 1: sample k_dx, bisect min feasible k_dy at each sample.
        k_dx_samples = sorted(set(
            int(round(k_min * m)) for m in (1.0, 1.15, 1.35, 1.6, 2.0, 2.5, 3.2)
        ))
        best_for_shape = None
        for k_dx in k_dx_samples:
            dx = k_dx * INCREMENT
            k_dy_lo, k_dy_hi = k_min - 1, k_min + max_k_dy_steps

            def feasible(k_dy, _dx=dx):
                max_T, *_ = evaluator.evaluate(assignment, shape, _dx, k_dy * INCREMENT)
                return max_T <= T_target_C

            if not feasible(k_dy_hi):
                continue  # even max vertical spacing tried doesn't help enough at this dx
            lo, hi = k_dy_lo, k_dy_hi
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if feasible(mid):
                    hi = mid
                else:
                    lo = mid
            k_dy = hi
            dy = k_dy * INCREMENT
            max_T, T_cond, env, conduits, detail = evaluator.evaluate(assignment, shape, dx, dy)
            candidate = dict(
                rows=rows, cols=cols, k_dx=k_dx, k_dy=k_dy, dx=dx, dy=dy,
                max_T=max_T, cost=_footprint_cost(env), T_cond=T_cond, env=env,
                conduits=conduits, detail=detail, assignment=assignment,
                spacing_axis="vertical",
            )
            if best_for_shape is None or candidate["cost"] < best_for_shape["cost"]:
                best_for_shape = candidate

        if best_for_shape is None:
            shape_log.append(dict(rows=rows, cols=cols, feasible=False))
            if verbose:
                print(f"shape {rows}x{cols}: infeasible across sampled spacings")
            continue

        if best_for_shape["k_dy"] <= k_min:
            raw_min = min_spacing
            governed_by = "clearance"
        else:
            raw_min = _refine_raw_minimum(
                evaluator, assign_positions(specs, best_for_shape["rows"], best_for_shape["cols"]),
                (best_for_shape["rows"], best_for_shape["cols"]), T_target_C,
                fixed_dx=best_for_shape["dx"],
                infeasible=(best_for_shape["k_dy"] - 1) * INCREMENT,
                feasible=best_for_shape["dy"], axis="dy")
            governed_by = "thermal"
        best_for_shape["raw_min_spacing_m"] = raw_min
        best_for_shape["spacing_governed_by"] = governed_by

        results.append(best_for_shape)
        shape_log.append(dict(rows=rows, cols=cols, feasible=True,
                              k_dx=best_for_shape["k_dx"], k_dy=best_for_shape["k_dy"],
                              cost=best_for_shape["cost"], max_T=float(best_for_shape["max_T"])))
        if verbose:
            print(f"shape {rows}x{cols}: k_dx={best_for_shape['k_dx']} "
                  f"k_dy={best_for_shape['k_dy']}  cost={best_for_shape['cost']:.3f} m^2")

    if not results:
        return None

    best = min(results, key=lambda r: r["cost"])
    best["shapes"] = shape_log
    best["solve_count"] = evaluator.solve_count
    best["min_clear_spacing_m"] = min_spacing
    best["evaluator"] = evaluator
    return best
