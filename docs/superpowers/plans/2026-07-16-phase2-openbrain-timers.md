# Phase 2: Open Brain + Timers Implementation Plan (my-HA)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the signed-off Phase 2 spec — timers/alarms/reminders with a persistent scheduler and websocket push to satellites, plus the real Open Brain knowledge route — in the my-HA repo.

**Architecture:** The router grows three new modules that keep `router.py` thin: `timeparse.py` (deterministic spoken-time parser), `timers.py` (SQLite store + asyncio scheduler + spoken-phrase helpers), and `push.py` (ws `/events` connection registry). `router.py` gains a FastAPI lifespan that owns shared clients, the store, and the scheduler/ping background tasks. The fake satellite gains a background listener thread. Carry-over fixes (module-level clients, STT timeout + error frame, py_compile smoke) land in the tasks that touch those files.

**Tech Stack:** Python 3.11, FastAPI/uvicorn, aiosqlite (new dep), websockets, httpx + respx (tests), anthropic SDK, pytest (`asyncio_mode=auto`).

**Spec:** `docs/superpowers/specs/2026-07-12-phase2-openbrain-timers-design.md` (signed off 2026-07-15, includes four audit-review edits). The companion plan `2026-07-16-open-brain-search-api-key.md` covers the open-brain repo change; this plan does NOT touch that repo and is fully testable without it (Open Brain calls are respx-mocked).

## Global Constraints

Copied verbatim from the spec — every task implicitly includes these:

- `ANNOUNCE_INTERVAL_S = 30`, `ANNOUNCE_MAX_REPEATS = 10` (5 min), `MISSED_GRACE_S = 300`.
- Satellite reconnect backoff 1 s → 30 s (capped exponential); router pings every 20 s.
- Recurrence values: `'none' | 'daily' | 'weekdays' | 'weekends' | 'weekly:<0-6>'` — presets only, no cron/RRULE. Weekday numbering is Python's: Monday=0 … Sunday=6.
- Timezone: parse and speak in `America/Chicago` (zoneinfo); **store UTC ISO-8601**. Recurrence math happens in local time.
- **No LLM in the timer path.** Unparseable requests get a spoken clarification, never a guess.
- Open Brain: `POST {OPEN_BRAIN_URL}/api/search`, header `X-API-Key`, body `{"query": ..., "limit": 5}`, response `{"thoughts": [{"content", "similarity", ...}]}` (NO `summary` field); keep hits with `similarity >= 0.55`; "found nothing" and "couldn't reach" are distinct spoken outcomes.
- `route()` never returns 5xx for routing failures (existing contract, keep it).
- Timer intents listed **before** device intents; all command patterns anchored to utterance start (optional politeness prefixes); unknown `action.type`/`verb` → clarification speech + startup validation fails fast.
- STT: client `asyncio.timeout(60)`; server replies `{"text": "", "error": "<exception class>"}` on transcription failure.
- `router/timers.db` is gitignored; SQLite in WAL mode; single writer (the router process).
- Style: type hints everywhere, ruff line-length 100 (`ruff check .` must stay clean), snake_case, tests use the existing `tests/conftest.py` sys.path-shim import pattern.
- Tests must never hit the network or real Anthropic — mock `ask_llm`/the anthropic client, use respx for httpx, local `websockets.serve` fakes for ws.
- Baseline before Task 1: 23 tests green. Run the suite from the repo root: `source .venv/bin/activate && python3 -m pytest`.
- Install the new dependency into the venv when Task 4 adds it: `uv pip install aiosqlite`.

## File Structure

- Create: `router/timeparse.py` — pure parsing + calendar math (`parse`, `next_occurrence`); no I/O.
- Create: `router/timers.py` — `Timer`, `TimerStore` (aiosqlite), `Scheduler`, `recover`, `handle_timer`, phrase helpers.
- Create: `router/push.py` — `PushChannel` (connection set, broadcast, ack routing, ping loop).
- Modify: `router/router.py` — lifespan + shared clients, startup intent validation, timer dispatch, unknown-type guard, `ask_open_brain` rewrite.
- Modify: `router/intents.yaml` — timer intents first, anchored patterns, `verb` field.
- Modify: `satellite/pipeline.py` — transcribe timeout + error-frame handling.
- Modify: `server/stt_server.py` — error frame on transcription failure.
- Modify: `satellite/fake_satellite.py` — `EventListener` thread, ack path, EOF exit.
- Create: `router/tests/test_timeparse.py`, `router/tests/test_timers.py`, `router/tests/test_push.py`, `satellite/tests/test_satellite_smoke.py`.
- Modify: `router/tests/test_router.py`, `satellite/tests/test_pipeline.py`, `satellite/tests/test_fake_satellite.py`, `server/tests/test_stt_server.py`, `router/requirements.txt`, `.gitignore`, `README.md`.

---

### Task 1: timeparse — module skeleton, number words, durations

**Files:**
- Create: `router/timeparse.py`
- Test: `router/tests/test_timeparse.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces: `LOCAL_TZ: ZoneInfo`, `ParsedTimer` dataclass, `parse_duration(text: str) -> int | None` (seconds), `_norm(text: str) -> str`. Later tasks add `parse()`, `parse_clock()`, `next_occurrence()` to this module.

- [ ] **Step 1: Write the failing tests**

Create `router/tests/test_timeparse.py`:

```python
"""timeparse unit tests — table-driven over supported forms + rejection cases (spec §Testing)."""

