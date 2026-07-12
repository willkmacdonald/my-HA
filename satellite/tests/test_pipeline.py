"""Pipeline unit tests: rms, endpointing, and STT round-trip against a local fake server."""

import json

import numpy as np
import pipeline
import websockets

# --- rms ---


def test_rms_of_silence_is_zero() -> None:
    assert pipeline.rms(np.zeros(100, dtype=np.int16)) == 0.0


def test_rms_of_constant_signal() -> None:
    assert pipeline.rms(np.full(100, 1000, dtype=np.int16)) == 1000.0


# --- record_utterance endpointing ---


class FakeStream:
    """Yields loud frames, then silence forever."""

    def __init__(self, loud_frames: int, level: int = 2000) -> None:
        self.remaining_loud = loud_frames
        self.level = level

    def read(self, n: int) -> tuple[np.ndarray, bool]:
        if self.remaining_loud > 0:
            self.remaining_loud -= 1
            return np.full(n, self.level, dtype=np.int16), False
        return np.zeros(n, dtype=np.int16), False


def test_record_stops_after_sustained_silence() -> None:
    audio = pipeline.record_utterance(
        FakeStream(loud_frames=5), silence_rms=300, silence_seconds=0.8, max_seconds=12.0
    )
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    expected_frames = 5 + quiet_needed
    assert len(audio) == expected_frames * pipeline.FRAME_SAMPLES * 2  # int16 = 2 bytes


def test_record_does_not_endpoint_before_speech_starts() -> None:
    """Leading silence must not trigger the endpoint — only silence after speech."""

    class SilenceThenLoud:
        def __init__(self) -> None:
            self.frame = 0

        def read(self, n: int) -> tuple[np.ndarray, bool]:
            self.frame += 1
            if self.frame <= 30:  # 30 quiet frames > quiet_needed (10)
                return np.zeros(n, dtype=np.int16), False
            if self.frame <= 35:
                return np.full(n, 2000, dtype=np.int16), False
            return np.zeros(n, dtype=np.int16), False

    audio = pipeline.record_utterance(SilenceThenLoud(), silence_rms=300)
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    expected_frames = 30 + 5 + quiet_needed
    assert len(audio) == expected_frames * pipeline.FRAME_SAMPLES * 2


def test_record_respects_max_length() -> None:
    class AlwaysLoud:
        def read(self, n: int) -> tuple[np.ndarray, bool]:
            return np.full(n, 2000, dtype=np.int16), False

    audio = pipeline.record_utterance(AlwaysLoud(), max_seconds=2.0)
    max_frames = int(2.0 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    assert len(audio) == max_frames * pipeline.FRAME_SAMPLES * 2


# --- transcribe ---


async def test_transcribe_streams_chunks_and_reads_reply() -> None:
    received: list[bytes] = []

    # untyped ws param on purpose: the connection class moved between
    # websockets versions (WebSocketServerProtocol → ServerConnection)
    async def handler(ws) -> None:  # noqa: ANN001
        async for msg in ws:
            if msg == "END":
                await ws.send(json.dumps({"text": "  hi there  "}))
                break
            received.append(msg)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        payload = b"\x01\x02" * 30000  # bigger than one send chunk
        text = await pipeline.transcribe(payload, f"ws://127.0.0.1:{port}/stt")

    assert text == "hi there"
    assert b"".join(received) == payload
    assert len(received) > 1  # actually chunked, not one blob
