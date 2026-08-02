"""Regression baseline: 1200A busbar / 800A main / 15 inverters at 350A each,
verified live in the HMI draft's browser console this session — this exact
combination correctly fails the 120% rule (an undersized busbar, not a bug).
"""

from app.switchboard_calc import check_120_percent_rule


def test_default_project_fails_matches_browser_verified():
    result = check_120_percent_rule(
        busbar_rating_a=1200,
        main_rating_a=800,
        backfed_ratings_a=[350] * 15,
    )
    assert result["allowance_a"] == 1440.0
    assert result["max_allowed_backfed_a"] == 640.0
    assert result["actual_backfed_a"] == 5250.0
    assert result["passes"] is False


def test_passes_when_backfed_within_allowance():
    result = check_120_percent_rule(busbar_rating_a=1200, main_rating_a=800, backfed_ratings_a=[300])
    assert result["passes"] is True


def test_supports_heterogeneous_backfed_ratings():
    result = check_120_percent_rule(busbar_rating_a=2000, main_rating_a=400, backfed_ratings_a=[100, 150, 200])
    assert result["actual_backfed_a"] == 450.0
