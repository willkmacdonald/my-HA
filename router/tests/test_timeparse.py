"""timeparse unit tests — table-driven over supported forms + rejection cases (spec §Testing)."""

from datetime import datetime

import pytest
import timeparse
from timeparse import LOCAL_TZ

# --- durations ---


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("ten minutes", 600),
        ("10 minutes", 600),
        ("1 minute", 60),
        ("a minute", 60),
        ("an hour", 3600),
        ("two hours", 7200),
        ("twenty minutes", 1200),
        ("twenty five minutes", 1500),
        ("twenty-five minutes", 1500),
        ("45 seconds", 45),
        ("an hour and a half", 5400),
        ("one and a half hours", 5400),
        ("half an hour", 1800),
        ("1 hour and 20 minutes", 4800),
    ],
)
def test_parse_duration_accepts(text: str, seconds: int) -> None:
    assert timeparse.parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing
        "eleventy minutes",  # not a number word
        "0 minutes",  # zero rejected
        "25 hours",  # > 24 h sanity cap
        "ten",  # number without unit
        "minutes",  # unit without number
    ],
)
def test_parse_duration_rejects(text: str) -> None:
    assert timeparse.parse_duration(text) is None


def _at(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


# --- clock parsing ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7", (7, 0, False)),
        ("7 am", (7, 0, True)),
        ("7 a m", (7, 0, True)),  # whisper writes "7 a.m." -> _norm -> "7 a m"
        ("6:30 pm", (18, 30, True)),
        ("12 pm", (12, 0, True)),
        ("12 am", (0, 0, True)),
        ("noon", (12, 0, True)),
        ("midnight", (0, 0, True)),
        ("seven am", (7, 0, True)),
    ],
)
def test_parse_clock_accepts(text: str, expected: tuple[int, int, bool]) -> None:
    assert timeparse.parse_clock(text) == expected


@pytest.mark.parametrize("text", ["", "25", "13 pm", "7:75", "banana"])
def test_parse_clock_rejects(text: str) -> None:
    assert timeparse.parse_clock(text) is None


# --- clock resolution ---


def test_explicit_meridiem_future_today() -> None:
    now = _at(2026, 7, 16, 6, 0)
    assert timeparse.resolve_clock(7, 0, True, now=now) == _at(2026, 7, 16, 7, 0)


def test_explicit_meridiem_past_rolls_to_tomorrow() -> None:
    now = _at(2026, 7, 16, 8, 0)
    assert timeparse.resolve_clock(7, 0, True, now=now) == _at(2026, 7, 17, 7, 0)


def test_bare_clock_resolves_next_occurrence_within_12h_pm() -> None:
    # "set an alarm for 7" said at 10:00 -> 7 pm today (9 h away)
    now = _at(2026, 7, 16, 10, 0)
    assert timeparse.resolve_clock(7, 0, False, now=now) == _at(2026, 7, 16, 19, 0)


def test_bare_clock_resolves_next_occurrence_within_12h_am() -> None:
    # "wake me at 7" said at 22:00 -> 7 am tomorrow (9 h away)
    now = _at(2026, 7, 16, 22, 0)
    assert timeparse.resolve_clock(7, 0, False, now=now) == _at(2026, 7, 17, 7, 0)


# --- recurrence ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("every day", "daily"),
        ("daily", "daily"),
        ("every weekday", "weekdays"),
        ("on weekdays", "weekdays"),
        ("every weekend", "weekends"),
        ("on weekends", "weekends"),
        ("every monday", "weekly:0"),
        ("on sundays", "weekly:6"),
        ("", "none"),
        ("sometimes", "none"),
    ],
)
def test_parse_recurrence(text: str, expected: str) -> None:
    assert timeparse.parse_recurrence(text) == expected


# --- next_occurrence ---


def test_next_occurrence_weekday_skips_weekend() -> None:
    # Friday 2026-07-17 08:00, weekday 7 am alarm -> Monday 2026-07-20 07:00
    after = _at(2026, 7, 17, 8, 0)
    got = timeparse.next_occurrence(after=after, hour=7, minute=0, recurrence="weekdays")
    assert got == _at(2026, 7, 20, 7, 0)


def test_next_occurrence_daily_preserves_wall_clock_across_dst() -> None:
    # US DST ends Sun 2026-11-01 (CDT -> CST). 7 am local stays 7 am local;
    # the UTC instant shifts by one hour.
    sat = _at(2026, 10, 31, 7, 0)
    sun = timeparse.next_occurrence(after=sat, hour=7, minute=0, recurrence="daily")
    assert (sun.hour, sun.minute) == (7, 0)
    assert sun.utcoffset() != sat.utcoffset()
