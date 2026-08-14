"""Shared satellite pipeline: endpointed recording + STT streaming.

Used by both satellite.py (Pi + XVF3800) and fake_satellite.py (Mac mic,
push-to-talk). Keeping this shared is the point: the fake satellite must
exercise the exact code the real one runs.
"""

import asyncio
import json
from typing import Protocol

import numpy as np
import websockets
from channelpick import select_channel

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms, openWakeWord's expected frame size
STT_TIMEOUT_S = 60


class AudioStream(Protocol):
    """The slice of sounddevice.InputStream we use (duck-typed for tests)."""

    def read(self, frames: int) -> tuple[object, bool]: ...


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


def record_utterance(
    stream: AudioStream,
    *,
    preroll: bytes = b"",
    capture_channel: str = "left",
    n_channels: int = 1,
    silence_rms: float = 300,
    silence_seconds: float = 0.8,
    onset_seconds: float = 3.0,
    max_seconds: float = 12.0,
) -> bytes:
    """Record the user's utterance: wait for speech to start, then capture
    until sustained silence (or max length). Returns b"" if no speech starts
    within `onset_seconds` (the caller then skips the turn).

    `preroll` is mono int16 audio the caller already captured *before* calling
    this — e.g. the frames read during wake-word detection. When speech does
    follow, the pre-roll is prepended so the start of an immediately-spoken
    question isn't lost in the wake→record gap ("tell me a joke" → "that's a
    joke"). **The pre-roll itself never counts as speech** — the tail of
    "Jarvis" is loud, and if it armed recording, a pause-after-wake-word would
    transcribe that tail as "service". So only a loud frame from the *live
    stream* (the user's actual question) arms recording; if none arrives within
    `onset_seconds`, the turn yields b"" and the pre-roll is discarded.

    The XVF3800 delivers 2 interleaved channels (left=processed, right=ASR);
    with n_channels>1 each *stream* frame is deinterleaved to `capture_channel`
    before both the endpointing RMS and the returned bytes (the pre-roll is
    already mono — the caller selects the channel before buffering it), so
    endpointing measures the chosen channel (not an average) and Whisper
    receives mono. With the default n_channels=1 (a plain mono mic, e.g.
    fake_satellite) select_channel is a passthrough and behavior is unchanged.
    """
    frames: list[bytes] = []
    quiet_frames = 0
    quiet_needed = int(silence_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    onset_frames = int(onset_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    max_frames = int(max_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    started = False

    for i in range(max_frames):
        raw, _ = stream.read(FRAME_SAMPLES)
        mono = select_channel(bytes(raw), channel=capture_channel, n_channels=n_channels)
        loud = rms(mono) >= silence_rms
        if not started:
            if loud:
                # Speech just started. Prepend the pre-roll (so the front of the
                # utterance is included) plus this first loud frame, and begin.
                started = True
                if preroll:
                    frames.append(preroll)
                frames.append(mono.tobytes())
            elif i >= onset_frames:
                return b""  # no speech within the onset window — skip the turn
            # else: still waiting for speech; don't buffer the silence
            continue
        frames.append(mono.tobytes())
        if loud:
            quiet_frames = 0
        else:
            quiet_frames += 1
            if quiet_frames >= quiet_needed:
                break
    return b"".join(frames)


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
