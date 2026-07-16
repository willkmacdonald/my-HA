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


def test_to_utc_iso_rejects_naive_datetime() -> None:
    from datetime import datetime as _dt

    with pytest.raises(ValueError, match="aware datetime"):
        timers.to_utc_iso(_dt(2026, 7, 16, 9, 0, 0))