import pytest
import timeparse


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
        "",                    # nothing
        "eleventy minutes",    # not a number word
        "0 minutes",           # zero rejected
        "25 hours",            # > 24 h sanity cap
        "ten",                 # number without unit
        "minutes",             # unit without number
    ],
)
def test_parse_duration_rejects(text: str) -> None:
    assert timeparse.parse_duration(text) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest router/tests/test_timeparse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'timeparse'` (collection error is fine).

- [ ] **Step 3: Write the implementation**

Create `router/timeparse.py`:

```python
"""Deterministic spoken-time parsing for timer/alarm/reminder intents.

Spec (Component 4): no LLM in this path. `parse()` returns a ParsedTimer or
None; None means "speak a clarification, change no state". All datetimes are
timezone-aware; callers pass `now` in LOCAL_TZ. Storage-side UTC conversion
happens in timers.py, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Chicago")

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


@dataclass(frozen=True)
class ParsedTimer:
    """Result of parsing a timer/alarm/reminder utterance."""

    verb: str  # "set" | "cancel" | "query"
    kind: str | None = None  # "timer" | "alarm" | "reminder" | None = unspecified
    fire_at: "object | None" = None  # datetime in LOCAL_TZ (set-alarm / at-reminder)
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
    if len(tokens) == 2 and tokens[0] in _TENS and tokens[1] in _UNITS and 0 < _UNITS[tokens[1]] < 10:
        return _TENS[tokens[0]] + _UNITS[tokens[1]]
    return None


_NUM = r"(?:\d+|a|an|(?:twenty|thirty|forty|fifty)(?: (?:one|two|three|four|five|six|seven|eight|nine))?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_timeparse.py -v`
Expected: all PASS (20 tests).

- [ ] **Step 5: Lint and commit**

```bash
ruff check router/timeparse.py router/tests/test_timeparse.py
git add router/timeparse.py router/tests/test_timeparse.py
git commit -m "feat(timeparse): duration parsing with word numbers"
```

---

### Task 2: timeparse — clock times, resolution rules, recurrence, next_occurrence

**Files:**
- Modify: `router/timeparse.py`
- Test: `router/tests/test_timeparse.py`

**Interfaces:**
- Consumes: `_norm`, `_words_to_int`, `LOCAL_TZ` from Task 1.
- Produces: `parse_clock(text: str) -> tuple[int, int, bool] | None` (hour24-if-explicit, minute, explicit_meridiem), `resolve_clock(hour: int, minute: int, explicit: bool, *, now: datetime) -> datetime`, `parse_recurrence(text: str) -> str`, `next_occurrence(*, after: datetime, hour: int, minute: int, recurrence: str) -> datetime`. All datetimes aware in `LOCAL_TZ`.

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_timeparse.py`:

```python
from datetime import datetime

from timeparse import LOCAL_TZ


def _at(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


# --- clock parsing ---

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7", (7, 0, False)),
        ("7 am", (7, 0, True)),
        ("7 a m", (7, 0, True)),      # whisper writes "7 a.m." -> _norm -> "7 a m"
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_timeparse.py -v`
Expected: new tests FAIL with `AttributeError: module 'timeparse' has no attribute 'parse_clock'`; Task 1 tests still PASS.

- [ ] **Step 3: Write the implementation**

Append to `router/timeparse.py` (also add `from datetime import date, datetime, time, timedelta` to the imports at the top):

```python
_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_CLOCK_RE = re.compile(
    rf"^(?P<h>\d{{1,2}}|{_NUM})(?::(?P<m>\d{{2}}))?(?: ?(?P<mer>a ?m|p ?m))?$"
)


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
        _combine(now.date() + timedelta(days=day), h, minute)
        for day in (0, 1)
        for h in hours
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
```

Note on `resolve_clock` candidates: keep the generator exactly as written — it produces today/tomorrow at `hour` and `hour+12 (mod 24)`, sorted; the loop picks the first future one, which is always within 12 hours for bare 1–12 inputs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_timeparse.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check router/
git add router/timeparse.py router/tests/test_timeparse.py
git commit -m "feat(timeparse): clock parsing, 12h resolution rule, recurrence, next_occurrence"
```

---

### Task 3: timeparse — full utterance parsing (set/cancel/query)

**Files:**
- Modify: `router/timeparse.py`
- Test: `router/tests/test_timeparse.py`

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces: `parse(text: str, *, now: datetime) -> ParsedTimer | None` — the single entry point Task 6's `handle_timer` calls.

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_timeparse.py`:

```python
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
        "set a timer",                      # no duration
        "set a timer for the pasta",        # no parseable duration
        "set an alarm for 25",              # invalid clock
        "remind me to feed the cat",        # reminder with no time
        "please do something",              # not a timer utterance at all
    ],
)
def test_parse_rejects(text: str) -> None:
    assert parse(text) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_timeparse.py -v`
Expected: new tests FAIL with `AttributeError: ... no attribute 'parse'`.

- [ ] **Step 3: Write the implementation**

Append to `router/timeparse.py`:

```python
_REC_TAIL = r"(?: (?P<rec>(?:every|on) [a-z ]+?|daily))?"

_SET_TIMER_RE = re.compile(
    rf"^(?:set|start) (?:a |an )?timer(?: for (?P<dur>.+))?$"
)
_NUM_MIN_TIMER_RE = re.compile(rf"^(?P<dur>.+?) timer$")
_SET_ALARM_RE = re.compile(
    rf"^(?:set (?:a |an )?alarm for|wake me(?: up)? at) (?P<clock>.+?){_REC_TAIL}$"
)
_REMIND_RE = re.compile(
    rf"^remind me to (?P<text>.+) (?P<prep>at|in) (?P<when>.+?){_REC_TAIL}$"
)
_CANCEL_RE = re.compile(
    r"^cancel (?P<all>all )?(?:my |the )?(?:(?P<clock>.+?) )?"
    r"(?P<kind>timer|alarm|reminder)(?P<plural>s)?$"
)
_QUERY_LEFT_RE = re.compile(r"^how long(?: is)? left(?: on (?:my|the) timers?)?$")
_QUERY_LIST_RE = re.compile(r"^what (?P<kind>timer|alarm|reminder)s?(?: do i have)?$")

_POLITENESS_RE = re.compile(r"^(?:please |hey |ok |okay |um |uh )+")


def _strip_politeness(t: str) -> str:
    return _POLITENESS_RE.sub("", t)


def parse(text: str, *, now: datetime) -> ParsedTimer | None:
    """Parse a timer-family utterance. None = unparseable -> clarification."""
    t = _strip_politeness(_norm(text))

    if m := _SET_TIMER_RE.match(t):
        dur = parse_duration(m["dur"]) if m["dur"] else None
        if dur is None:
            return None
        return ParsedTimer(verb="set", kind="timer", duration_seconds=dur)

    if (m := _NUM_MIN_TIMER_RE.match(t)) and parse_duration(m["dur"]) is not None:
        return ParsedTimer(verb="set", kind="timer", duration_seconds=parse_duration(m["dur"]))

    if m := _SET_ALARM_RE.match(t):
        clock = parse_clock(m["clock"])
        if clock is None:
            return None
        recurrence = parse_recurrence(m["rec"] or "")
        hour, minute, explicit = clock
        fire = resolve_clock(hour, minute, explicit, now=now)
        if recurrence != "none" and not _recurrence_matches(fire.date(), recurrence):
            fire = next_occurrence(after=now, hour=fire.hour, minute=minute, recurrence=recurrence)
        return ParsedTimer(verb="set", kind="alarm", fire_at=fire, recurrence=recurrence)

    # reminders: parse on the ORIGINAL text (minus politeness/punctuation) so the
    # spoken payload keeps its casing; the greedy (?P<text>.+) makes the LAST
    # at/in win ("look at the mail at 8 pm").
    orig = _strip_politeness(re.sub(r"[.!?]+$", "", text.strip()))
    if m := _REMIND_RE.match(orig.lower()):
        span_text = orig[m.start("text"):m.end("text")]
        recurrence = parse_recurrence(m["rec"] or "")
        if m["prep"] == "at":
            clock = parse_clock(m["when"])
            if clock is None:
                return None
            hour, minute, explicit = clock
            fire = resolve_clock(hour, minute, explicit, now=now)
            if recurrence != "none" and not _recurrence_matches(fire.date(), recurrence):
                fire = next_occurrence(after=now, hour=fire.hour, minute=minute, recurrence=recurrence)
            return ParsedTimer(
                verb="set", kind="reminder", fire_at=fire, text=span_text, recurrence=recurrence
            )
        dur = parse_duration(m["when"])
        if dur is None:
            return None
        return ParsedTimer(
            verb="set", kind="reminder", duration_seconds=dur, text=span_text,
            recurrence=recurrence,
        )

    if m := _CANCEL_RE.match(t):
        qualifier: tuple[int, int] | None = None
        if m["clock"]:
            clock = parse_clock(m["clock"])
            if clock is None:
                return None
            qualifier = (clock[0], clock[1])
        cancel_all = bool(m["all"]) or (bool(m["plural"]) and qualifier is None)
        return ParsedTimer(
            verb="cancel", kind=m["kind"], cancel_all=cancel_all, at_qualifier=qualifier
        )

    if _QUERY_LEFT_RE.match(t):
        return ParsedTimer(verb="query", kind="timer")
    if m := _QUERY_LIST_RE.match(t):
        return ParsedTimer(verb="query", kind=m["kind"])

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_timeparse.py -v`
Expected: all PASS. If `test_reminder_text_keeps_inner_at_rightmost_wins` fails, check that `(?P<text>.+)` is greedy (no `?`).

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check router/ && python3 -m pytest
git add router/timeparse.py router/tests/test_timeparse.py
git commit -m "feat(timeparse): full utterance parsing for set/cancel/query"
```

---

### Task 4: timers — Timer model + TimerStore (aiosqlite, WAL)

**Files:**
- Create: `router/timers.py`
- Modify: `router/requirements.txt`, `.gitignore`
- Test: `router/tests/test_timers.py`

**Interfaces:**
- Consumes: `LOCAL_TZ`, `next_occurrence` from `timeparse` (Task 2).
- Produces: `Timer` dataclass (fields: `id, kind, text, fire_at, recurrence, duration_seconds, status, created_at`; `fire_at_dt` property → aware UTC datetime), `TimerStore` with `async open()`, `async close()`, `async add(*, kind, fire_at, text=None, recurrence="none", duration_seconds=None) -> Timer`, `async get(timer_id) -> Timer | None`, `async by_status(status) -> list[Timer]` (ordered by fire_at), `async next_armed() -> Timer | None`, `async set_status(timer_id, status)`, `async rearm(timer_id, fire_at)`; helpers `utcnow() -> datetime`, `to_utc_iso(dt) -> str`, `from_utc_iso(s) -> datetime`.

- [ ] **Step 1: Add the dependency and gitignore entries**

Append `aiosqlite` on its own line to `router/requirements.txt`. Append to `.gitignore` (bottom):

```
# Phase 2 timer store (spec Component 1) — runtime state, never committed
router/timers.db*
```

Install: `uv pip install aiosqlite`

- [ ] **Step 2: Write the failing tests**

Create `router/tests/test_timers.py`:

```python
"""TimerStore + scheduler tests — temp DB per test (spec §Testing)."""

from datetime import timedelta
from pathlib import Path

import pytest
import timers


@pytest.fixture
async def store(tmp_path: Path):
    s = timers.TimerStore(tmp_path / "timers.db")
    await s.open()
    yield s
    await s.close()


async def test_add_and_get_roundtrip(store: timers.TimerStore) -> None:
    fire = timers.utcnow() + timedelta(minutes=10)
    t = await store.add(kind="timer", fire_at=fire, duration_seconds=600)
    got = await store.get(t.id)
    assert got is not None
    assert got.kind == "timer"
    assert got.status == "armed"
    assert got.duration_seconds == 600
    assert abs((got.fire_at_dt - fire).total_seconds()) < 1


async def test_by_status_ordered_by_fire_at(store: timers.TimerStore) -> None:
    late = await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(minutes=20))
    early = await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(minutes=5))
    armed = await store.by_status("armed")
    assert [t.id for t in armed] == [early.id, late.id]


async def test_next_armed_none_when_empty(store: timers.TimerStore) -> None:
    assert await store.next_armed() is None


async def test_set_status_and_rearm(store: timers.TimerStore) -> None:
    t = await store.add(kind="alarm", fire_at=timers.utcnow() + timedelta(hours=1))
    await store.set_status(t.id, "firing")
    assert (await store.get(t.id)).status == "firing"
    new_fire = timers.utcnow() + timedelta(days=1)
    await store.rearm(t.id, new_fire)
    got = await store.get(t.id)
    assert got.status == "armed"
    assert abs((got.fire_at_dt - new_fire).total_seconds()) < 1


async def test_wal_mode_enabled(store: timers.TimerStore) -> None:
    assert await store.journal_mode() == "wal"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_timers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'timers'`.

- [ ] **Step 4: Write the implementation**

Create `router/timers.py`:

```python
"""Timer store, scheduler, and spoken-phrase helpers (spec Component 1).

router.py stays thin and dispatches here. SQLite via aiosqlite, WAL mode,
single writer (the router process). Times stored as UTC ISO-8601 strings;
all local-time math lives in timeparse.py.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

log = logging.getLogger("timers")

ANNOUNCE_INTERVAL_S = 30
ANNOUNCE_MAX_REPEATS = 10
MISSED_GRACE_S = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('timer','alarm','reminder')),
  text       TEXT,
  fire_at    TEXT NOT NULL,
  recurrence TEXT NOT NULL DEFAULT 'none',
  duration_seconds INTEGER,
  status     TEXT NOT NULL DEFAULT 'armed',
  created_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_utc_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


@dataclass(frozen=True)
class Timer:
    id: str
    kind: str
    text: str | None
    fire_at: str  # UTC ISO-8601
    recurrence: str
    duration_seconds: int | None
    status: str
    created_at: str

    @property
    def fire_at_dt(self) -> datetime:
        return from_utc_iso(self.fire_at)


def _row_to_timer(row: aiosqlite.Row) -> Timer:
    return Timer(**{k: row[k] for k in row.keys()})


