"""Conduit-to-position assignment and candidate (rows x cols) shape enumeration."""

from __future__ import annotations


def feasible_shapes(n: int) -> list[tuple[int, int]]:
    """
    Candidate (rows, cols) layer/column splits for n conduits, favoring
    near-even splits. Includes true factor pairs plus padded near-factors
    (n doesn't need to divide evenly -- a shape can have unused slots,
    handled by the caller as gaps in the grid).
    """
    shapes = set()
    for rows in range(1, n + 1):
        cols = -(-n // rows)  # ceil division: enough columns to fit n in `rows` rows
        if rows * cols >= n:
            shapes.add((rows, cols))
    return sorted(shapes, key=lambda rc: (rc[0], rc[1]))


def assign_positions(conduits, rows: int, cols: int) -> dict[str, tuple[int, int]]:
    """
    Assign each conduit to a (row, col) grid slot, placing the highest-loss
    (I^2R) conduits at the most exposed positions (corners, then edges,
    working inward) and the lowest-loss conduits in the interior.

    Returns dict: conduit.id -> (row, col)
    """
    positions = [(r, c) for r in range(rows) for c in range(cols)]
    if len(positions) < len(conduits):
        raise ValueError(f"grid ({rows}x{cols}={rows*cols}) too small for {len(conduits)} conduits")

    def exposure_score(rc):
        # Lower score = more exposed (closer to an edge/corner) = cooler position.
        r, c = rc
        return min(r, rows - 1 - r) + min(c, cols - 1 - c)

    positions_sorted = sorted(positions, key=exposure_score)
    # Ties in loss_metric are broken by id so the assignment is deterministic
    # across runs -- a report that reshuffles identical conduits between runs
    # looks like a design change when nothing changed.
    conduits_sorted = sorted(conduits, key=lambda cd: (-cd.loss_metric, cd.id))

    return {cd.id: pos for cd, pos in zip(conduits_sorted, positions_sorted)}
