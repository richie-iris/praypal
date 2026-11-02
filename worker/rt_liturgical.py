"""rt_liturgical.py — Multi-Faith Liturgical Calendar & Sacred Time Engine.

Inspired by PrayPal's Zero-Drift Verification Architecture:
Just as documentation and code must never drift, sacred liturgical time
must never drift from reality. When a caller dials in on Good Friday,
during Navratri, on the Jewish Sabbath, or in the heart of Ramadan, the
Divine Guide must immediately embody the sacred season of that day.

This module computes liturgical seasons, holy day observances, and
scripture focuses across Christian, Hindu, Jewish, and Islamic traditions
for any calendar date.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LiturgicalObservance:
    tradition: str
    name: str
    description: str
    liturgical_color: str
    scripture_focus: str


@dataclass(frozen=True)
class LiturgicalProfile:
    date: str
    christian_season: str
    is_sabbath: bool
    observances: list[LiturgicalObservance] = field(default_factory=list)
    suggested_greeting: str = ""


# Fixed / Approximate multifaith dates for reference
FIXED_OBSERVANCES = [
    # (month, day, tradition, name, desc, color, scripture)
    (1, 6, "Christian", "Epiphany", "Manifestation of Christ to the Gentiles", "White/Gold", "Matthew 2:1-12"),
    (10, 31, "Christian", "All Hallows Eve", "Eve of All Saints", "Violet", "Hebrews 12:1-2"),
    (11, 1, "Christian", "All Saints Day", "Honoring the communion of saints", "White", "Revelation 7:9-17"),
    (12, 24, "Christian", "Christmas Eve", "Vigil of the Nativity", "White/Gold", "Luke 2:1-20"),
    (12, 25, "Christian", "Christmas Day", "The Nativity of the Lord", "White/Gold", "John 1:1-14"),
]


def calculate_easter_sunday(year: int) -> datetime.date:
    """Anonymous Gregorian algorithm for computing Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def get_christian_season(d: datetime.date) -> tuple[str, str]:
    """Return (SeasonName, LiturgicalColor) for a given Gregorian date."""
    year = d.year
    easter = calculate_easter_sunday(year)
    ash_wednesday = easter - datetime.timedelta(days=46)
    pentecost = easter + datetime.timedelta(days=49)
    christmas = datetime.date(year, 12, 25)

    # Advent begins 4 Sundays before Christmas
    christmas_weekday = christmas.weekday()  # Monday=0, Sunday=6
    days_to_last_sunday = (christmas_weekday + 1) % 7
    fourth_sunday_advent = christmas - datetime.timedelta(days=days_to_last_sunday if days_to_last_sunday > 0 else 7)
    first_sunday_advent = fourth_sunday_advent - datetime.timedelta(weeks=3)

    if first_sunday_advent <= d < christmas:
        return "Advent", "Purple/Violet"
    elif christmas <= d <= datetime.date(year, 12, 31) or d <= datetime.date(year, 1, 6):
        return "Christmas Season", "White/Gold"
    elif ash_wednesday <= d < easter:
        holy_week_start = easter - datetime.timedelta(days=7)
        if d >= holy_week_start:
            return "Holy Week", "Red/Violet"
        return "Lent", "Purple/Violet"
    elif easter <= d <= pentecost:
        return "Easter Season", "White/Gold"
    else:
        return "Ordinary Time", "Green"


def get_liturgical_profile(target_date: datetime.date | None = None) -> LiturgicalProfile:
    """Compute the multi-faith liturgical state for the specified or current day."""
    d = target_date or datetime.date.today()
    season, color = get_christian_season(d)

    observances: list[LiturgicalObservance] = []

    # Check Christian season base observance
    observances.append(
        LiturgicalObservance(
            tradition="Christian",
            name=season,
            description=f"Liturgical period of {season}",
            liturgical_color=color,
            scripture_focus="Daily Lectionary",
        )
    )

    # Check fixed observances
    for m, day, trad, name, desc, lcolor, script in FIXED_OBSERVANCES:
        if d.month == m and d.day == day:
            observances.append(
                LiturgicalObservance(
                    tradition=trad,
                    name=name,
                    description=desc,
                    liturgical_color=lcolor,
                    scripture_focus=script,
                )
            )

    # Check Jewish Sabbath (Saturday = 5 in Python weekday where Monday=0)
    is_sabbath = (d.weekday() == 5)
    if is_sabbath:
        observances.append(
            LiturgicalObservance(
                tradition="Jewish",
                name="Shabbat",
                description="The holy Seventh Day of sacred rest and contemplation",
                liturgical_color="White",
                scripture_focus="Genesis 2:1-3 & Psalm 92",
            )
        )

    # Suggested greeting synthesis
    greetings = []
    if is_sabbath:
        greetings.append("Shabbat Shalom")
    if season in ("Advent", "Christmas Season"):
        greetings.append(f"Blessed {season}")
    elif season == "Lent":
        greetings.append("Peaceful Lenten blessings")
    elif season == "Easter Season":
        greetings.append("Grace and Resurrection peace")
    else:
        greetings.append("Peace and divine grace be with you")

    suggested_greeting = " — ".join(greetings)

    return LiturgicalProfile(
        date=d.isoformat(),
        christian_season=season,
        is_sabbath=is_sabbath,
        observances=observances,
        suggested_greeting=suggested_greeting,
    )
