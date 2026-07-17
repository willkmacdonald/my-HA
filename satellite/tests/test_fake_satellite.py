"""fake_satellite unit tests — subprocess and HTTP are mocked."""

import asyncio
import json
import threading
from typing import Any
from unittest.mock import MagicMock

import fake_satellite
import pytest
import websockets


def test_speak_uses_macos_say(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        fake_satellite.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock()
    )
    fake_satellite.speak("hello world")
    assert calls == [["say", "hello world"]]


def test_ask_router_posts_text_and_returns_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict[str, Any] = {}

    def fake_post(url: str, json: dict, timeout: int) -> MagicMock:
        posted["url"] = url
        posted["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"speech": "It is 9 PM.", "intent": "llm_fallback"}
        return resp

    monkeypatch.setattr(fake_satellite.requests, "post", fake_post)
    speech = fake_satellite.ask_router("what time is it")
    assert speech == "It is 9 PM."
    assert posted["json"] == {"text": "what time is it"}
    assert posted["url"] == fake_satellite.ROUTER_URL


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
    monkeypatch.setattr(
        fake_satellite.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or MagicMock()
    )
    monkeypatch.setattr(fake_satellite, "speak", spoken.append)

    listener = fake_satellite.EventListener(f"ws://127.0.0.1:{fake_router.port}/events")
    listener.start()
    try:
        assert fake_router.connected.wait(timeout=5)
        fake_router.send(
            {
                "type": "announce",
                "event_id": "ev-9",
                "speech": "Your 2-minute timer is done.",
                "repeat_n": 1,
            }
        )
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


def test_listener_replies_pong_to_ping(
    fake_router: FakeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_stop_immediately_after_start_terminates_thread() -> None:
    listener = fake_satellite.EventListener("ws://127.0.0.1:1/events")
    listener.start()
    listener.stop()
    assert not listener._thread.is_alive()


def test_stop_logs_warning_when_join_times_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import time

    listener = fake_satellite.EventListener("ws://127.0.0.1:1/events")

    # simulate a reader wedged in a long blocking call by making the thread
    # ignore the stop signal for longer than the join timeout
    def slow_run() -> None:
        time.sleep(1.0)

    monkeypatch.setattr(listener, "_run", slow_run)
    listener._thread = threading.Thread(target=listener._run, daemon=True)
    listener.start()
    with caplog.at_level("WARNING", logger="fake_satellite"):
        listener.stop(join_timeout=0.1)
    assert any(
        "did not stop within" in r.message or "never finished starting" in r.message
        for r in caplog.records
    )


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