class TimerStore:
    """Async SQLite store. Call open() before use, close() at shutdown."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def journal_mode(self) -> str:
        async with self._db.execute("PRAGMA journal_mode") as cur:
            return (await cur.fetchone())[0]

    async def add(
        self,
        *,
        kind: str,
        fire_at: datetime,
        text: str | None = None,
        recurrence: str = "none",
        duration_seconds: int | None = None,
    ) -> Timer:
        t = Timer(
            id=str(uuid.uuid4()),
            kind=kind,
            text=text,
            fire_at=to_utc_iso(fire_at),
            recurrence=recurrence,
            duration_seconds=duration_seconds,
            status="armed",
            created_at=to_utc_iso(utcnow()),
        )
        await self._db.execute(
            "INSERT INTO timers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, t.kind, t.text, t.fire_at, t.recurrence, t.duration_seconds,
             t.status, t.created_at),
        )
        await self._db.commit()
        return t

    async def get(self, timer_id: str) -> Timer | None:
        async with self._db.execute("SELECT * FROM timers WHERE id = ?", (timer_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_timer(row) if row else None

    async def by_status(self, status: str) -> list[Timer]:
        async with self._db.execute(
            "SELECT * FROM timers WHERE status = ? ORDER BY fire_at", (status,)
        ) as cur:
            return [_row_to_timer(r) for r in await cur.fetchall()]

    async def next_armed(self) -> Timer | None:
        armed = await self.by_status("armed")
        return armed[0] if armed else None

    async def set_status(self, timer_id: str, status: str) -> None:
        await self._db.execute("UPDATE timers SET status = ? WHERE id = ?", (status, timer_id))
        await self._db.commit()

    async def rearm(self, timer_id: str, fire_at: datetime) -> None:
        await self._db.execute(
            "UPDATE timers SET status = 'armed', fire_at = ? WHERE id = ?",
            (to_utc_iso(fire_at), timer_id),
        )
        await self._db.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_timers.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint and commit**

```bash
ruff check router/ && python3 -m pytest
git add router/timers.py router/tests/test_timers.py router/requirements.txt .gitignore
git commit -m "feat(timers): SQLite TimerStore with WAL mode"
```

---

### Task 5: timers — spoken-phrase helpers + announcement speech

**Files:**
- Modify: `router/timers.py`
- Test: `router/tests/test_timers.py`

**Interfaces:**
- Consumes: `Timer`, `LOCAL_TZ` (import `from timeparse import LOCAL_TZ` in timers.py).
- Produces: `duration_noun(seconds) -> str` ("10 minutes"), `duration_adj(seconds) -> str` ("10-minute"), `clock_phrase(dt_local) -> str` ("7 am", "6:30 pm", "noon", "midnight"), `recurrence_phrase(recurrence) -> str` ("", " every day", " every weekday", " every weekend", " every Monday"), `remaining_phrase(seconds) -> str` ("8 minutes 12 seconds"), `announcement_speech(t: Timer) -> str` — exact spec phrasings.

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_timers.py`:

```python
from datetime import datetime

from timeparse import LOCAL_TZ


def _local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 7, 16, h, m, tzinfo=LOCAL_TZ)


def _mk(kind: str, *, fire_local: datetime | None = None, text: str | None = None,
        duration_seconds: int | None = None, recurrence: str = "none") -> timers.Timer:
    fire = fire_local or _local(7)
    return timers.Timer(
        id="t1", kind=kind, text=text, fire_at=timers.to_utc_iso(fire),
        recurrence=recurrence, duration_seconds=duration_seconds,
        status="armed", created_at=timers.to_utc_iso(timers.utcnow()),
    )


@pytest.mark.parametrize(
    ("seconds", "noun", "adj"),
    [(600, "10 minutes", "10-minute"), (60, "1 minute", "1-minute"),
     (3600, "1 hour", "1-hour"), (7200, "2 hours", "2-hour"),
     (90, "90 seconds", "90-second"), (5400, "90 minutes", "90-minute")],
)
def test_duration_phrases(seconds: int, noun: str, adj: str) -> None:
    assert timers.duration_noun(seconds) == noun
    assert timers.duration_adj(seconds) == adj


@pytest.mark.parametrize(
    ("h", "m", "phrase"),
    [(7, 0, "7 am"), (18, 30, "6:30 pm"), (12, 0, "noon"), (0, 0, "midnight"), (23, 5, "11:05 pm")],
)
def test_clock_phrase(h: int, m: int, phrase: str) -> None:
    assert timers.clock_phrase(_local(h, m)) == phrase


def test_announcement_speech_timer() -> None:
    t = _mk("timer", duration_seconds=120)
    assert timers.announcement_speech(t) == "Your 2-minute timer is done."


def test_announcement_speech_alarm() -> None:
    t = _mk("alarm", fire_local=_local(7))
    assert timers.announcement_speech(t) == "It's 7 am. This is your alarm."


def test_announcement_speech_reminder() -> None:
    t = _mk("reminder", text="feed the cat")
    assert timers.announcement_speech(t) == "Reminder: feed the cat."


def test_remaining_phrase() -> None:
    assert timers.remaining_phrase(492) == "8 minutes 12 seconds"
    assert timers.remaining_phrase(60) == "1 minute"
    assert timers.remaining_phrase(30) == "30 seconds"


def test_recurrence_phrase() -> None:
    assert timers.recurrence_phrase("none") == ""
    assert timers.recurrence_phrase("daily") == " every day"
    assert timers.recurrence_phrase("weekdays") == " every weekday"
    assert timers.recurrence_phrase("weekly:0") == " every Monday"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_timers.py -v`
Expected: new tests FAIL with `AttributeError: ... 'duration_noun'`.

- [ ] **Step 3: Write the implementation**

Append to `router/timers.py` (add `from timeparse import LOCAL_TZ` to the imports):

```python
_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _duration_parts(seconds: int) -> tuple[int, str]:
    if seconds % 3600 == 0:
        return seconds // 3600, "hour"
    if seconds % 60 == 0:
        return seconds // 60, "minute"
    return seconds, "second"


def duration_noun(seconds: int) -> str:
    n, unit = _duration_parts(seconds)
    return f"{n} {unit}" + ("s" if n != 1 else "")


def duration_adj(seconds: int) -> str:
    n, unit = _duration_parts(seconds)
    return f"{n}-{unit}"


def clock_phrase(dt_local: datetime) -> str:
    h, m = dt_local.hour, dt_local.minute
    if (h, m) == (12, 0):
        return "noon"
    if (h, m) == (0, 0):
        return "midnight"
    mer = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12} {mer}" if m == 0 else f"{h12}:{m:02d} {mer}"


def recurrence_phrase(recurrence: str) -> str:
    if recurrence == "none":
        return ""
    if recurrence == "daily":
        return " every day"
    if recurrence == "weekdays":
        return " every weekday"
    if recurrence == "weekends":
        return " every weekend"
    return f" every {_DAY_NAMES[int(recurrence.split(':', 1)[1])]}"


def remaining_phrase(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s or not parts:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts)


def announcement_speech(t: Timer) -> str:
    if t.kind == "timer":
        return f"Your {duration_adj(t.duration_seconds or 0)} timer is done."
    if t.kind == "alarm":
        return f"It's {clock_phrase(t.fire_at_dt.astimezone(LOCAL_TZ))}. This is your alarm."
    return f"Reminder: {t.text}."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_timers.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check router/ && python3 -m pytest
git add router/timers.py router/tests/test_timers.py
git commit -m "feat(timers): spoken-phrase helpers and announcement speech"
```

---

### Task 6: timers — Scheduler (fire → repeat → ack), recovery, handle_timer

**Files:**
- Modify: `router/timers.py`
- Test: `router/tests/test_timers.py`

**Interfaces:**
- Consumes: `TimerStore`, phrase helpers, `timeparse.parse`, `timeparse.next_occurrence`.
- Produces:
  - `Scheduler(store, announce, *, interval_s=ANNOUNCE_INTERVAL_S, max_repeats=ANNOUNCE_MAX_REPEATS)` where `announce: Callable[[str, str, int], Awaitable[None]]` receives `(event_id, speech, repeat_n)`; attributes/methods: `wake: asyncio.Event`, `def ack(event_id: str) -> None`, `async run() -> None` (runs until cancelled).
  - `async recover(store: TimerStore) -> None` — restart recovery per spec.
  - `async handle_timer(verb: str, text: str, store: TimerStore, scheduler: Scheduler) -> str` — the entry `router.py` dispatches to (Task 9 wires it).
  - `CLARIFICATION = "Sorry, I didn't catch that. Try something like: set a timer for ten minutes."`

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_timers.py`:

```python
import asyncio
from datetime import timedelta


class Announcer:
    """Records announce calls; can auto-ack after N calls."""

    def __init__(self, scheduler_ref: dict, ack_after: int | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._scheduler_ref = scheduler_ref
        self._ack_after = ack_after

    async def __call__(self, event_id: str, speech: str, repeat_n: int) -> None:
        self.calls.append((event_id, speech, repeat_n))
        if self._ack_after is not None and len(self.calls) >= self._ack_after:
            self._scheduler_ref["s"].ack(event_id)


async def _run_until(scheduler: timers.Scheduler, predicate, timeout: float = 2.0) -> None:
    task = asyncio.create_task(scheduler.run())
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_fire_repeats_until_max_then_done(store: timers.TimerStore) -> None:
    ref: dict = {}
    ann = Announcer(ref)
    s = timers.Scheduler(store, ann, interval_s=0.02, max_repeats=3)
    ref["s"] = s
    t = await store.add(kind="timer", fire_at=timers.utcnow(), duration_seconds=60)
    await _run_until(s, lambda: len(ann.calls) >= 3)
    await asyncio.sleep(0.05)
    assert [c[2] for c in ann.calls[:3]] == [1, 2, 3]
    assert (await store.get(t.id)).status == "done"


async def test_ack_stops_repeats(store: timers.TimerStore) -> None:
    ref: dict = {}
    ann = Announcer(ref, ack_after=1)
    s = timers.Scheduler(store, ann, interval_s=0.02, max_repeats=10)
    ref["s"] = s
    t = await store.add(kind="timer", fire_at=timers.utcnow(), duration_seconds=60)
    await _run_until(s, lambda: bool(ann.calls))
    await asyncio.sleep(0.1)
    assert len(ann.calls) == 1
    assert (await store.get(t.id)).status == "done"


async def test_recurring_rearms_after_ack(store: timers.TimerStore) -> None:
    ref: dict = {}
    ann = Announcer(ref, ack_after=1)
    s = timers.Scheduler(store, ann, interval_s=0.02, max_repeats=10)
    ref["s"] = s
    t = await store.add(kind="alarm", fire_at=timers.utcnow(), recurrence="daily")
    await _run_until(s, lambda: bool(ann.calls))
    await asyncio.sleep(0.05)
    got = await store.get(t.id)
    assert got.status == "armed"
    assert got.fire_at_dt > timers.utcnow()


async def test_wake_event_reevaluates_earlier_timer(store: timers.TimerStore) -> None:
    ref: dict = {}
    ann = Announcer(ref, ack_after=1)
    s = timers.Scheduler(store, ann, interval_s=0.02, max_repeats=1)
    ref["s"] = s
    await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(hours=1))
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0.05)  # scheduler now sleeping ~1 h
    await store.add(kind="timer", fire_at=timers.utcnow(), duration_seconds=60)
    s.wake.set()
    async with asyncio.timeout(2):
        while not ann.calls:
            await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --- restart recovery (spec: exit criterion 1) ---

async def test_recover_fresh_pastdue_stays_armed(store: timers.TimerStore) -> None:
    t = await store.add(kind="timer", fire_at=timers.utcnow() - timedelta(seconds=10),
                        duration_seconds=60)
    await timers.recover(store)
    assert (await store.get(t.id)).status == "armed"


async def test_recover_stale_oneshot_marked_done(store: timers.TimerStore) -> None:
    t = await store.add(kind="timer",
                        fire_at=timers.utcnow() - timedelta(seconds=timers.MISSED_GRACE_S + 60),
                        duration_seconds=60)
    await timers.recover(store)
    assert (await store.get(t.id)).status == "done"


async def test_recover_stale_recurring_rearmed_future(store: timers.TimerStore) -> None:
    t = await store.add(kind="alarm",
                        fire_at=timers.utcnow() - timedelta(days=1), recurrence="daily")
    await timers.recover(store)
    got = await store.get(t.id)
    assert got.status == "armed"
    assert got.fire_at_dt > timers.utcnow()


async def test_recover_stuck_firing_treated_as_pastdue(store: timers.TimerStore) -> None:
    t = await store.add(kind="timer", fire_at=timers.utcnow() - timedelta(seconds=5),
                        duration_seconds=60)
    await store.set_status(t.id, "firing")
    await timers.recover(store)
    assert (await store.get(t.id)).status == "armed"


# --- handle_timer glue ---

class StubScheduler:
    def __init__(self) -> None:
        self.wake = asyncio.Event()


async def test_handle_timer_set_confirms_and_persists(store: timers.TimerStore) -> None:
    sched = StubScheduler()
    speech = await timers.handle_timer("set", "set a timer for 10 minutes", store, sched)
    assert speech == "Timer set for 10 minutes."
    assert sched.wake.is_set()
    assert len(await store.by_status("armed")) == 1


async def test_handle_timer_set_alarm_recurring(store: timers.TimerStore) -> None:
    speech = await timers.handle_timer(
        "set", "set an alarm for 7 am every weekday", store, StubScheduler()
    )
    assert speech == "Alarm set for 7 am every weekday."


async def test_handle_timer_unparseable_clarifies_no_state(store: timers.TimerStore) -> None:
    speech = await timers.handle_timer("set", "set a timer for the pasta", store, StubScheduler())
    assert speech == timers.CLARIFICATION
    assert await store.by_status("armed") == []


async def test_handle_timer_cancel_soonest_says_which(store: timers.TimerStore) -> None:
    await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(minutes=20),
                    duration_seconds=1200)
    await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(minutes=5),
                    duration_seconds=300)
    speech = await timers.handle_timer("cancel", "cancel the timer", store, StubScheduler())
    assert speech == "Cancelled your 5-minute timer."
    assert len(await store.by_status("armed")) == 1


async def test_handle_timer_cancel_all(store: timers.TimerStore) -> None:
    await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(minutes=5),
                    duration_seconds=300)
    await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(minutes=9),
                    duration_seconds=540)
    speech = await timers.handle_timer("cancel", "cancel all my timers", store, StubScheduler())
    assert speech == "Cancelled 2 timers."
    assert await store.by_status("armed") == []


async def test_handle_timer_cancel_none(store: timers.TimerStore) -> None:
    speech = await timers.handle_timer("cancel", "cancel the timer", store, StubScheduler())
    assert speech == "You don't have any timers."


async def test_handle_timer_query_lists_remaining(store: timers.TimerStore) -> None:
    await store.add(kind="timer", fire_at=timers.utcnow() + timedelta(seconds=300),
                    duration_seconds=600)
    speech = await timers.handle_timer("query", "how long is left on my timer", store,
                                       StubScheduler())
    assert speech.startswith("Your 10-minute timer has ")
    assert speech.endswith(" left.")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_timers.py -v`
Expected: new tests FAIL with `AttributeError: ... 'Scheduler'`.

- [ ] **Step 3: Write the implementation**

Append to `router/timers.py` (extend imports: `import asyncio`, `from datetime import timedelta`, `from typing import Awaitable, Callable`, and `import timeparse`):

```python
AnnounceFn = Callable[[str, str, int], Awaitable[None]]

CLARIFICATION = "Sorry, I didn't catch that. Try something like: set a timer for ten minutes."


class Scheduler:
    """One asyncio task: sleep until the earliest armed fire_at; wake early on
    self.wake (set whenever a timer is created/cancelled). On fire: announce,
    repeat every interval_s up to max_repeats, stop on ack."""

    def __init__(
        self,
        store: TimerStore,
        announce: AnnounceFn,
        *,
        interval_s: float = ANNOUNCE_INTERVAL_S,
        max_repeats: int = ANNOUNCE_MAX_REPEATS,
    ) -> None:
        self._store = store
        self._announce = announce
        self._interval_s = interval_s
        self._max_repeats = max_repeats
        self.wake = asyncio.Event()
        self._acks: dict[str, asyncio.Event] = {}

    def ack(self, event_id: str) -> None:
        ev = self._acks.get(event_id)
        if ev is not None:
            ev.set()

    async def run(self) -> None:
        while True:
            nxt = await self._store.next_armed()
            if nxt is None:
                await self.wake.wait()
                self.wake.clear()
                continue
            delay = (nxt.fire_at_dt - utcnow()).total_seconds()
            if delay > 0:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                    self.wake.clear()
                    continue  # store changed — re-evaluate earliest
                except TimeoutError:
                    pass
            await self._fire(nxt)

    async def _fire(self, t: Timer) -> None:
        await self._store.set_status(t.id, "firing")
        speech = announcement_speech(t)
        ev = asyncio.Event()
        self._acks[t.id] = ev
        try:
            for n in range(1, self._max_repeats + 1):
                await self._announce(t.id, speech, n)
                try:
                    await asyncio.wait_for(ev.wait(), timeout=self._interval_s)
                    break
                except TimeoutError:
                    continue
        finally:
            self._acks.pop(t.id, None)
        await self._resolve(t)

    async def _resolve(self, t: Timer) -> None:
        if t.recurrence == "none":
            await self._store.set_status(t.id, "done")
            return
        local = t.fire_at_dt.astimezone(LOCAL_TZ)
        nxt = timeparse.next_occurrence(
            after=utcnow().astimezone(LOCAL_TZ),
            hour=local.hour, minute=local.minute, recurrence=t.recurrence,
        )
        await self._store.rearm(t.id, nxt)


async def recover(store: TimerStore) -> None:
    """Restart recovery (spec): items stuck in 'firing' are treated as past-due
    armed. Past-due armed: < MISSED_GRACE_S stale -> leave armed (run loop fires
    immediately); older one-shots -> done (logged); older recurring -> re-arm."""
    for t in await store.by_status("firing"):
        await store.set_status(t.id, "armed")
    now = utcnow()
    for t in await store.by_status("armed"):
        overdue = (now - t.fire_at_dt).total_seconds()
        if overdue <= MISSED_GRACE_S:
            continue
        if t.recurrence == "none":
            log.info("recovery: %s %s missed by %.0fs — marking done", t.kind, t.id, overdue)
            await store.set_status(t.id, "done")
        else:
            local = t.fire_at_dt.astimezone(LOCAL_TZ)
            nxt = timeparse.next_occurrence(
                after=now.astimezone(LOCAL_TZ),
                hour=local.hour, minute=local.minute, recurrence=t.recurrence,
            )
            await store.rearm(t.id, nxt)


def _describe(t: Timer) -> str:
    if t.kind == "timer":
        return f"{duration_adj(t.duration_seconds or 0)} timer"
    if t.kind == "alarm":
        return f"{clock_phrase(t.fire_at_dt.astimezone(LOCAL_TZ))} alarm"
    return f"reminder to {t.text}"


async def handle_timer(verb: str, text: str, store: TimerStore, scheduler) -> str:
    """Dispatch target for action type 'timer' (spec Component 4)."""
    now_local = utcnow().astimezone(LOCAL_TZ)
    parsed = timeparse.parse(text, now=now_local)
    if parsed is None or parsed.verb != verb:
        return CLARIFICATION

    if verb == "set":
        if parsed.kind == "timer":
            fire = utcnow() + timedelta(seconds=parsed.duration_seconds)
            await store.add(kind="timer", fire_at=fire,
                            duration_seconds=parsed.duration_seconds)
            scheduler.wake.set()
            return f"Timer set for {duration_noun(parsed.duration_seconds)}."
        if parsed.kind == "alarm":
            await store.add(kind="alarm", fire_at=parsed.fire_at, recurrence=parsed.recurrence)
            scheduler.wake.set()
            return (f"Alarm set for {clock_phrase(parsed.fire_at)}"
                    f"{recurrence_phrase(parsed.recurrence)}.")
        fire = parsed.fire_at or (utcnow() + timedelta(seconds=parsed.duration_seconds))
        await store.add(kind="reminder", fire_at=fire, text=parsed.text,
                        recurrence=parsed.recurrence)
        scheduler.wake.set()
        when = (f"at {clock_phrase(parsed.fire_at)}" if parsed.fire_at
                else f"in {duration_noun(parsed.duration_seconds)}")
        return f"Okay, I'll remind you to {parsed.text} {when}{recurrence_phrase(parsed.recurrence)}."

    kind = parsed.kind or "timer"
    matching = [t for t in await store.by_status("armed") if t.kind == kind]
    if parsed.at_qualifier is not None:
        h, m = parsed.at_qualifier
        matching = [
            t for t in matching
            if (lambda lt: (lt.hour % 12, lt.minute) == (h % 12, m))(
                t.fire_at_dt.astimezone(LOCAL_TZ))
        ]
    if not matching:
        return f"You don't have any {kind}s."

    if verb == "cancel":
        if parsed.cancel_all:
            for t in matching:
                await store.set_status(t.id, "cancelled")
            scheduler.wake.set()
            n = len(matching)
            return f"Cancelled {n} {kind}" + ("s." if n != 1 else ".")
        soonest = matching[0]  # by_status orders by fire_at
        await store.set_status(soonest.id, "cancelled")
        scheduler.wake.set()
        return f"Cancelled your {_describe(soonest)}."

    # query
    parts: list[str] = []
    for t in matching:
        if t.kind == "timer":
            left = (t.fire_at_dt - utcnow()).total_seconds()
            parts.append(f"Your {duration_adj(t.duration_seconds or 0)} timer has "
                         f"{remaining_phrase(left)} left.")
        elif t.kind == "alarm":
            parts.append(f"Alarm at {clock_phrase(t.fire_at_dt.astimezone(LOCAL_TZ))}"
                         f"{recurrence_phrase(t.recurrence)}.")
        else:
            parts.append(f"Reminder to {t.text} at "
                         f"{clock_phrase(t.fire_at_dt.astimezone(LOCAL_TZ))}.")
    return " ".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_timers.py -v`
Expected: all PASS. These are timing-sensitive tests with shrunk intervals — if `test_fire_repeats_until_max_then_done` flakes, raise the `_run_until` timeout, not the asserts.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check router/ && python3 -m pytest
git add router/timers.py router/tests/test_timers.py
git commit -m "feat(timers): scheduler with repeat-until-ack, restart recovery, handle_timer"
```

---

### Task 7: push — websocket /events channel

**Files:**
- Create: `router/push.py`
- Test: `router/tests/test_push.py`

**Interfaces:**
- Consumes: nothing from other new modules (ack handler injected).
- Produces: `PushChannel` with `set_ack_handler(cb: Callable[[str], None])`, `async handle(ws: WebSocket)` (accept → register → read acks/pongs → unregister), `async broadcast_announce(event_id: str, speech: str, repeat_n: int)`, `async ping_loop()`, `PING_INTERVAL_S = 20`. Frame shapes exactly per spec Component 2.

- [ ] **Step 1: Write the failing tests**

Create `router/tests/test_push.py`:

```python
"""Push channel tests — TestClient websocket (spec §Testing)."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

