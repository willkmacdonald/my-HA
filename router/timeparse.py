"""Deterministic spoken-time parsing for timer/alarm/reminder intents.

Spec (Component 4): no LLM in this path. `parse()` returns a ParsedTimer or
None; None means "speak a clarification, change no state". All datetimes are
timezone-aware; callers pass `now` in LOCAL_TZ. Storage-side UTC conversion
happens in timers.py, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Chicago")

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


@dataclass(frozen=True)
class ParsedTimer:
    """Result of parsing a timer/alarm/reminder utterance."""

    verb: str  # "set" | "cancel" | "query"
    kind: str | None = None  # "timer" | "alarm" | "reminder" | None = unspecified
    fire_at: object | None = None  # datetime in LOCAL_TZ (set-alarm / at-reminder)
    duration_seconds: int | None = None  # set-timer / in-reminder
    text: str | None = None  # reminder payload
    recurrence: str = "none"
    cancel_all: bool = False
    at_qualifier: tuple[int, int] | None = None  # (hour, minute) for "cancel my 7 am alarm"


def _norm(text: str) -> str:
    """Lowercase, strip punctuation (keep ':' for 6:30), collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s:]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _words_to_int(phrase: str) -> int | None:
    """'25', 'ten', 'twenty five', 'a'/'an' (=1) -> int; None if not a number."""
    phrase = phrase.strip()
    if phrase.isdigit():
        return int(phrase)
    if phrase in ("a", "an"):
        return 1
    tokens = phrase.split()
    if len(tokens) == 1:
        if tokens[0] in _UNITS:
            return _UNITS[tokens[0]]
        if tokens[0] in _TENS:
            return _TENS[tokens[0]]
        return None
    if (
        len(tokens) == 2
        and tokens[0] in _TENS
        and tokens[1] in _UNITS
        and 0 < _UNITS[tokens[1]] < 10
    ):
        return _TENS[tokens[0]] + _UNITS[tokens[1]]
    return None


# Number patterns for regex: digits, a/an, word numbers 1-19, and compound 20-59
_WORD_TEENS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    "|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
)
_WORD_TENS = "twenty|thirty|forty|fifty"
_TENS_WITH_UNITS = r"(?:one|two|three|four|five|six|seven|eight|nine)"
_NUM = rf"(?:\d+|a|an|(?:{_WORD_TENS})(?: {_TENS_WITH_UNITS})?|zero|{_WORD_TEENS})"
_UNIT = r"(?P<unit>second|minute|hour)s?"

_DUR_HALF_AN = re.compile(r"^half an? (?P<unit>minute|hour)$")
_DUR_AND_A_HALF = re.compile(rf"^(?P<num>{_NUM}) and a half (?P<unit>minute|hour)s?$")
_DUR_NUM_HALF = re.compile(rf"^(?P<num>{_NUM}) (?P<unit>minute|hour)s? and a half$")
_DUR_COMPOUND = re.compile(rf"^(?P<h>{_NUM}) hours? and (?P<m>{_NUM}) minutes?$")
_DUR_SIMPLE = re.compile(rf"^(?P<num>{_NUM}) {_UNIT}$")

_MAX_DURATION_S = 24 * 3600


def _checked(seconds: float | None) -> int | None:
    if seconds is None or seconds <= 0 or seconds > _MAX_DURATION_S:
        return None
    return int(seconds)


def parse_duration(text: str) -> int | None:
    """'ten minutes', 'an hour and a half', '1 hour and 20 minutes' -> seconds."""
    t = _norm(text)
    if m := _DUR_HALF_AN.match(t):
        return _checked(_UNIT_SECONDS[m["unit"]] / 2)
    if m := _DUR_AND_A_HALF.match(t) or _DUR_NUM_HALF.match(t):
        n = _words_to_int(m["num"])
        if n is None:
            return None
        unit = _UNIT_SECONDS[m["unit"]]
        return _checked(n * unit + unit / 2)
    if m := _DUR_COMPOUND.match(t):
        h, mins = _words_to_int(m["h"]), _words_to_int(m["m"])
        if h is None or mins is None:
            return None
        return _checked(h * 3600 + mins * 60)
    if m := _DUR_SIMPLE.match(t):
        n = _words_to_int(m["num"])
        if n is None:
            return None
        return _checked(n * _UNIT_SECONDS[m["unit"]])
    return None


