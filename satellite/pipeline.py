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
    silence_rms: float = 300,
    silence_seconds: float = 0.8,
    max_seconds: float = 12.0,
) -> bytes:
    """Record from trigger until sustained silence (or max length).

    Endpointing only arms after speech is first heard, so leading silence
    doesn't cut the recording short.
    """
    frames: list[bytes] = []
    quiet_frames = 0
    quiet_needed = int(silence_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    max_frames = int(max_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    started = False

    for _ in range(max_frames):
        frame, _ = stream.read(FRAME_SAMPLES)
        frames.append(bytes(frame))
        loud = rms(np.frombuffer(bytes(frame), dtype=np.int16)) >= silence_rms
        if loud:
            started = True
            quiet_frames = 0
        elif started:
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