import push


@pytest.fixture
def channel() -> push.PushChannel:
    return push.PushChannel()


@pytest.fixture
def app(channel: push.PushChannel) -> FastAPI:
    app = FastAPI()

    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        await channel.handle(ws)

    @app.post("/announce")
    async def announce() -> dict:
        await channel.broadcast_announce("ev-1", "Your 2-minute timer is done.", 1)
        return {}

    return app


def test_announce_delivered_to_connected_satellite(app: FastAPI, channel) -> None:
    client = TestClient(app)
    with client.websocket_connect("/events") as ws:
        client.post("/announce")
        msg = ws.receive_json()
    assert msg == {"type": "announce", "event_id": "ev-1",
                   "speech": "Your 2-minute timer is done.", "repeat_n": 1}


def test_ack_roundtrip_invokes_handler(app: FastAPI, channel) -> None:
    acked: list[str] = []
    channel.set_ack_handler(acked.append)
    client = TestClient(app)
    with client.websocket_connect("/events") as ws:
        client.post("/announce")
        ws.receive_json()
        ws.send_json({"type": "ack", "event_id": "ev-1"})
        client.post("/announce")  # server work forces the ack frame to be processed
        ws.receive_json()
    assert acked == ["ev-1"]


def test_broadcast_reaches_all_satellites(app: FastAPI, channel) -> None:
    client = TestClient(app)
    with client.websocket_connect("/events") as ws1, client.websocket_connect("/events") as ws2:
        client.post("/announce")
        assert ws1.receive_json()["event_id"] == "ev-1"
        assert ws2.receive_json()["event_id"] == "ev-1"


