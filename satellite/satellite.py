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
import time

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
# piper-tts (the pip package) wants a PATH to the voice's .onnx file, not a bare
# voice name — download it once with `python -m piper.download_voices <name>`,
# then point PIPER_VOICE at the resulting .onnx (absolute path is safest so it
# works regardless of the process's working directory).
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium.onnx")
# MUST be the XVF3800's own playback device (see docs/hardware.md): the AEC
# can only cancel audio it plays itself. On the Pi the XVF3800 enumerates as
# ALSA card "Array", so APLAY_DEVICE=plughw:CARD=Array,DEV=0.
APLAY_DEVICE = os.environ.get("APLAY_DEVICE", "default")
# Absolute paths so the binaries resolve even when PATH is minimal (e.g. under
# systemd, which does NOT inherit the venv's PATH — piper lives in the venv's
# bin). Interactively, the bare names on PATH still work as defaults.
PIPER_BIN = os.environ.get("PIPER_BIN", "piper")
APLAY_BIN = os.environ.get("APLAY_BIN", "aplay")


def speak(text: str) -> None:
    piper = subprocess.Popen(
        [PIPER_BIN, "--model", PIPER_VOICE, "--output-raw"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    aplay = subprocess.Popen(
        [APLAY_BIN, "-D", APLAY_DEVICE, "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
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
    ap.add_argument(
        "--no-speak",
        action="store_true",
        help="skip TTS — print the reply instead of speaking it. Lets the full "
        "wake→record→STT→route pipeline be verified before Piper is installed.",
    )
    ap.add_argument(
        "--timing",
        action="store_true",
        help="print a per-stage latency breakdown after each query (record / STT / "
        "router round-trip incl. the router's own reported time / TTS). Diagnostic "
        "for finding which stage dominates latency.",
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

            t0 = time.perf_counter()
            audio = record_utterance(
                stream, capture_channel=args.capture_channel, n_channels=CAPTURE_CHANNELS
            )
            t_record = time.perf_counter()
            text = asyncio.run(transcribe(audio, STT_URL))
            t_stt = time.perf_counter()
            print(f"heard: {text!r}")
            if not text:
                continue

            # ?timing=1 asks the router to report its own handling time, so the
            # round-trip can be split into network vs router-thinking.
            url = ROUTER_URL + ("?timing=1" if args.timing else "")
            resp = requests.post(url, json={"text": text}, timeout=60)
            t_route = time.perf_counter()
            body = resp.json()
            speech = body.get("speech", "")
            print(f"reply: {speech!r}")

            t_speak = t_route
            if speech and not args.no_speak:
                speak(speech)
                t_speak = time.perf_counter()

            if args.timing:
                router_ms = (body.get("timing_ms") or {}).get("router")
                rt_ms = (t_route - t_stt) * 1000
                net_ms = rt_ms - router_ms if router_ms is not None else None
                parts = [
                    f"record {(t_record - t0) * 1000:6.0f}ms",
                    f"STT {(t_stt - t_record) * 1000:6.0f}ms",
                    f"route {rt_ms:6.0f}ms"
                    + (
                        f" (router {router_ms:.0f} + net {net_ms:.0f})"
                        if net_ms is not None
                        else ""
                    ),
                    f"TTS {(t_speak - t_route) * 1000:6.0f}ms",
                    f"| total {(t_speak - t0) * 1000:6.0f}ms",
                ]
                print("  ⏱  " + "  ".join(parts))


if __name__ == "__main__":
    main()