_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_CLOCK_RE = re.compile(rf"^(?P<h>\d{{1,2}}|{_NUM})(?::(?P<m>\d{{2}}))?(?: ?(?P<mer>a ?m|p ?m))?$")


def parse_clock(text: str) -> tuple[int, int, bool] | None:
    """'7', '7 am', '6:30 pm', 'noon', 'midnight' -> (hour24, minute, explicit)."""
    t = _norm(text)
    if t == "noon":
        return (12, 0, True)
    if t == "midnight":
        return (0, 0, True)
    m = _CLOCK_RE.match(t)
    if not m:
        return None
    hour = _words_to_int(m["h"])
    minute = int(m["m"]) if m["m"] else 0
    if hour is None or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    mer = (m["mer"] or "").replace(" ", "")
    if mer:
        if not (1 <= hour <= 12):
            return None
        hour = hour % 12 + (12 if mer == "pm" else 0)
        return (hour, minute, True)
    return (hour, minute, False)


def _combine(d: date, hour: int, minute: int) -> datetime:
    """Wall-clock combine in LOCAL_TZ — DST-safe (never adds timedeltas to aware dts)."""
    return datetime.combine(d, time(hour, minute), tzinfo=LOCAL_TZ)


def resolve_clock(hour: int, minute: int, explicit: bool, *, now: datetime) -> datetime:
    """Explicit am/pm: today-or-tomorrow at that time. Bare: next occurrence
    within 12 hours (spec rule) — try both am and pm interpretations."""
    if explicit:
        cand = _combine(now.date(), hour, minute)
        return cand if cand > now else _combine(now.date() + timedelta(days=1), hour, minute)
    # bare "7" -> {7:00, 19:00}; bare "12" -> {0:00, 12:00}; bare 24h "14" -> {14:00}
    hours = {hour} if hour > 12 else {hour % 12, hour % 12 + 12}
    candidates = sorted(
        _combine(now.date() + timedelta(days=day), h, minute) for day in (0, 1) for h in hours
    )
    for cand in candidates:
        if cand > now:
            return cand
    raise AssertionError("unreachable: candidates span more than 24 h")


def parse_recurrence(text: str) -> str:
    t = _norm(text)
    if t in ("every day", "daily", "everyday"):
        return "daily"
    if t in ("every weekday", "on weekdays", "weekdays"):
        return "weekdays"
    if t in ("every weekend", "on weekends", "weekends"):
        return "weekends"
    m = re.match(r"^(?:every|on) (?P<day>\w+?)s?$", t)
    if m and m["day"] in _WEEKDAY_NAMES:
        return f"weekly:{_WEEKDAY_NAMES[m['day']]}"
    return "none"


def _recurrence_matches(d: date, recurrence: str) -> bool:
    if recurrence in ("none", "daily"):
        return True
    if recurrence == "weekdays":
        return d.weekday() < 5
    if recurrence == "weekends":
        return d.weekday() >= 5
    if recurrence.startswith("weekly:"):
        return d.weekday() == int(recurrence.split(":", 1)[1])
    raise ValueError(f"unknown recurrence {recurrence!r}")


def next_occurrence(*, after: datetime, hour: int, minute: int, recurrence: str) -> datetime:
    """First LOCAL_TZ datetime strictly after `after` at hour:minute matching
    the recurrence day-filter. Wall-clock math -> DST-safe."""
    for days in range(0, 9):
        cand = _combine(after.date() + timedelta(days=days), hour, minute)
        if cand > after and _recurrence_matches(cand.date(), recurrence):
            return cand
    raise AssertionError("unreachable: 9 days always contains a match")
