"""Trench / duct-bank thermal design — numerical (finite-volume) steady-state
heat conduction for direct-buried conduits, plus a layer-count/spacing
optimizer and a cross-section renderer.

This package is deliberately app-agnostic: pure geometry, soil physics, and
solver code with no imports from the rest of `app`. Everything that knows
about this project's data model (raceway runs, NEC conductor tables,
insulation types, conduit dimensions) lives in `app.trench_calc`, which is
the single seam between the two.

Why numerical rather than the closed-form Neher-McGrath/Kennelly formula:
the closed form only covers idealized homogeneous soil, and the case this
was built for is an engineered-backfill envelope of one thermal resistivity
embedded in native soil of another. `solver.ThermalSolver` doesn't care what
shape that envelope is -- `materials.SoilZones` supports both a rectangle
and an arbitrary polygon.

Accuracy: `tests/test_trench_thermal.py` regression-checks the solver against
the closed-form Kennelly result for an isolated buried conduit (the one case
where a closed form is exactly right), across a 0.3-2.5 m burial-depth range.
See `solver.EQUIV_SOURCE_RADIUS_RATIO` before changing anything about grid
construction.
"""
