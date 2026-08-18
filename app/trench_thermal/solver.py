"""2D steady-state finite-volume conduction solver for a trench cross-section.

Domain: x = horizontal (m), y = depth below grade (m, positive down).
Boundaries: top = Robin (convective, to ambient air), left/right/bottom = Dirichlet
(far-field deep-earth ambient).

The conductance matrix A depends only on grid geometry and soil conductivity
(both fixed once the layout is chosen) -- so it is assembled and factorized
ONCE, then reused across every outer nonlinear iteration and every trial
layout in the spacing/layer optimizer. Only the source vector Q changes.
"""

from __future__ import annotations

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu


class ThermalSolver:
    def __init__(self, grid, k_field, h_conv, T_air_C, T_deep_C):
        """
        grid      : grid.Grid2D
        k_field   : (ny, nx) array of thermal conductivity [W/(m*K)] per cell
        h_conv    : combined surface heat-transfer coefficient [W/(m^2*K)] at grade
        T_air_C   : ambient air temp at grade [degC]
        T_deep_C  : far-field deep-earth ambient temp [degC]
        """
        self.grid = grid
        self.k = k_field
        self.h = h_conv
        self.T_air = T_air_C
        self.T_deep = T_deep_C
        self._assemble()

    @staticmethod
    def _harmonic(k1, k2):
        return 2.0 * k1 * k2 / (k1 + k2)

    def _assemble(self):
        """Assemble the conductance matrix.

        Written array-at-a-time rather than cell-by-cell: the optimizer builds
        a fresh grid and matrix for every trial layout, and a Python loop over
        ~25k cells inserting into a lil_matrix dominated the whole calc's
        runtime (seconds per layout, minutes per search). The assembled matrix
        is identical either way -- tests/test_trench_thermal.py asserts that
        against a direct cell-by-cell reference implementation, since an
        assembly bug here would be invisible in the physics but wrong
        everywhere.

        Face conductances are symmetric: the conductance from cell (j,i) east
        to (j,i+1) is the same number as from (j,i+1) west to (j,i), because
        the harmonic face conductivity and the centre-to-centre distance are
        shared and the face length (dy for a vertical face, dx for a
        horizontal one) doesn't vary along the other axis of a tensor grid.
        So each internal face is computed once and applied to both cells.
        """
        g = self.grid
        nx, ny = g.nx, g.ny
        n = nx * ny
        k, dx, dy = self.k, g.dx, g.dy

        cell_index = np.arange(n).reshape(ny, nx)
        diag = np.zeros((ny, nx))
        b_fixed = np.zeros((ny, nx))
        rows, cols, vals = [], [], []

        def add_face(cond, index_a, index_b):
            rows.append(index_a.ravel())
            cols.append(index_b.ravel())
            vals.append(-cond.ravel())
            rows.append(index_b.ravel())
            cols.append(index_a.ravel())
            vals.append(-cond.ravel())

        # --- internal vertical faces (between columns i and i+1) ---
        if nx > 1:
            k_face = self._harmonic(k[:, :-1], k[:, 1:])
            dist = 0.5 * (dx[:-1] + dx[1:])
            cond = k_face * dy[:, None] / dist[None, :]
            add_face(cond, cell_index[:, :-1], cell_index[:, 1:])
            diag[:, :-1] += cond
            diag[:, 1:] += cond

        # --- internal horizontal faces (between rows j and j+1) ---
        if ny > 1:
            k_face = self._harmonic(k[:-1, :], k[1:, :])
            dist = 0.5 * (dy[:-1] + dy[1:])
            cond = k_face * dx[None, :] / dist[:, None]
            add_face(cond, cell_index[:-1, :], cell_index[1:, :])
            diag[:-1, :] += cond
            diag[1:, :] += cond

        # --- Dirichlet far-field: left, right, bottom ---
        cond_w = k[:, 0] * dy / (0.5 * dx[0])
        diag[:, 0] += cond_w
        b_fixed[:, 0] += cond_w * self.T_deep

        cond_e = k[:, -1] * dy / (0.5 * dx[-1])
        diag[:, -1] += cond_e
        b_fixed[:, -1] += cond_e * self.T_deep

        cond_s = k[-1, :] * dx / (0.5 * dy[-1])
        diag[-1, :] += cond_s
        b_fixed[-1, :] += cond_s * self.T_deep

        # --- Robin (convective) top boundary at grade: series conduction
        # (half-cell) + convection resistance, per unit face length dx. ---
        cond_n = dx / ((0.5 * dy[0]) / k[0, :] + 1.0 / self.h)
        diag[0, :] += cond_n
        b_fixed[0, :] += cond_n * self.T_air

        rows.append(cell_index.ravel())
        cols.append(cell_index.ravel())
        vals.append(diag.ravel())

        self.n = n
        self.A = coo_matrix(
            (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
            shape=(n, n),
        ).tocsc()
        self.b_fixed = b_fixed.ravel()
        self._lu = splu(self.A)   # factorize ONCE; reuse for every solve

    def solve(self, Q):
        """
        Q: (n,) source vector [W/m], nonzero at conduit cell indices.
        Returns T: (ny, nx) temperature field [degC].
        """
        rhs = self.b_fixed + Q
        T_flat = self._lu.solve(rhs)
        return T_flat.reshape(self.grid.ny, self.grid.nx)


# Equivalent source-radius ratio for a point heat source injected at a grid
# cell of local size h: r_eq = EQUIV_SOURCE_RADIUS_RATIO * h. A point source
# on a finite grid runs numerically hotter than a real finite-radius duct
# (the raw source-cell value corresponds to a *smaller* effective radius
# than the true duct wall), so the correction below is always a downward
# adjustment for realistic conduit sizes. This is the heat-conduction analog
# of the classical Peaceman equivalent well-block radius used in reservoir
# simulation for point sources/sinks on a discretized grid.
#
# Calibrated against the closed-form Kennelly comparison in
# tests/test_trench_thermal.py: stable at ~0.40-0.43 across an 8x range of
# grid resolution and a 4.5x range of burial depth. Re-run that test if the
# grid construction (graded_faces growth rate, cell aspect ratio near
# sources) changes materially. Removing this correction does not "simplify"
# anything -- it takes the isolated-conduit error from ~0.2% to ~42%.
EQUIV_SOURCE_RADIUS_RATIO = 0.41


def corrected_wall_temperature(T_field, grid, k_field, i, j, Q_i, r_duct,
                               radius_ratio=EQUIV_SOURCE_RADIUS_RATIO):
    """
    Convert the raw FV solution at a source cell into a physically meaningful
    duct-wall temperature, correcting for the point-source discretization
    artifact. i, j are the cell indices where the conduit's source was
    injected; Q_i [W/m] is that conduit's heat generation; r_duct [m] is the
    conduit's actual outer radius.
    """
    h_local = np.sqrt(grid.dx[i] * grid.dy[j])
    k_local = k_field[j, i]
    correction = (Q_i / (2.0 * np.pi * k_local)) * np.log(radius_ratio * h_local / r_duct)
    return T_field[j, i] + correction
