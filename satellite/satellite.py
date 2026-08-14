"""Voice satellite — runs on the Pi 5 with the ReSpeaker XVF3800.

Loop: wait for wake word → record until silence → stream audio to the STT
server on the Mac (over Tailscale) → send transcript to the router → speak
the reply with Piper.

The XVF3800 presents as a normal USB audio device; echo cancellation and
beamforming happen on its XMOS chip. It exposes two 16 kHz channels
(left=processed, right=ASR-tuned); this script reads both and selects one
(--capture-channel, default right) for wake-word detection and the Whisper
recording.

Usage:
    STT_URL=ws://mac-studio:8100/stt ROUTER_URL=http://mac-studio:8200/route \
        python satellite.py --device 1 [--capture-channel right]
"""

import argparse
import asyncio
import os
import subprocess

import requests
import sounddevice as sd
from channelpick import CHANNEL_INDEX, select_channel
from openwakeword.model import Model
from pipeline import FRAME_SAMPLES, SAMPLE_RATE, record_utterance, transcribe

# The XVF3800 exposes two capture channels: left=processed, right=ASR reference.
# Both hit 100% wake-word detection in the Phase 3 bench; right is the vendor's
# ASR-tuned output, so it's the default for the Whisper-facing stream. Override
# with --capture-channel. (A plain mono mic would be CAPTURE_CHANNELS=1.)
CAPTURE_CHANNELS = 2

STT_URL = os.environ.get("STT_URL", "ws://localhost:8100/stt")
ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8200/route")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
# MUST be the XVF3800's own playback device (see docs/hardware.md): the AEC
# can only cancel audio it plays itself. e.g. APLAY_DEVICE=plughw:CARD=XVF3800
APLAY_DEVICE = os.environ.get("APLAY_DEVICE", "default")


def speak(text: str) -> None:
    piper = subprocess.Popen(
        ["piper", "--model", PIPER_VOICE, "--output-raw"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    aplay = subprocess.Popen(
        ["aplay", "-D", APLAY_DEVICE, "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
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
    ap.add_argument(
        "--inference-framework",
        choices=["onnx", "tflite"],
        default="onnx",
        help="openWakeWord backend. Default onnx: it works on both the Mac and the "
        "Pi, whereas tflite-runtime has no wheel for the Pi's Python/ARM.",
    )
    ap.add_argument(
        "--capture-channel",
        choices=sorted(CHANNEL_INDEX),
        default="right",
        help="which XVF3800 channel to use (left=processed, right=ASR-tuned); "
        "feeds both wake-word detection and the Whisper recording",
    )
    args = ap.parse_args()

    oww = Model(wakeword_models=[args.wake_model], inference_framework=args.inference_framework)
    print(f"listening for wake word ({args.wake_model}) on the {args.capture_channel} channel…")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CAPTURE_CHANNELS,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        device=args.device,
    ) as stream:
        while True:
            raw, _ = stream.read(FRAME_SAMPLES)
            mono = select_channel(
                bytes(raw), channel=args.capture_channel, n_channels=CAPTURE_CHANNELS
            )
            scores = oww.predict(mono)
            if max(scores.values()) < args.threshold:
                continue

            print("wake word detected, listening…")
            oww.reset()
            audio = record_utterance(
                stream, capture_channel=args.capture_channel, n_channels=CAPTURE_CHANNELS
            )
            text = asyncio.run(transcribe(audio, STT_URL))
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
