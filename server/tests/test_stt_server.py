"""STT websocket protocol tests with a fake whisper model — no weights download."""

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import stt_server
from fastapi.testclient import TestClient


def _fake_model(captured: dict[str, Any]) -> SimpleNamespace:
    def transcribe(audio: np.ndarray, **kwargs: Any) -> tuple[list[SimpleNamespace], None]:
        captured["audio"] = audio
        return [SimpleNamespace(text=" hello "), SimpleNamespace(text="world ")], None

    return SimpleNamespace(transcribe=transcribe)


def test_stt_roundtrip_joins_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(stt_server, "get_model", lambda: _fake_model(captured))
    client = TestClient(stt_server.app)
    pcm = np.zeros(1600, dtype=np.int16).tobytes()

    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(pcm)
        ws.send_text("END")
        reply = ws.receive_json()

    assert reply == {"text": "hello world"}


def test_stt_converts_int16_to_normalized_float32(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(stt_server, "get_model", lambda: _fake_model(captured))
    client = TestClient(stt_server.app)
    pcm = np.array([32767, -32768, 0], dtype=np.int16).tobytes()

    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(pcm)
        ws.send_text("END")
        ws.receive_json()

    audio = captured["audio"]
    assert audio.dtype == np.float32
    assert audio[0] == pytest.approx(1.0, abs=1e-4)
    assert audio[1] == pytest.approx(-1.0, abs=1e-4)
    assert audio[2] == 0.0


def test_stt_multiple_chunks_are_concatenated(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(stt_server, "get_model", lambda: _fake_model(captured))
    client = TestClient(stt_server.app)
    chunk = np.ones(100, dtype=np.int16).tobytes()

    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(chunk)
        ws.send_bytes(chunk)
        ws.send_text("END")
        ws.receive_json()

    assert len(captured["audio"]) == 200
