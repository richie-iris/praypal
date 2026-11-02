"""test_rt_liturgical.py — Tests for multi-faith liturgical calendar computations."""
import datetime
import pytest
from rt_liturgical import (
    calculate_easter_sunday,
    get_christian_season,
    get_liturgical_profile,
)


def test_calculate_easter_sunday():
    # Canonical Easter dates
    assert calculate_easter_sunday(2026) == datetime.date(2026, 4, 5)
    assert calculate_easter_sunday(2027) == datetime.date(2027, 3, 28)


def test_get_christian_season():
    # Christmas
    season, color = get_christian_season(datetime.date(2026, 12, 25))
    assert season == "Christmas Season"
    assert "White" in color

    # Lent in 2026 (Easter is April 5, Ash Wed is Feb 18)
    season_lent, color_lent = get_christian_season(datetime.date(2026, 3, 1))
    assert season_lent == "Lent"
    assert "Purple" in color_lent

    # Ordinary Time in September
    season_ord, color_ord = get_christian_season(datetime.date(2026, 9, 7))
    assert season_ord == "Ordinary Time"
    assert color_ord == "Green"


def test_get_liturgical_profile_sabbath():
    # Saturday, September 5, 2026 was a Saturday (Shabbat)
    sabbath_date = datetime.date(2026, 9, 5)
    profile = get_liturgical_profile(sabbath_date)

    assert profile.is_sabbath is True
    assert any(obs.name == "Shabbat" for obs in profile.observances)
    assert "Shabbat Shalom" in profile.suggested_greeting


def test_get_liturgical_profile_christmas_eve():
    christmas_eve = datetime.date(2026, 12, 24)
    profile = get_liturgical_profile(christmas_eve)

    assert any(obs.name == "Christmas Eve" for obs in profile.observances)
    assert "Luke 2:1-20" in [obs.scripture_focus for obs in profile.observances if obs.name == "Christmas Eve"][0]
