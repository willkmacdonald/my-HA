"""Voice satellite — runs on the Pi 5 with the ReSpeaker XVF3800.

Loop: wait for wake word → record until silence → stream audio to the STT
server on the Mac (over Tailscale) → send transcript to the router → speak
the reply with Piper.

The XVF3800 presents as a normal USB audio device; echo cancellation and
beamforming happen on its XMOS chip, so this script just reads clean mono
16 kHz audio.

Usage:
    STT_URL=ws://mac-studio:8100/stt ROUTER_URL=http://mac-studio:8200/route \
        python satellite.py --device 1
"""

import argparse
import asyncio
import json
import os
import subprocess

import numpy as np
import requests
import sounddevice as sd
import websockets
from openwakeword.model import Model

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms, openWakeWord's expected frame size

STT_URL = os.environ.get("STT_URL", "ws://localhost:8100/stt")
ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8200/route")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")

# Utterance endpointing: stop after this much sustained quiet, or at max length.
SILENCE_RMS = 300          # int16 RMS below this counts as silence; tune on-device
SILENCE_SECONDS = 0.8
MAX_UTTERANCE_SECONDS = 12.0


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


def record_utterance(stream: sd.InputStream) -> bytes:
    """Record from wake-word trigger until sustained silence."""
    frames: list[bytes] = []
    quiet_frames = 0
    quiet_needed = int(SILENCE_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    max_frames = int(MAX_UTTERANCE_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    started = False  # don't endpoint before speech has begun

    for _ in range(max_frames):
        frame, _ = stream.read(FRAME_SAMPLES)
        frames.append(bytes(frame))
        loud = rms(np.frombuffer(frame, dtype=np.int16)) >= SILENCE_RMS
        if loud:
            started = True
            quiet_frames = 0
        elif started:
            quiet_frames += 1
            if quiet_frames >= quiet_needed:
                break
    return b"".join(frames)


async def transcribe(audio: bytes) -> str:
    """Stream PCM chunks to the STT server, get the transcript back."""
    async with websockets.connect(STT_URL, max_size=None) as ws:
        chunk = FRAME_SAMPLES * 2 * 10  # ~0.8 s per chunk
        for i in range(0, len(audio), chunk):
            await ws.send(audio[i : i + chunk])
        await ws.send("END")
        reply = json.loads(await ws.recv())
    return reply["text"].strip()


def speak(text: str) -> None:
    piper = subprocess.Popen(
        ["piper", "--model", PIPER_VOICE, "--output-raw"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    aplay = subprocess.Popen(
        ["aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
        stdin=piper.stdout,
    )
    piper.stdin.write(text.encode())
    piper.stdin.close()
    aplay.wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index (XVF3800)")
    ap.add_argument("--wake-model", default="hey_jarvis")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    oww = Model(wakeword_models=[args.wake_model])
    print(f"listening for wake word ({args.wake_model})…")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        device=args.device,
    ) as stream:
        while True:
            frame, _ = stream.read(FRAME_SAMPLES)
            scores = oww.predict(np.frombuffer(frame, dtype=np.int16))
            if max(scores.values()) < args.threshold:
                continue

            print("wake word detected, listening…")
            oww.reset()
            audio = record_utterance(stream)
            text = asyncio.run(transcribe(audio))
            print(f"heard: {text!r}")
            if not text:
                continue

            resp = requests.post(ROUTER_URL, json={"text": text}, timeout=60)
            speech = resp.json().get("speech", "")
            print(f"reply: {speech!r}")
            if speech:
                speak(speech)


if __name__ == "__main__":
    main()
