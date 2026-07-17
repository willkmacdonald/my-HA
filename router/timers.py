"""Timer store, scheduler, and spoken-phrase helpers (spec Component 1).

router.py stays thin and dispatches here. SQLite via aiosqlite, WAL mode,
single writer (the router process). Times stored as UTC ISO-8601 strings;
all local-time math lives in timeparse.py.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import timeparse
from timeparse import LOCAL_TZ

log = logging.getLogger("timers")

ANNOUNCE_INTERVAL_S = 30
ANNOUNCE_MAX_REPEATS = 10
MISSED_GRACE_S = 300
SCHEDULER_RETRY_S = 5

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
    return datetime.now(UTC)


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("to_utc_iso requires an aware datetime; got naive input")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_utc_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


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
            (
                t.id,
                t.kind,
                t.text,
                t.fire_at,
                t.recurrence,
                t.duration_seconds,
                t.status,
                t.created_at,
            ),
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
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed; retrying in %ds", SCHEDULER_RETRY_S)
                await asyncio.sleep(SCHEDULER_RETRY_S)

    async def _tick(self) -> None:
        nxt = await self._store.next_armed()
        if nxt is None:
            await self.wake.wait()
            self.wake.clear()
            return
        delay = (nxt.fire_at_dt - utcnow()).total_seconds()
        if delay > 0:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
                self.wake.clear()
                return  # store changed — re-evaluate earliest
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
            hour=local.hour,
            minute=local.minute,
            recurrence=t.recurrence,
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
                hour=local.hour,
                minute=local.minute,
                recurrence=t.recurrence,
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
            await store.add(kind="timer", fire_at=fire, duration_seconds=parsed.duration_seconds)
            scheduler.wake.set()
            return f"Timer set for {duration_noun(parsed.duration_seconds)}."
        if parsed.kind == "alarm":
            await store.add(kind="alarm", fire_at=parsed.fire_at, recurrence=parsed.recurrence)
            scheduler.wake.set()
            return (
                f"Alarm set for {clock_phrase(parsed.fire_at)}"
                f"{recurrence_phrase(parsed.recurrence)}."
            )
        fire = parsed.fire_at or (utcnow() + timedelta(seconds=parsed.duration_seconds))
        await store.add(
            kind="reminder", fire_at=fire, text=parsed.text, recurrence=parsed.recurrence
        )
        scheduler.wake.set()
        when = (
            f"at {clock_phrase(parsed.fire_at)}"
            if parsed.fire_at
            else f"in {duration_noun(parsed.duration_seconds)}"
        )
        return (
            f"Okay, I'll remind you to {parsed.text} {when}{recurrence_phrase(parsed.recurrence)}."
        )

    kind = parsed.kind or "timer"
    matching = [t for t in await store.by_status("armed") if t.kind == kind]
    if parsed.at_qualifier is not None:
        h, m, explicit = parsed.at_qualifier
        if explicit:
            # explicit am/pm ("cancel my 7 pm alarm") -> exact 24-h hour match,
            # so a 7 am and 7 pm alarm armed at once are never conflated.
            matching = [
                t
                for t in matching
                if (lambda lt: (lt.hour, lt.minute) == (h, m))(t.fire_at_dt.astimezone(LOCAL_TZ))
            ]
        else:
            # bare qualifier ("cancel my 7 alarm") -> mod-12 match, ambiguous
            # between am/pm by design (spec: bare clock resolves either way).
            matching = [
                t
                for t in matching
                if (lambda lt: (lt.hour % 12, lt.minute) == (h % 12, m))(
                    t.fire_at_dt.astimezone(LOCAL_TZ)
                )
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
            parts.append(
                f"Your {duration_adj(t.duration_seconds or 0)} timer has "
                f"{remaining_phrase(left)} left."
            )
        elif t.kind == "alarm":
            parts.append(
                f"Alarm at {clock_phrase(t.fire_at_dt.astimezone(LOCAL_TZ))}"
                f"{recurrence_phrase(t.recurrence)}."
            )
        else:
            parts.append(
                f"Reminder to {t.text} at {clock_phrase(t.fire_at_dt.astimezone(LOCAL_TZ))}."
            )
    return " ".join(parts)
