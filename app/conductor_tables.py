"""Shared NEC conductor data and lookups.

Ported from the HMI draft's client-side JS, where this table was duplicated
across the Ampacity, Bonding, and Voltage Drop panels. Here it's one source
of truth every calc module imports from.
"""

from __future__ import annotations

# Ordered smallest to largest — several lookups below rely on this order.
CONDUCTOR_ORDER: list[str] = [
    "10 AWG", "8 AWG", "6 AWG", "4 AWG", "3 AWG", "2 AWG", "1 AWG", "1/0 AWG",
    "2/0 AWG", "3/0 AWG", "4/0 AWG", "250 kcmil", "300 kcmil", "350 kcmil",
    "400 kcmil", "500 kcmil", "600 kcmil", "700 kcmil", "750 kcmil",
]

# NEC Table 310.16, copper, 75°C and 90°C columns (common sizes subset).
AMPACITY_TABLE_CU: dict[str, dict[str, int]] = {
    "10 AWG": {"c75": 35, "c90": 40},
    "8 AWG": {"c75": 50, "c90": 55},
    "6 AWG": {"c75": 65, "c90": 75},
    "4 AWG": {"c75": 85, "c90": 95},
    "3 AWG": {"c75": 100, "c90": 110},
    "2 AWG": {"c75": 115, "c90": 130},
    "1 AWG": {"c75": 130, "c90": 150},
    "1/0 AWG": {"c75": 150, "c90": 170},
    "2/0 AWG": {"c75": 175, "c90": 195},
    "3/0 AWG": {"c75": 200, "c90": 225},
    "4/0 AWG": {"c75": 230, "c90": 260},
    "250 kcmil": {"c75": 255, "c90": 290},
    "300 kcmil": {"c75": 285, "c90": 320},
    "350 kcmil": {"c75": 310, "c90": 350},
    "400 kcmil": {"c75": 335, "c90": 380},
    "500 kcmil": {"c75": 380, "c90": 430},
    "600 kcmil": {"c75": 420, "c90": 475},
    "700 kcmil": {"c75": 460, "c90": 520},
    "750 kcmil": {"c75": 475, "c90": 535},
}

# NEC Table 310.16, aluminum/copper-clad aluminum, 75°C and 90°C columns
# (same sizes subset as the copper table above). Used by the value-engineering
# cost comparison (app/wire_cost_calc.py) to size an all-aluminum candidate
# feeder against the same required ampacity as the copper one.
AMPACITY_TABLE_AL: dict[str, dict[str, int]] = {
    "10 AWG": {"c75": 30, "c90": 35},
    "8 AWG": {"c75": 40, "c90": 45},
    "6 AWG": {"c75": 50, "c90": 55},
    "4 AWG": {"c75": 65, "c90": 75},
    "3 AWG": {"c75": 75, "c90": 85},
    "2 AWG": {"c75": 90, "c90": 100},
    "1 AWG": {"c75": 100, "c90": 115},
    "1/0 AWG": {"c75": 120, "c90": 135},
    "2/0 AWG": {"c75": 135, "c90": 150},
    "3/0 AWG": {"c75": 155, "c90": 175},
    "4/0 AWG": {"c75": 180, "c90": 205},
    "250 kcmil": {"c75": 205, "c90": 230},
    "300 kcmil": {"c75": 230, "c90": 260},
    "350 kcmil": {"c75": 250, "c90": 280},
    "400 kcmil": {"c75": 270, "c90": 305},
    "500 kcmil": {"c75": 310, "c90": 350},
    "600 kcmil": {"c75": 340, "c90": 385},
    "700 kcmil": {"c75": 375, "c90": 420},
    "750 kcmil": {"c75": 385, "c90": 435},
}

AMPACITY_TABLES: dict[str, dict[str, dict[str, int]]] = {"CU": AMPACITY_TABLE_CU, "AL": AMPACITY_TABLE_AL}

# Kept as the pre-existing name — every current call site (ampacity_calc,
# raceway_calc, cable_routing_calc, bonding_calc) reads copper ampacities
# through this name, so it stays an alias rather than a second source of truth.
AMPACITY_TABLE = AMPACITY_TABLE_CU

# Circular mils per conductor size, for voltage-drop calculations.
CIRCULAR_MILS: dict[str, int] = {
    "10 AWG": 10380, "8 AWG": 16510, "6 AWG": 26240, "4 AWG": 41740, "3 AWG": 52620,
    "2 AWG": 66360, "1 AWG": 83690, "1/0 AWG": 105600, "2/0 AWG": 133100, "3/0 AWG": 167800,
    "4/0 AWG": 211600, "250 kcmil": 250000, "300 kcmil": 300000, "350 kcmil": 350000,
    "400 kcmil": 400000, "500 kcmil": 500000, "600 kcmil": 600000, "700 kcmil": 700000,
    "750 kcmil": 750000,
}

