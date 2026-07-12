"""Fake satellite — runs on the Mac. Push-to-talk instead of wake word.

Exercises the identical record → STT → route → TTS loop the Pi runs, using
the Mac's mic and the built-in `say` command for TTS. This is the Phase 1
exit-criterion tool: speak a question at the Mac, hear a spoken answer.

Usage:
    STT_URL=ws://localhost:8100/stt ROUTER_URL=http://localhost:8200/route \
        python3 fake_satellite.py [--device N] [--silence-rms 300]

Run `python3 -c "import sounddevice; print(sounddevice.query_devices())"`
to list input devices if the default mic isn't right.
"""

import argparse
import asyncio
import logging
import os
import subprocess

import requests
import sounddevice as sd
from pipeline import FRAME_SAMPLES, SAMPLE_RATE, record_utterance, transcribe

STT_URL = os.environ.get("STT_URL", "ws://localhost:8100/stt")
ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8200/route")

log = logging.getLogger("fake_satellite")


def speak(text: str) -> None:
    """macOS built-in TTS — no Piper needed on the Mac."""
    subprocess.run(["say", text], check=False)


def ask_router(text: str) -> str:
    resp = requests.post(ROUTER_URL, json={"text": text}, timeout=60)
    resp.raise_for_status()
    return resp.json().get("speech", "")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index")
    ap.add_argument("--silence-rms", type=float, default=300, help="endpointing threshold")
    args = ap.parse_args()

    print("fake satellite — press Enter, speak, pause; Ctrl-C to quit")
    while True:
        try:
            input("⏎ ")
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                device=args.device,
            ) as stream:
                audio = record_utterance(stream, silence_rms=args.silence_rms)
            text = asyncio.run(transcribe(audio, STT_URL))
            print(f"heard: {text!r}")
            if not text:
                continue
            speech = ask_router(text)
            print(f"reply: {speech!r}")
            if speech:
                speak(speech)
        except KeyboardInterrupt:
            print("\nbye")
            return
        except Exception:
            log.exception("turn failed; listening again")


if __name__ == "__main__":
    main()
