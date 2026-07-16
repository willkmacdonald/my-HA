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


# --- full utterance parsing ---

NOW = _at(2026, 7, 16, 10, 0)  # Thursday 10:00 local


def parse(text: str):
    return timeparse.parse(text, now=NOW)


# --- set timer ---


def test_set_timer_digits() -> None:
    p = parse("set a timer for 10 minutes")
    assert p == timeparse.ParsedTimer(verb="set", kind="timer", duration_seconds=600)


def test_set_timer_word_number_prefix_form() -> None:
    p = parse("twenty minute timer")
    assert p is not None and p.kind == "timer" and p.duration_seconds == 1200


def test_set_timer_hour_and_a_half() -> None:
    p = parse("start a timer for an hour and a half")
    assert p is not None and p.duration_seconds == 5400


# --- set alarm ---


def test_set_alarm_explicit() -> None:
    p = parse("set an alarm for 7 am")
    assert p is not None and p.kind == "alarm" and p.recurrence == "none"
    assert p.fire_at == _at(2026, 7, 17, 7, 0)  # 7 am tomorrow (7 am today has passed)


def test_wake_me_up_bare_clock() -> None:
    p = parse("wake me up at 6:30")
    assert p is not None and p.kind == "alarm"
    assert p.fire_at == _at(2026, 7, 16, 18, 30)  # next occurrence within 12 h


def test_set_alarm_recurring_weekdays() -> None:
    p = parse("set an alarm for 7 am every weekday")
    assert p is not None and p.recurrence == "weekdays"
    assert p.fire_at == _at(2026, 7, 17, 7, 0)  # Friday is a weekday


# --- set reminder ---


def test_reminder_at_clock() -> None:
    p = parse("remind me to feed the cat at 8 pm")
    assert p is not None and p.kind == "reminder" and p.text == "feed the cat"
    assert p.fire_at == _at(2026, 7, 16, 20, 0)


def test_reminder_in_duration() -> None:
    p = parse("remind me to check the oven in 2 hours")
    assert p is not None and p.kind == "reminder" and p.text == "check the oven"
    assert p.duration_seconds == 7200


def test_reminder_recurring() -> None:
    p = parse("remind me to take out the trash at 8 pm every monday")
    assert p is not None and p.recurrence == "weekly:0"


def test_reminder_with_capitalized_politeness_prefix() -> None:
    p = parse("Please remind me to feed the cat at 8 pm")
    assert p is not None and p.kind == "reminder" and p.text == "feed the cat"


def test_reminder_with_hey_prefix_preserves_payload_casing() -> None:
    p = parse("Hey remind me to email Dave at 8 pm")
    assert p is not None and p.text == "email Dave"


def test_set_timer_with_capitalized_politeness_prefix() -> None:
    p = parse("Okay set a timer for 10 minutes")
    assert p is not None and p.kind == "timer" and p.duration_seconds == 600


def test_reminder_text_keeps_inner_at_rightmost_wins() -> None:
    p = parse("remind me to look at the mail at 8 pm")
    assert p is not None and p.text == "look at the mail"


# --- cancel ---


def test_cancel_the_timer() -> None:
    p = parse("cancel the timer")
    assert p == timeparse.ParsedTimer(verb="cancel", kind="timer")


def test_cancel_all_timers() -> None:
    p = parse("cancel all my timers")
    assert p is not None and p.cancel_all is True and p.kind == "timer"


def test_cancel_alarm_with_clock_qualifier() -> None:
    p = parse("cancel my 7 am alarm")
    assert p is not None and p.kind == "alarm" and p.at_qualifier == (7, 0)


def test_cancel_reminders_plural_means_all() -> None:
    p = parse("cancel my reminders")
    assert p is not None and p.kind == "reminder" and p.cancel_all is True


# --- query ---


def test_query_how_long_left() -> None:
    p = parse("how long is left on my timer")
    assert p == timeparse.ParsedTimer(verb="query", kind="timer")


def test_query_what_alarms() -> None:
    p = parse("what alarms do I have")
    assert p == timeparse.ParsedTimer(verb="query", kind="alarm")


# --- rejection: unparseable -> None (spec: clarification, no state change) ---


@pytest.mark.parametrize(
    "text",
    [
        "set a timer",  # no duration
        "set a timer for the pasta",  # no parseable duration
        "set an alarm for 25",  # invalid clock
        "remind me to feed the cat",  # reminder with no time
        "please do something",  # not a timer utterance at all
    ],
)
def test_parse_rejects(text: str) -> None:
    assert parse(text) is None
