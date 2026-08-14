"""Pipeline unit tests: rms, endpointing, and STT round-trip against a local fake server."""

import json

import numpy as np
import pipeline
import pytest
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


# --- pre-roll: audio captured before record_utterance is called (fixes the
#     wake-word→record gap that clipped the front of the utterance) ---


def test_preroll_is_prepended_to_the_recording() -> None:
    """Frames the caller already read (e.g. during wake-word detection) are
    prepended, so the start of the utterance isn't lost between detection and
    the recorder starting."""
    preroll = np.full(pipeline.FRAME_SAMPLES * 3, 1234, dtype=np.int16).tobytes()  # 3 frames
    audio = pipeline.record_utterance(
        FakeStream(loud_frames=5), preroll=preroll, silence_rms=300, silence_seconds=0.8
    )
    # the returned audio must START with the preroll bytes verbatim
    assert audio[: len(preroll)] == preroll
    # and total length = preroll + (recorded frames)
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    recorded = (5 + quiet_needed) * pipeline.FRAME_SAMPLES * 2
    assert len(audio) == len(preroll) + recorded


def test_preroll_counts_toward_endpointing_start() -> None:
    """Loud pre-roll means speech has already started — so a stream that is
    silent from frame 0 still records (and endpoints), rather than waiting
    forever for a speech onset that already happened in the pre-roll."""

    class SilentForever:
        def read(self, n: int) -> tuple[np.ndarray, bool]:
            return np.zeros(n, dtype=np.int16), False

    loud_preroll = np.full(pipeline.FRAME_SAMPLES * 2, 2000, dtype=np.int16).tobytes()
    audio = pipeline.record_utterance(
        SilentForever(), preroll=loud_preroll, silence_rms=300, silence_seconds=0.8, max_seconds=5.0
    )
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    # started=True from the pre-roll → endpoints after quiet_needed silent frames,
    # not run to max_seconds.
    assert len(audio) == len(loud_preroll) + quiet_needed * pipeline.FRAME_SAMPLES * 2


# --- record_utterance channel selection (XVF3800: 2 interleaved channels) ---


class FakeStereoStream:
    """Yields `loud_frames` interleaved-stereo frames where the two channels
    can differ, then stereo silence forever. `read(n)` returns 2*n int16
    samples interleaved [L0,R0,L1,R1,...] — exactly what a 2-channel
    sounddevice read delivers when np.frombuffer'd."""

    def __init__(self, loud_frames: int, left_level: int, right_level: int) -> None:
        self.remaining_loud = loud_frames
        self.left_level = left_level
        self.right_level = right_level

    def read(self, n: int) -> tuple[np.ndarray, bool]:
        left = self.left_level if self.remaining_loud > 0 else 0
        right = self.right_level if self.remaining_loud > 0 else 0
        if self.remaining_loud > 0:
            self.remaining_loud -= 1
        stereo = np.empty(n * 2, dtype=np.int16)
        stereo[0::2] = left
        stereo[1::2] = right
        return stereo, False


def test_record_stereo_returns_mono_bytes() -> None:
    """A 2-channel stream must yield MONO bytes (one channel), not the full
    interleaved buffer — otherwise Whisper gets stereo it can't read."""
    audio = pipeline.record_utterance(
        FakeStereoStream(loud_frames=5, left_level=2000, right_level=2000),
        capture_channel="right",
        n_channels=2,
        silence_rms=300,
        silence_seconds=0.8,
    )
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    expected_frames = 5 + quiet_needed
    # mono: FRAME_SAMPLES samples/frame * 2 bytes — NOT doubled for 2 channels
    assert len(audio) == expected_frames * pipeline.FRAME_SAMPLES * 2


def test_record_endpoints_on_the_selected_channel_only() -> None:
    """Endpointing must measure the SELECTED channel, not an average of both.
    Left loud, right silent: selecting 'right' sees silence and never arms, so
    it records to max_seconds; selecting 'left' hears speech."""
    max_seconds = 2.0
    max_frames = int(max_seconds * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)

    # capture_channel='right' on a right-silent stream: never starts, runs to max.
    right = pipeline.record_utterance(
        FakeStereoStream(loud_frames=5, left_level=2000, right_level=0),
        capture_channel="right",
        n_channels=2,
        silence_rms=300,
        max_seconds=max_seconds,
    )
    assert len(right) == max_frames * pipeline.FRAME_SAMPLES * 2

    # capture_channel='left' on the same stream: hears the 5 loud frames, endpoints.
    left = pipeline.record_utterance(
        FakeStereoStream(loud_frames=5, left_level=2000, right_level=0),
        capture_channel="left",
        n_channels=2,
        silence_rms=300,
        silence_seconds=0.8,
        max_seconds=max_seconds,
    )
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    assert len(left) == (5 + quiet_needed) * pipeline.FRAME_SAMPLES * 2


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
