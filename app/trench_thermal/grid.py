"""Grid generation for the trench thermal FV solver.

Builds a non-uniform tensor-product grid: fine uniform spacing across a
"core" region (where the conduits live), geometrically coarsening out to
the far-field domain boundary. This is a deliberate simplification vs a
fully unstructured/locally-refined mesh -- much simpler to implement and
verify correctly, at the cost of some near-wall accuracy right at each
conduit (which is exactly what the single-conduit validation case in
tests/test_trench_thermal.py is designed to catch and quantify).

DO NOT change the grading scheme (growth rate, core extent, cell aspect
ratio near sources) without re-running the calibration in
tests/test_trench_thermal.py -- solver.EQUIV_SOURCE_RADIUS_RATIO is fit
against this specific construction.
"""

from __future__ import annotations

import numpy as np


def graded_faces(core_min, core_max, domain_min, domain_max, h_fine, growth=1.35):
    """
    Build 1D face coordinates: uniform spacing h_fine across [core_min, core_max],
    geometrically growing spacing out to domain_min and domain_max.

    Returns a sorted 1D numpy array of face coordinates (length = n_cells + 1).
    """
    assert domain_min < core_min < core_max < domain_max, (
        f"require domain_min < core_min < core_max < domain_max, "
        f"got {domain_min}, {core_min}, {core_max}, {domain_max}"
    )

    # Core: uniform fine spacing. Round to a whole number of cells so the
    # core boundaries land exactly on faces.
    n_core = max(1, int(np.ceil((core_max - core_min) / h_fine)))
    core = np.linspace(core_min, core_max, n_core + 1)

    # Left extension: grow outward from core_min down to domain_min.
    left = []
    pos = core_min
    step = h_fine
    while pos > domain_min + 1e-9:
        step *= growth
        pos -= step
        if pos <= domain_min:
            pos = domain_min
        left.append(pos)
    left = list(reversed(left))

    # Right extension: grow outward from core_max up to domain_max.
    right = []
    pos = core_max
    step = h_fine
    while pos < domain_max - 1e-9:
        step *= growth
        pos += step
        if pos >= domain_max:
            pos = domain_max
        right.append(pos)

    faces = np.array(left + list(core) + right)
    faces = np.unique(np.round(faces, 9))  # dedupe any exact repeats, keep sorted
    return faces


class Grid2D:
    """
    Cell-centered 2D tensor grid. x = horizontal (m), y = depth below grade (m, positive down).
    """

    def __init__(self, x_faces, y_faces):
        self.x_faces = np.asarray(x_faces, dtype=float)
        self.y_faces = np.asarray(y_faces, dtype=float)
        self.nx = len(x_faces) - 1
        self.ny = len(y_faces) - 1

        self.dx = np.diff(self.x_faces)          # (nx,)
        self.dy = np.diff(self.y_faces)          # (ny,)
        self.xc = 0.5 * (self.x_faces[:-1] + self.x_faces[1:])   # cell-center x (nx,)
        self.yc = 0.5 * (self.y_faces[:-1] + self.y_faces[1:])   # cell-center y (ny,)

    def n_cells(self):
        return self.nx * self.ny

    def idx(self, i, j):
        """Flatten (i=x-index, j=y-index) -> linear index, row-major over (j,i)."""
        return j * self.nx + i

    def nearest_cell(self, x, y):
        """Index (i, j) of the cell whose center is closest to physical point (x, y)."""
        i = int(np.argmin(np.abs(self.xc - x)))
        j = int(np.argmin(np.abs(self.yc - y)))
        return i, j