def test_zero_connected_satellites_is_not_an_error(app: FastAPI, channel) -> None:
    client = TestClient(app)
    resp = client.post("/announce")  # no ws connected
    assert resp.status_code == 200


async def test_ping_loop_sends_ping_frames(channel) -> None:
    import asyncio
    import json

    sent: list[str] = []

    class FakeWs:
        async def send_text(self, frame: str) -> None:
            sent.append(frame)

    channel._conns.add(FakeWs())  # type: ignore[arg-type]
    task = asyncio.create_task(channel.ping_loop(interval_s=0.02))
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert sent
    assert all(json.loads(f) == {"type": "ping"} for f in sent)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_push.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'push'`.

- [ ] **Step 3: Write the implementation**

Create `router/push.py`:

```python
"""Websocket push channel — router endpoint side (spec Component 2).

Satellites hold one connection each. Announcements broadcast to ALL
connected satellites; an ack from any one is forwarded to the scheduler.
Zero connected satellites is not an error."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("push")

PING_INTERVAL_S = 20


class PushChannel:
    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._on_ack: Callable[[str], None] | None = None

    def set_ack_handler(self, cb: Callable[[str], None]) -> None:
        self._on_ack = cb

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        self._conns.add(ws)
        log.info("satellite connected (%d total)", len(self._conns))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON frame: %r", raw[:80])
                    continue
                if msg.get("type") == "ack" and self._on_ack is not None:
                    self._on_ack(str(msg.get("event_id", "")))
                # pongs and anything else: ignored
        except WebSocketDisconnect:
            pass
        finally:
            self._conns.discard(ws)
            log.info("satellite disconnected (%d total)", len(self._conns))

    async def broadcast_announce(self, event_id: str, speech: str, repeat_n: int) -> None:
        await self._send_all(json.dumps(
            {"type": "announce", "event_id": event_id, "speech": speech, "repeat_n": repeat_n}
        ))

    async def ping_loop(self, interval_s: float = PING_INTERVAL_S) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self._send_all(json.dumps({"type": "ping"}))

    async def _send_all(self, frame: str) -> None:
        for ws in list(self._conns):
            try:
                await ws.send_text(frame)
            except Exception:
                self._conns.discard(ws)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_push.py -v`
Expected: all PASS. If `test_ack_roundtrip_invokes_handler` is flaky under TestClient's portal, replace the second announce with `ws.send_json` followed by closing the context and asserting after exit — the ack must still be recorded.

- [ ] **Step 5: Lint and commit**

```bash
ruff check router/ && python3 -m pytest
git add router/push.py router/tests/test_push.py
git commit -m "feat(push): ws /events channel with broadcast, ack routing, keepalive"
```

---

### Task 8: router — lifespan, shared clients (carry-over), test-client migration

**Files:**
- Modify: `router/router.py`
- Modify: `router/tests/test_router.py`

**Interfaces:**
- Consumes: `TimerStore`, `Scheduler`, `recover` (timers), `PushChannel` (push).
- Produces: `app` with lifespan; `app.state.http: httpx.AsyncClient`, `app.state.anthropic: AsyncAnthropic`, `app.state.store`, `app.state.scheduler`, `app.state.push`; new signatures `run_device_action(intent, match, http)`, `ask_llm(text, anthropic)`; env `TIMERS_DB` overrides the DB path (tests use tmp dirs). `/events` websocket endpoint.

- [ ] **Step 1: Write/adjust the failing tests**

In `router/tests/test_router.py`:

1. Delete the module-level `client = TestClient(router.app)` line.
2. Add fixtures at the top (after the imports):

```python
@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Lifespan-aware test client with an isolated timer DB."""
    monkeypatch.setenv("TIMERS_DB", str(tmp_path / "timers.db"))
    with TestClient(router.app) as c:
        yield c
```

3. Add `client` as a parameter to every test that uses it: `test_unmatched_text_falls_back_to_llm`, `test_knowledge_intent_without_open_brain_falls_back_to_llm`, `test_knowledge_intent_with_open_brain_routes_there`, `test_device_intent_dispatches_to_device_action`, `test_llm_failure_returns_spoken_error_not_500`, `test_device_action_http_error_returns_spoken_error_not_success`.
4. Update the direct-call device test to pass a client:

```python
@respx.mock
async def test_run_device_action_calls_url_with_slots() -> None:
    route_mock = respx.post("http://lights.local/api/kitchen/on").mock(return_value=Response(200))
    matched = router.match_intent("turn on the kitchen lights")
    assert matched is not None
    intent, match = matched
    async with httpx.AsyncClient() as http:
        speech = await router.run_device_action(intent, match, http)
    assert route_mock.called
    assert speech == "Okay, kitchen lights on."
```

(add `import httpx` to the test file imports)

5. Add new lifespan tests:

```python
def test_lifespan_creates_shared_clients_and_scheduler(client) -> None:
    assert isinstance(router.app.state.http, httpx.AsyncClient)
    assert router.app.state.store is not None
    assert router.app.state.scheduler is not None


def test_events_websocket_accepts_connection(client) -> None:
    with client.websocket_connect("/events"):
        pass  # connect + clean close is the assertion
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_router.py -v`
Expected: FAIL — `run_device_action()` takes 2 positional args; `app.state.http` missing; `/events` 403/404.

- [ ] **Step 3: Write the implementation**

In `router/router.py`:

1. Replace the imports block and app creation with:

```python
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request, WebSocket
from pydantic import BaseModel

import push as push_mod
import timers

OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "")
OPEN_BRAIN_API_KEY = os.environ.get("OPEN_BRAIN_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")
```

2. Keep `SYSTEM_PROMPT`, `ERROR_SPEECH`, `log`, `INTENTS` load, `Utterance`, `match_intent` as they are, then add the lifespan and replace `app = FastAPI()`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=30)
    # Tests never call the real API (ask_llm is mocked); real runs source .env.
    app.state.anthropic = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "unset"))
    store = timers.TimerStore(os.environ.get("TIMERS_DB", str(Path(__file__).with_name("timers.db"))))
    await store.open()
    await timers.recover(store)
    channel = push_mod.PushChannel()
    scheduler = timers.Scheduler(store, channel.broadcast_announce)
    channel.set_ack_handler(scheduler.ack)
    app.state.store, app.state.push, app.state.scheduler = store, channel, scheduler
    tasks = [asyncio.create_task(scheduler.run()), asyncio.create_task(channel.ping_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.close()
        await app.state.http.aclose()
        await app.state.anthropic.close()


app = FastAPI(lifespan=lifespan)
```

(move the `INTENTS = ...` line above `app = FastAPI(lifespan=lifespan)` if needed — order: constants, log, INTENTS, models, lifespan, app.)

3. Change the executors to take shared clients (per-call `AsyncClient`/`AsyncAnthropic` construction is removed — this is the carry-over fix):

```python
async def run_device_action(intent: dict, match: re.Match, http: httpx.AsyncClient) -> str:
    """Call a device API directly. The URL/body can use {slot} groups from the regex."""
    action = intent["action"]
    url = action["url"].format(**match.groupdict())
    resp = await http.request(action.get("method", "POST"), url, json=action.get("json"))
    resp.raise_for_status()
    return intent.get("response", "Done.").format(**match.groupdict())


async def ask_llm(text: str, anthropic: AsyncAnthropic) -> str:
    msg = await anthropic.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text
```

4. Update `route()` to pass state (leave `ask_open_brain` with its current body for now — Task 10 rewrites it; give it the new signature `async def ask_open_brain(query: str, http: httpx.AsyncClient, anthropic: AsyncAnthropic) -> str` and change its internal `async with httpx.AsyncClient(...)` usage to `resp = await http.post(...)`):

```python
@app.post("/route")
async def route(utt: Utterance, request: Request) -> dict:
    text = utt.text.strip()
    state = request.app.state
    try:
        matched = match_intent(text)
        if matched:
            intent, match = matched
            kind = intent["action"]["type"]
            if kind == "device":
                return {"speech": await run_device_action(intent, match, state.http),
                        "intent": intent["name"]}
            if kind == "open_brain" and OPEN_BRAIN_URL:
                return {"speech": await ask_open_brain(text, state.http, state.anthropic),
                        "intent": intent["name"]}
        return {"speech": await ask_llm(text, state.anthropic), "intent": "llm_fallback"}
    except Exception:
        log.exception("routing failed for %r", text)
        return {"speech": ERROR_SPEECH, "intent": "error"}
```

5. Add the websocket endpoint at the bottom:

```python
@app.websocket("/events")
async def events(ws: WebSocket) -> None:
    await ws.app.state.push.handle(ws)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/ -v`
