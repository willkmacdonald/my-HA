"""Milestone 1 validation: measure wake word reliability on Pi 5 + XVF3800.

Say the wake word once per prompt; the script counts detections and prints
the hit rate at the end. The bare-mic baseline was ~1/20 (5%) — the XVF3800
theory predicts near-100%.

Usage (on the Pi):
    python wakeword_bench.py --trials 20 --model hey_jarvis
    python wakeword_bench.py --list-devices   # find the XVF3800 index
"""

import argparse
import time

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms — the frame size openWakeWord expects


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--model", default="hey_jarvis", help="openWakeWord model name or path")
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index (XVF3800)")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--window", type=float, default=4.0, help="seconds to listen per trial")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    oww = Model(wakeword_models=[args.model])
    hits = 0
    peak_scores: list[float] = []

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        device=args.device,
    ) as stream:
        for trial in range(1, args.trials + 1):
            oww.reset()
            input(f"\n[{trial}/{args.trials}] Press Enter, then say the wake word…")
            deadline = time.monotonic() + args.window
            peak = 0.0
            detected = False
            while time.monotonic() < deadline:
                frame, _ = stream.read(FRAME_SAMPLES)
                scores = oww.predict(np.frombuffer(frame, dtype=np.int16))
                score = max(scores.values())
                peak = max(peak, score)
                if score >= args.threshold:
                    detected = True
                    break
            hits += detected
            peak_scores.append(peak)
            print(f"  {'DETECTED' if detected else 'missed  '}  peak score {peak:.3f}")

    rate = hits / args.trials
    print(f"\n=== {hits}/{args.trials} detected ({rate:.0%}) ===")
    median_score = sorted(peak_scores)[len(peak_scores) // 2]
    print(
        f"peak scores: min {min(peak_scores):.3f}  "
        f"median {median_score:.3f}  max {max(peak_scores):.3f}"
    )
    print("baseline with bare mic was ~5%; theory validated if this is >90%")


if __name__ == "__main__":
    main()
