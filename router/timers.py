"""Timer store, scheduler, and spoken-phrase helpers (spec Component 1).

router.py stays thin and dispatches here. SQLite via aiosqlite, WAL mode,
single writer (the router process). Times stored as UTC ISO-8601 strings;
all local-time math lives in timeparse.py.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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
    return datetime.now(UTC)


def to_utc_iso(dt: datetime) -> str:
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
