"""Push channel tests — TestClient websocket (spec §Testing)."""

import push
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient


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
    assert msg == {
        "type": "announce",
        "event_id": "ev-1",
        "speech": "Your 2-minute timer is done.",
        "repeat_n": 1,
    }


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