# NEC Table 250.66 (also serves Table 250.102(C)(1) for system bonding jumper
# sizing per 250.28(D)(1)) — grounding/bonding conductor size as a function of
# the largest ungrounded conductor, expressed as ordered (breakpoint, size) pairs.
GROUNDING_CONDUCTOR_BREAKPOINTS: list[tuple[str, str]] = [
    ("2 AWG", "8 AWG"),
    ("1/0 AWG", "6 AWG"),
    ("3/0 AWG", "4 AWG"),
    ("350 kcmil", "2 AWG"),
    ("600 kcmil", "1/0 AWG"),
    ("750 kcmil", "2/0 AWG"),
]

# NEC 240.6(A) standard overcurrent device ratings.
STANDARD_OCPD_SIZES: list[int] = [
    15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 110, 125, 150, 175,
    200, 225, 250, 300, 350, 400, 450, 500, 600, 700, 800, 1000, 1200,
]

# NEC Table 250.122 — minimum equipment grounding conductor (EGC) size as a
# function of the OCPD rating protecting the circuit (NOT the ungrounded
# conductor size — that's Table 250.66 / GROUNDING_CONDUCTOR_BREAKPOINTS
# above, used for the grounding electrode conductor / bonding jumper, a
# different conductor with a different sizing rule). Ordered ascending by
# OCPD breakpoint; capped to STANDARD_OCPD_SIZES' 1200 A top end and
# CONDUCTOR_ORDER's 750 kcmil top end, same common-sizes-subset posture as
# the rest of this file — verify against the current NEC edition before issue.
EGC_BREAKPOINTS_CU: list[tuple[int, str]] = [
    (15, "10 AWG"), (20, "10 AWG"), (30, "10 AWG"), (40, "10 AWG"), (60, "10 AWG"),
    (100, "8 AWG"), (200, "6 AWG"), (300, "4 AWG"), (400, "3 AWG"), (500, "2 AWG"),
    (600, "1 AWG"), (800, "1/0 AWG"), (1000, "2/0 AWG"), (1200, "3/0 AWG"),
]
EGC_BREAKPOINTS_AL: list[tuple[int, str]] = [
    (15, "8 AWG"), (20, "8 AWG"), (30, "8 AWG"), (40, "8 AWG"), (60, "8 AWG"),
    (100, "6 AWG"), (200, "4 AWG"), (300, "2 AWG"), (400, "1 AWG"), (500, "1/0 AWG"),
    (600, "2/0 AWG"), (800, "3/0 AWG"), (1000, "4/0 AWG"), (1200, "250 kcmil"),
]
EGC_BREAKPOINTS: dict[str, list[tuple[int, str]]] = {"CU": EGC_BREAKPOINTS_CU, "AL": EGC_BREAKPOINTS_AL}


def select_conductor(required_ampacity: float, insulation_rating: int, material: str = "CU") -> str | None:
    """Smallest conductor whose ampacity at the given rating clears required_ampacity."""
    column = "c90" if insulation_rating == 90 else "c75"
    table = AMPACITY_TABLES.get(material, AMPACITY_TABLE_CU)
    for conductor in CONDUCTOR_ORDER:
        if table[conductor][column] >= required_ampacity:
            return conductor
    return None


def equipment_grounding_conductor_size(ocpd_rating_a: float, material: str = "CU") -> str:
    """Table 250.122 lookup: minimum EGC size for the OCPD protecting the
    circuit. Note 250.122(F): where ungrounded conductors are run in
    parallel in multiple raceways/cables, the EGC is NOT downsized per
    raceway — every raceway gets its own EGC sized off the full OCPD rating.
    Callers building a parallel-set BOM should multiply this size's per-foot
    cost by the number of raceways, not divide the ampacity across them."""
    breakpoints = EGC_BREAKPOINTS.get(material, EGC_BREAKPOINTS_CU)
    for breakpoint, size in breakpoints:
        if ocpd_rating_a <= breakpoint:
            return size
    return breakpoints[-1][1]


def next_standard_size(min_rating: float) -> int:
    """Next NEC 240.6(A) standard size at or above min_rating."""
    for size in STANDARD_OCPD_SIZES:
        if size >= min_rating:
            return size
    return STANDARD_OCPD_SIZES[-1]


def grounding_conductor_size(conductor: str) -> str:
    """Table 250.66 lookup: bonding/grounding conductor size for a given ungrounded conductor."""
    idx = CONDUCTOR_ORDER.index(conductor)
    for breakpoint, size in GROUNDING_CONDUCTOR_BREAKPOINTS:
        if idx <= CONDUCTOR_ORDER.index(breakpoint):
            return size
    return GROUNDING_CONDUCTOR_BREAKPOINTS[-1][1]