Expected: all PASS (existing dispatch/error tests unchanged in behavior).

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python3 -m pytest
git add router/router.py router/tests/test_router.py
git commit -m "feat(router): lifespan-owned shared clients, timer store/scheduler wiring, ws /events"
```

---

### Task 9: router — intents.yaml rewrite, startup validation, timer dispatch, collision tests

**Files:**
- Modify: `router/intents.yaml`
- Modify: `router/router.py`
- Test: `router/tests/test_router.py`

**Interfaces:**
- Consumes: `timers.handle_timer` (Task 6).
- Produces: `validate_intents(intents: list[dict]) -> None` (raises `RuntimeError`), `HANDLED_TYPES = {"device", "open_brain", "timer"}`, `TIMER_VERBS = {"set", "cancel", "query"}`; intent names `timer_set`, `timer_cancel`, `timer_query` dispatching to `handle_timer`; unknown-type guard returning `{"speech": "I don't know how to do that yet.", "intent": "unsupported"}`.

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_router.py`:

```python
# --- intent-matching collision tests (spec §Testing, audit 2026-07-15) ---

@pytest.mark.parametrize(
    ("text", "expected_intent"),
    [
        ("what did I decide about the kitchen lights on the porch", "knowledge_query"),
        ("check my notes about office lights off schedule", "knowledge_query"),
        ("remind me to turn on the porch lights at 8", "timer_set"),
        ("remind me to check my notes at 8 pm", "timer_set"),
        ("set a timer for 10 minutes", "timer_set"),
        ("cancel all my timers", "timer_cancel"),
        ("how long is left on my timer", "timer_query"),
        ("turn on the kitchen lights", "lights_on"),
        ("kitchen lights off", "lights_off"),
    ],
)
def test_intent_routing_precision(text: str, expected_intent: str) -> None:
    matched = router.match_intent(text)
    assert matched is not None, f"expected {expected_intent}, got no match"
    assert matched[0]["name"] == expected_intent


@pytest.mark.parametrize(
    "text",
    [
        "don't turn on the kitchen lights",                      # negation -> LLM
        "The lights on my dashboard are red, what does that mean",  # article slot -> LLM
        "turn on the living room lights",                         # multi-word room (known gap)
    ],
)
def test_non_commands_fall_through_to_llm(text: str) -> None:
    assert router.match_intent(text) is None


# --- dispatch guard (spec: Matching precision & dispatch safety) ---

def test_validate_intents_rejects_unknown_type() -> None:
    bad = [{"name": "x", "patterns": ["^x$"], "action": {"type": "bogus"}}]
    with pytest.raises(RuntimeError, match="unhandled action type"):
        router.validate_intents(bad)


def test_validate_intents_rejects_unknown_timer_verb() -> None:
    bad = [{"name": "x", "patterns": ["^x$"], "action": {"type": "timer", "verb": "snooze"}}]
    with pytest.raises(RuntimeError, match="unhandled timer verb"):
        router.validate_intents(bad)


def test_unknown_action_type_speaks_clarification_never_llm(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = AsyncMock(return_value="should never be called")
    monkeypatch.setattr(router, "ask_llm", llm)
    monkeypatch.setattr(
        router, "match_intent",
        lambda text: ({"name": "x", "action": {"type": "bogus"}}, None),
    )
    resp = client.post("/route", json={"text": "whatever"})
    assert resp.json() == {"speech": "I don't know how to do that yet.", "intent": "unsupported"}
    llm.assert_not_awaited()


# --- timer dispatch wiring ---

def test_timer_intent_dispatches_to_handle_timer(client, monkeypatch: pytest.MonkeyPatch) -> None:
    handled: dict = {}

    async def fake_handle(verb, text, store, scheduler):
        handled["args"] = (verb, text)
        return "Timer set for 10 minutes."

    monkeypatch.setattr(router.timers, "handle_timer", fake_handle)
    resp = client.post("/route", json={"text": "set a timer for 10 minutes"})
    assert resp.json() == {"speech": "Timer set for 10 minutes.", "intent": "timer_set"}
    assert handled["args"] == ("set", "set a timer for 10 minutes")


def test_timer_set_end_to_end_through_route(client) -> None:
    resp = client.post("/route", json={"text": "set a timer for 10 minutes"})
    assert resp.json() == {"speech": "Timer set for 10 minutes.", "intent": "timer_set"}
    resp = client.post("/route", json={"text": "how long is left on my timer"})
    assert resp.json()["intent"] == "timer_query"
    assert resp.json()["speech"].startswith("Your 10-minute timer has ")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_router.py -v`
Expected: collision tests FAIL (old un-anchored patterns mis-route), `validate_intents` missing, timer dispatch missing.

- [ ] **Step 3: Replace `router/intents.yaml` with the full new file**

```yaml
# Local intents, checked in order; first regex match wins.
#
# Ordering + anchoring (spec "Matching precision & dispatch safety", 2026-07-15):
# timer intents come BEFORE device intents; every command pattern is anchored
# to the utterance start with an optional politeness prefix, so command
# n-grams inside questions or reminder payloads never hijack routing.
#
# action.type: timer       -> dispatched to timers.handle_timer (verb: set|cancel|query)
#              device      -> HTTP call to a device API, speak `response`
#              open_brain  -> forward the full utterance to Open Brain
# Named groups in patterns are available as {slot} in action.url.
#
# _p is the politeness/filler prefix, inlined in every pattern:
#   (?:please |hey |ok |okay |um |uh )*

intents:
  - name: timer_set
    patterns:
      - "^(?:please |hey |ok |okay |um |uh )*(?:set|start) (?:a |an )?timer\\b"
      - "^(?:please |hey |ok |okay |um |uh )*(?:\\w+[ -])+minute timer\\b"
      - "^(?:please |hey |ok |okay |um |uh )*set (?:a |an )?alarm\\b"
      - "^(?:please |hey |ok |okay |um |uh )*wake me(?: up)? at\\b"
      - "^(?:please |hey |ok |okay |um |uh )*remind me to\\b"
    action:
      type: timer
      verb: set

  - name: timer_cancel
    patterns:
      - "^(?:please |hey |ok |okay |um |uh )*cancel\\b.*\\b(?:timer|alarm|reminder)s?\\b"
    action:
      type: timer
      verb: cancel

  - name: timer_query
    patterns:
      - "^(?:please |hey |ok |okay |um |uh )*how long(?: is)? left\\b"
      - "^(?:please |hey |ok |okay |um |uh )*what (?:timer|alarm|reminder)s?\\b"
    action:
      type: timer
      verb: query

  - name: knowledge_query
    patterns:
      - "^(?:please |hey |ok |okay |um |uh )*what did (?:i|we) (?:decide|say|conclude)\\b"
      - "^(?:please |hey |ok |okay |um |uh )*check (?:my|the) (?:notes|brain)\\b"
    action:
      type: open_brain

  - name: lights_on
    patterns:
      - "^(?:please |hey |ok |okay |um |uh )*turn (?:on|up) the (?P<room>\\w+) lights?\\b"
      - "^(?:please |hey |ok |okay |um |uh )*(?P<room>(?!the\\b|a\\b|an\\b|my\\b|our\\b)\\w+) lights? on\\b"
    action:
      type: device
      method: POST
      # TODO(will): point at the real device API (Hue bridge, Tasmota, etc.)
      url: "http://lights.local/api/{room}/on"
    response: "Okay, {room} lights on."

  - name: lights_off
    patterns:
      - "^(?:please |hey |ok |okay |um |uh )*turn off the (?P<room>\\w+) lights?\\b"
      - "^(?:please |hey |ok |okay |um |uh )*(?P<room>(?!the\\b|a\\b|an\\b|my\\b|our\\b)\\w+) lights? off\\b"
    action:
      type: device
      method: POST
      url: "http://lights.local/api/{room}/off"
    response: "Okay, {room} lights off."
```

- [ ] **Step 4: Add validation + timer dispatch to `router/router.py`**

After the `INTENTS = ...` line:

```python
HANDLED_TYPES = {"device", "open_brain", "timer"}
TIMER_VERBS = {"set", "cancel", "query"}


def validate_intents(intents: list[dict]) -> None:
    """Fail fast at startup if intents.yaml declares an action the router
    can't dispatch (spec: Matching precision & dispatch safety)."""
    for intent in intents:
        action = intent.get("action") or {}
        kind = action.get("type")
        if kind not in HANDLED_TYPES:
            raise RuntimeError(
                f"intents.yaml: intent {intent.get('name')!r} declares "
                f"unhandled action type {kind!r} (handled: {sorted(HANDLED_TYPES)})"
            )
        if kind == "timer" and action.get("verb") not in TIMER_VERBS:
            raise RuntimeError(
                f"intents.yaml: intent {intent.get('name')!r} declares "
                f"unhandled timer verb {action.get('verb')!r} (handled: {sorted(TIMER_VERBS)})"
            )


validate_intents(INTENTS)
```

In `route()`, replace the dispatch block inside `if matched:` with:

```python
            intent, match = matched
            kind = intent["action"]["type"]
            if kind == "device":
                return {"speech": await run_device_action(intent, match, state.http),
                        "intent": intent["name"]}
            if kind == "timer":
                speech = await timers.handle_timer(
                    intent["action"]["verb"], text, state.store, state.scheduler
                )
                return {"speech": speech, "intent": intent["name"]}
            if kind == "open_brain":
                if OPEN_BRAIN_URL:
                    return {"speech": await ask_open_brain(text, state.http, state.anthropic),
                            "intent": intent["name"]}
                # documented degradation: no Open Brain configured -> LLM fallback
            else:
                # unreachable via validate_intents; runtime belt for hand-edited configs
                log.warning("intent %r declares unhandled action type %r",
                            intent["name"], kind)
                return {"speech": "I don't know how to do that yet.", "intent": "unsupported"}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/ -v`
Expected: all PASS, including the pre-existing `test_match_intent_lights_on` (anchored pattern still matches "turn on the kitchen lights").

- [ ] **Step 6: Lint, full suite, commit**

```bash
ruff check . && python3 -m pytest
git add router/intents.yaml router/router.py router/tests/test_router.py
git commit -m "feat(router): timer dispatch, anchored intents, startup validation, unknown-type guard"
```

---

### Task 10: router — ask_open_brain rewrite (real contract + synthesis)

**Files:**
- Modify: `router/router.py`
- Test: `router/tests/test_router.py`

**Interfaces:**
- Consumes: `app.state.http`, `app.state.anthropic` (Task 8).
- Produces: `ask_open_brain(query: str, http: httpx.AsyncClient, anthropic: AsyncAnthropic) -> str` implementing spec Component 5; `SYNTH_PROMPT` constant; `OPEN_BRAIN_API_KEY` module global (added in Task 8).

- [ ] **Step 1: Replace the two old ask_open_brain tests and add the new ones**

