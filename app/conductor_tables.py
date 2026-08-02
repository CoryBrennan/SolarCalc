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
AMPACITY_TABLE: dict[str, dict[str, int]] = {
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


def select_conductor(required_ampacity: float, insulation_rating: int) -> str | None:
    """Smallest conductor whose ampacity at the given rating clears required_ampacity."""
    column = "c90" if insulation_rating == 90 else "c75"
    for conductor in CONDUCTOR_ORDER:
        if AMPACITY_TABLE[conductor][column] >= required_ampacity:
            return conductor
    return None


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
