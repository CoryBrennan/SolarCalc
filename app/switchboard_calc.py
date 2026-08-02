"""Switchboard 120% rule — NEC 705.12(B)(3)(2): the sum of a switchboard's main
OCPD and all backfed (inverter) OCPDs must not exceed 120% of the busbar rating.
"""

from __future__ import annotations


def check_120_percent_rule(
    busbar_rating_a: float,
    main_rating_a: float,
    backfed_ratings_a: list[float],
) -> dict:
    allowance_a = busbar_rating_a * 1.2
    max_allowed_backfed_a = allowance_a - main_rating_a
    actual_backfed_a = sum(backfed_ratings_a)
    passes = actual_backfed_a <= max_allowed_backfed_a

    return {
        "allowance_a": round(allowance_a, 1),
        "max_allowed_backfed_a": round(max_allowed_backfed_a, 1),
        "actual_backfed_a": round(actual_backfed_a, 1),
        "passes": passes,
    }