In `router/tests/test_router.py`, DELETE `test_ask_open_brain_returns_first_hit_summary` and `test_ask_open_brain_empty_hits`, then append:

```python
def _ob_payload(*sims: float) -> dict:
    return {
        "thoughts": [
            {"content": f"note {i} content", "similarity": s, "id": f"id-{i}",
             "created_at": "2026-07-01T00:00:00Z"}
            for i, s in enumerate(sims)
        ],
        "total": len(sims),
        "query": "q",
    }


class _FakeAnthropic:
    """Duck-typed messages.create capturing the synthesis call."""

    def __init__(self, reply: str) -> None:
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                from types import SimpleNamespace
                return SimpleNamespace(content=[SimpleNamespace(text=reply)])

        self.messages = _Messages()


@respx.mock
async def test_ask_open_brain_synthesizes_from_filtered_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "OPEN_BRAIN_API_KEY", "test-key")
    search = respx.post("http://ob.local/api/search").mock(
        return_value=Response(200, json=_ob_payload(0.9, 0.6, 0.3))
    )
    fake = _FakeAnthropic("You picked the ND91-4 driver.")
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("speaker choice", http, fake)

    assert speech == "You picked the ND91-4 driver."
    req = search.calls[0].request
    assert req.headers["X-API-Key"] == "test-key"
    import json as _json
    assert _json.loads(req.content) == {"query": "speaker choice", "limit": 5}
    prompt_text = fake.calls[0]["messages"][0]["content"]
    assert "note 0 content" in prompt_text and "note 1 content" in prompt_text
    assert "note 2 content" not in prompt_text  # similarity 0.3 < 0.55 filtered


@respx.mock
async def test_ask_open_brain_no_hits_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(
        return_value=Response(200, json=_ob_payload(0.2))
    )
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("nothing", http, _FakeAnthropic("x"))
    assert speech == "I didn't find anything about that."


@respx.mock
async def test_ask_open_brain_unreachable_distinct_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("anything", http, _FakeAnthropic("x"))
    assert speech == "I couldn't reach my notes."


@respx.mock
async def test_ask_open_brain_401_treated_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(return_value=Response(401))
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("anything", http, _FakeAnthropic("x"))
    assert speech == "I couldn't reach my notes."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_router.py -v`
Expected: new tests FAIL (old body posts to `/search`, no header, expects a list).

- [ ] **Step 3: Write the implementation**

In `router/router.py`, replace `ask_open_brain` entirely (and add the constant near `SYSTEM_PROMPT`):

```python
SYNTH_PROMPT = (
    "Answer the question in one or two short spoken sentences using only "
    "these notes; if the notes don't answer it, say so."
)


async def ask_open_brain(query: str, http: httpx.AsyncClient, anthropic: AsyncAnthropic) -> str:
    """Spec Component 5: search Open Brain, filter by similarity, synthesize
    a spoken answer with one Claude call. 'Found nothing' and 'couldn't
    reach' are deliberately distinct spoken outcomes."""
    try:
        resp = await http.post(
            f"{OPEN_BRAIN_URL}/api/search",
            headers={"X-API-Key": OPEN_BRAIN_API_KEY},
            json={"query": query, "limit": 5},
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        log.exception("open brain search failed")
        return "I couldn't reach my notes."
    hits = [t for t in resp.json().get("thoughts", []) if t.get("similarity", 0) >= 0.55]
    if not hits:
        return "I didn't find anything about that."
    notes = "\n\n".join(t["content"] for t in hits)
    msg = await anthropic.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYNTH_PROMPT,
        messages=[{"role": "user", "content": f"Notes:\n{notes}\n\nQuestion: {query}"}],
    )
    return msg.content[0].text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python3 -m pytest
git add router/router.py router/tests/test_router.py
git commit -m "feat(router): ask_open_brain implements the real /api/search contract with synthesis"
```

---

### Task 11: STT hardening — server error frame + client timeout (carry-over pair)

**Files:**
- Modify: `server/stt_server.py`
- Modify: `satellite/pipeline.py`
- Test: `server/tests/test_stt_server.py`, `satellite/tests/test_pipeline.py`

**Interfaces:**
- Consumes: existing ws protocol.
- Produces: server replies `{"text": "", "error": "<exception class>"}` on transcription failure; `pipeline.STT_TIMEOUT_S = 60`; `transcribe` raises `RuntimeError` on an error reply and `TimeoutError` on deadline.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_stt_server.py`:

```python
def test_stt_transcription_failure_sends_error_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(audio, **kwargs):
        raise ValueError("bad buffer")

    monkeypatch.setattr(stt_server, "get_model",
                        lambda: SimpleNamespace(transcribe=boom))
    client = TestClient(stt_server.app)
    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(np.zeros(10, dtype=np.int16).tobytes())
        ws.send_text("END")
        reply = ws.receive_json()
    assert reply == {"text": "", "error": "ValueError"}
```

Append to `satellite/tests/test_pipeline.py`:

```python
import pytest


async def test_transcribe_raises_on_error_frame() -> None:
    async def handler(ws) -> None:  # noqa: ANN001
        async for msg in ws:
            if msg == "END":
                await ws.send(json.dumps({"text": "", "error": "ValueError"}))
                break

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(RuntimeError, match="STT server error: ValueError"):
            await pipeline.transcribe(b"\x01\x02", f"ws://127.0.0.1:{port}/stt")


