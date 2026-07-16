"""TimerStore + scheduler tests — temp DB per test (spec §Testing)."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import timers
from timeparse import LOCAL_TZ


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


def test_to_utc_iso_rejects_naive_datetime() -> None:
    from datetime import datetime as _dt

    with pytest.raises(ValueError, match="aware datetime"):
        timers.to_utc_iso(_dt(2026, 7, 16, 9, 0, 0))


def _local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 7, 16, h, m, tzinfo=LOCAL_TZ)


def _mk(
    kind: str,
    *,
    fire_local: datetime | None = None,
    text: str | None = None,
    duration_seconds: int | None = None,
    recurrence: str = "none",
) -> timers.Timer:
    fire = fire_local or _local(7)
    return timers.Timer(
        id="t1",
        kind=kind,
        text=text,
        fire_at=timers.to_utc_iso(fire),
        recurrence=recurrence,
        duration_seconds=duration_seconds,
        status="armed",
        created_at=timers.to_utc_iso(timers.utcnow()),
    )


@pytest.mark.parametrize(
    ("seconds", "noun", "adj"),
    [
        (600, "10 minutes", "10-minute"),
        (60, "1 minute", "1-minute"),
        (3600, "1 hour", "1-hour"),
        (7200, "2 hours", "2-hour"),
        (90, "90 seconds", "90-second"),
        (5400, "90 minutes", "90-minute"),
    ],
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