async def test_transcribe_times_out_on_silent_server(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(ws) -> None:  # noqa: ANN001
        async for _ in ws:
            pass  # never replies

    monkeypatch.setattr(pipeline, "STT_TIMEOUT_S", 0.2)
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(TimeoutError):
            await pipeline.transcribe(b"\x01\x02", f"ws://127.0.0.1:{port}/stt")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest server/tests/ satellite/tests/test_pipeline.py -v`
Expected: server test FAILs (connection closes with no reply → receive_json raises); pipeline tests FAIL (`KeyError: 'text'` path / no timeout).

- [ ] **Step 3: Write the implementations**

`server/stt_server.py` — add `import logging` and `log = logging.getLogger("stt_server")` near the top, then replace everything after the receive loop (from `audio = ...` to the end of the handler) with:

```python
    try:
        audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = await asyncio.to_thread(
            lambda: get_model().transcribe(audio, language="en", vad_filter=True)
        )
        text = " ".join(seg.text.strip() for seg in segments)
    except Exception as exc:
        # Spec (carry-over, 2026-07-15): never close with no reply — the client
        # pairs this with its 60 s deadline and fails the turn immediately.
        log.exception("transcription failed")
        await ws.send_json({"text": "", "error": type(exc).__name__})
        await ws.close()
        return
    await ws.send_json({"text": text})
    await ws.close()
```

`satellite/pipeline.py` — add `import asyncio` to the imports, add `STT_TIMEOUT_S = 60` under `FRAME_SAMPLES`, and replace `transcribe` with:

```python
async def transcribe(audio: bytes, stt_url: str) -> str:
    """Stream PCM chunks to the STT server, get the transcript back.

    Bounded by STT_TIMEOUT_S (spec carry-over: a wedged STT server must not
    hang a satellite forever). An error reply from the server raises
    RuntimeError immediately instead of stalling to the deadline."""
    async with asyncio.timeout(STT_TIMEOUT_S):
        async with websockets.connect(stt_url, max_size=None) as ws:
            chunk = FRAME_SAMPLES * 2 * 10  # ~0.8 s per chunk
            for i in range(0, len(audio), chunk):
                await ws.send(audio[i : i + chunk])
            await ws.send("END")
            reply = json.loads(await ws.recv())
    if reply.get("error"):
        raise RuntimeError(f"STT server error: {reply['error']}")
    return reply["text"].strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest server/tests/ satellite/tests/ -v`
Expected: all PASS (including the original round-trip tests).

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python3 -m pytest
git add server/stt_server.py satellite/pipeline.py server/tests/test_stt_server.py satellite/tests/test_pipeline.py
git commit -m "feat(stt): error frame on transcription failure + client-side 60s deadline"
```

---

### Task 12: fake_satellite — EventListener thread, ack path, EOF exit, py_compile smoke

**Files:**
- Modify: `satellite/fake_satellite.py`
- Test: `satellite/tests/test_fake_satellite.py`
- Create: `satellite/tests/test_satellite_smoke.py`

**Interfaces:**
- Consumes: ws `/events` protocol frames (spec Component 2).
- Produces: `EventListener(events_url)` with `.start()`, `.stop()`, `.request_ack()`, `.announcement_active: threading.Event`; module constants `EVENTS_URL` (env, default `ws://localhost:8200/events`), `CHIME_CMD = ["afplay", "/System/Library/Sounds/Glass.aiff"]`, `RECONNECT_MIN_S = 1.0`, `RECONNECT_MAX_S = 30.0`. `main()` handles `EOFError` as clean exit.

- [ ] **Step 1: Write the failing tests**

Append to `satellite/tests/test_fake_satellite.py`:

```python
import asyncio
import json
import threading

import websockets


class FakeRouter:
    """Local ws server standing in for the router's /events endpoint."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.connected = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._ws = None
        self.port: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        async def handler(ws) -> None:  # noqa: ANN001
            self._ws = ws
            self.connected.set()
            async for raw in ws:
                self.received.append(json.loads(raw))

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=5)

    def send(self, msg: dict) -> None:
        async def _send() -> None:
            await self._ws.send(json.dumps(msg))

        asyncio.run_coroutine_threadsafe(_send(), self._loop).result(timeout=5)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)


@pytest.fixture
def fake_router():
    r = FakeRouter()
    r.start()
    yield r
    r.stop()


def test_listener_announce_chimes_speaks_and_acks(
    fake_router: FakeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []
    spoken: list[str] = []
    monkeypatch.setattr(fake_satellite.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or MagicMock())
    monkeypatch.setattr(fake_satellite, "speak", spoken.append)

    listener = fake_satellite.EventListener(f"ws://127.0.0.1:{fake_router.port}/events")
    listener.start()
    try:
        assert fake_router.connected.wait(timeout=5)
        fake_router.send({"type": "announce", "event_id": "ev-9",
                          "speech": "Your 2-minute timer is done.", "repeat_n": 1})
        assert listener.announcement_active.wait(timeout=5)
        assert spoken == ["Your 2-minute timer is done."]
        assert ran == [fake_satellite.CHIME_CMD]

        listener.request_ack()
        deadline = threading.Event()
        for _ in range(100):
            if {"type": "ack", "event_id": "ev-9"} in fake_router.received:
                break
            deadline.wait(0.05)
        assert {"type": "ack", "event_id": "ev-9"} in fake_router.received
        assert not listener.announcement_active.is_set()
    finally:
        listener.stop()


def test_listener_replies_pong_to_ping(fake_router: FakeRouter,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_satellite, "speak", lambda s: None)
    monkeypatch.setattr(fake_satellite.subprocess, "run", lambda cmd, **kw: MagicMock())
    listener = fake_satellite.EventListener(f"ws://127.0.0.1:{fake_router.port}/events")
    listener.start()
    try:
        assert fake_router.connected.wait(timeout=5)
        fake_router.send({"type": "ping"})
        for _ in range(100):
            if {"type": "pong"} in fake_router.received:
                break
            threading.Event().wait(0.05)
        assert {"type": "pong"} in fake_router.received
    finally:
        listener.stop()


def test_main_exits_cleanly_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubListener:
        def __init__(self, url: str) -> None:
            self.announcement_active = threading.Event()
            self.stopped = False

        def start(self) -> None: ...

        def stop(self) -> None:
            self.stopped = True

    stub_holder: dict = {}

    def make_stub(url: str) -> StubListener:
        stub_holder["l"] = StubListener(url)
        return stub_holder["l"]

    monkeypatch.setattr(fake_satellite, "EventListener", make_stub)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))
    monkeypatch.setattr("sys.argv", ["fake_satellite.py"])
    fake_satellite.main()  # must return, not busy-loop
    assert stub_holder["l"].stopped is True
```

Create `satellite/tests/test_satellite_smoke.py`:

```python
"""py_compile smoke for satellite.py (spec carry-over): openwakeword is
Pi-only so the Mac suite can't import it — but name-level breakage should
fail here, not at Phase 3 hardware bring-up."""

import py_compile
from pathlib import Path


def test_satellite_py_compiles() -> None:
    py_compile.compile(str(Path(__file__).resolve().parents[1] / "satellite.py"), doraise=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest satellite/tests/ -v`
Expected: new fake_satellite tests FAIL (`AttributeError: ... 'EventListener'`); smoke test PASSES already (fine — it guards future edits).

- [ ] **Step 3: Write the implementation**

In `satellite/fake_satellite.py`, extend the imports:

```python
import asyncio
import json
import threading

import websockets
```

Add after `ROUTER_URL`:

```python
EVENTS_URL = os.environ.get("EVENTS_URL", "ws://localhost:8200/events")
CHIME_CMD = ["afplay", "/System/Library/Sounds/Glass.aiff"]
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0
```

Add the listener class after `ask_router`:

```python
class _StopSession(Exception):
    """Internal: unwinds the TaskGroup when stop() is requested."""


class EventListener:
    """Background push-channel listener (spec Component 3).

    Own thread running its own asyncio loop: connect to EVENTS_URL, reconnect
    forever with capped backoff, chime + speak on announce, pong on ping.
    announcement_active is set while an event is unacknowledged; the
    foreground loop calls request_ack() to acknowledge.

    Note: speak() blocks this loop for the utterance duration — acceptable for
    the fake satellite; the announce repeat cadence is router-side."""

    def __init__(self, events_url: str) -> None:
        self.events_url = events_url
        self.announcement_active = threading.Event()
        self._pending_event_id: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ack_queue: asyncio.Queue[str] | None = None
        self._stop_async: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="events-listener")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None and self._stop_async is not None:
            self._loop.call_soon_threadsafe(self._stop_async.set)
        self._thread.join(timeout=5)

    def request_ack(self) -> None:
        """Called from the foreground thread on Enter during an announcement."""
        eid = self._pending_event_id
        if eid is not None and self._loop is not None and self._ack_queue is not None:
            self._loop.call_soon_threadsafe(self._ack_queue.put_nowait, eid)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ack_queue = asyncio.Queue()
        self._stop_async = asyncio.Event()
        backoff = RECONNECT_MIN_S
        while not self._stop_async.is_set():
            try:
                async with websockets.connect(self.events_url) as ws:
                    backoff = RECONNECT_MIN_S
                    try:
                        await self._session(ws)
                    except* _StopSession:
                        return
            except (OSError, websockets.WebSocketException, ExceptionGroup):
                # TaskGroup wraps a reader ConnectionClosed in an ExceptionGroup;
                # either way: reconnect with backoff.
                pass
            if self._stop_async.is_set():
                return
            try:
                await asyncio.wait_for(self._stop_async.wait(), timeout=backoff)
                return
            except TimeoutError:
                backoff = min(backoff * 2, RECONNECT_MAX_S)

    async def _session(self, ws) -> None:  # noqa: ANN001 — ws class moved between versions
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._read(ws))
            tg.create_task(self._pump_acks(ws))
            tg.create_task(self._watch_stop())

    async def _watch_stop(self) -> None:
        await self._stop_async.wait()
        raise _StopSession

    async def _read(self, ws) -> None:  # noqa: ANN001
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "announce":
                self._pending_event_id = msg["event_id"]
                self.announcement_active.set()
                subprocess.run(CHIME_CMD, check=False)
                speak(msg["speech"])
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    async def _pump_acks(self, ws) -> None:  # noqa: ANN001
        while True:
            eid = await self._ack_queue.get()
            await ws.send(json.dumps({"type": "ack", "event_id": eid}))
            self.announcement_active.clear()
            self._pending_event_id = None
```

TaskGroup semantics note for the implementer: when `_read` raises (connection closed) or `_watch_stop` raises `_StopSession`, the group cancels the sibling tasks and re-raises as an `ExceptionGroup` — that is why `_main` uses `except* _StopSession` for clean shutdown and lists `ExceptionGroup` in the reconnect handler. Do not "simplify" either clause away.

Replace `main()`'s body with:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index")
    ap.add_argument("--silence-rms", type=float, default=300, help="endpointing threshold")
    args = ap.parse_args()

    listener = EventListener(EVENTS_URL)
    listener.start()
    print("fake satellite — press Enter, speak, pause; Enter during an alarm acks it; Ctrl-C to quit")
    try:
        while True:
            try:
                input("⏎ ")
                if listener.announcement_active.is_set():
                    listener.request_ack()
                    print("(acknowledged)")
                    continue
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=FRAME_SAMPLES,
                    device=args.device,
                ) as stream:
                    audio = record_utterance(stream, silence_rms=args.silence_rms)
                text = asyncio.run(transcribe(audio, STT_URL))
                print(f"heard: {text!r}")
                if not text:
                    continue
                speech = ask_router(text)
                print(f"reply: {speech!r}")
                if speech:
                    speak(speech)
            except KeyboardInterrupt:
                print("\nbye")
                return
            except EOFError:
                # spec (audit edit): closed/redirected stdin exits cleanly,
                # never busy-loops through the generic handler
                print("\nstdin closed — exiting")
                return
            except Exception:
                log.exception("turn failed; listening again")
    finally:
        listener.stop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest satellite/tests/ -v`
Expected: all PASS. These are thread-coordination tests — every wait has a 5 s ceiling; a hang means the listener isn't pumping.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python3 -m pytest
git add satellite/fake_satellite.py satellite/tests/test_fake_satellite.py satellite/tests/test_satellite_smoke.py
git commit -m "feat(fake_satellite): background events listener with ack path and clean EOF exit"
```

---

### Task 13: Docs, final sweep, and exit-criteria checklist

**Files:**
- Modify: `README.md`

**Interfaces:** none — documentation + verification only.

- [ ] **Step 1: Update README.md**

In the Router section (step 3), replace the parenthetical note `(\`OPEN_BRAIN_URL\` is unset until Phase 2 — knowledge queries fall back to the LLM.)` with:

```markdown
   Phase 2 env (in `.env`): `OPEN_BRAIN_URL` (the Azure Container Apps URL) and
   `OPEN_BRAIN_API_KEY` (Key Vault secret `ob-search-api-key`). Leave both unset
   to route knowledge queries to the LLM instead. Timers persist in
   `router/timers.db` (gitignored; override path with `TIMERS_DB`).
```

After the "Fake satellite" block, add:

```markdown
**Timers (Phase 2):** with both services running, try "set a timer for 2
minutes" — the fake satellite chimes and announces when it fires, repeating
every 30 s (max 10×) until you press Enter. The satellite holds a websocket
open to `ws://localhost:8200/events` (override with `EVENTS_URL`); timers
survive router restarts.
```

- [ ] **Step 2: Full verification sweep**

```bash
source .venv/bin/activate
python3 -m pytest        # expected: all green (baseline 23 + ~75 new)
ruff check .             # expected: clean
```

- [ ] **Step 3: Commit and push**

```bash
git add README.md
git commit -m "docs: Phase 2 run notes (Open Brain env, timers, events channel)"
git push
```

- [ ] **Step 4: Manual exit-criteria checklist (with Will, after the open-brain plan deploys)**

Not subagent work — record results in the session:

1. **Timers:** `set a timer for 2 minutes` on the fake satellite → chime + announcement 2 minutes later, repeating until Enter — **including after killing and restarting the router mid-countdown**.
2. **Open Brain:** with `.env` carrying the real `OPEN_BRAIN_URL` + `OPEN_BRAIN_API_KEY` (companion plan deployed): "what did I decide about the speaker driver?" returns a sensible spoken answer synthesized from actual Open Brain content.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** Component 1 → Tasks 4–6; Component 2 → Task 7 (+ endpoint in Task 8); Component 3 → Task 12; Component 4 → Tasks 1–3, 6, 9; Component 5 → Task 10 (+ companion open-brain plan); carry-over fixes → Tasks 8 (clients), 11 (timeout + error frame), 12 (py_compile); audit edits → Task 9 (precision + guard), Task 12 (EOF), Task 11 (error frame); error handling § → Tasks 6/7/10; testing § → every task; exit criteria → Task 13.
- **Known simplifications (deliberate):** timer labels ("pasta timer") are unsupported — spec doesn't require them; `speak()` blocking the listener loop is documented in-code; alarm `at_qualifier` matching compares hour-mod-12 + minute so "cancel my 7 am alarm" matches a 7:00 alarm regardless of am/pm ambiguity in the utterance; the scheduler announces one firing at a time — a second timer due during another's announcement loop waits for that loop to ack or exhaust (worst case 5 min; spec doesn't define overlap behavior and single-satellite v1 makes simultaneous announcements undesirable anyway).
- **Type consistency check:** `handle_timer(verb, text, store, scheduler)` — Tasks 6 (def) and 9 (call) match. `Scheduler(store, announce)` + `broadcast_announce(event_id, speech, repeat_n)` — Tasks 6/7/8 match. `run_device_action(intent, match, http)` / `ask_llm(text, anthropic)` / `ask_open_brain(query, http, anthropic)` — Tasks 8/9/10 match. `EventListener.start/stop/request_ack/announcement_active` — Task 12 def and tests match.
