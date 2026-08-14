"""Milestone 1 validation: measure wake word reliability on Pi 5 + XVF3800.

Say the wake word once per prompt; the script counts detections and prints
the hit rate at the end. The bare-mic baseline was ~1/20 (5%) — the XVF3800
theory predicts near-100%.

The XVF3800 exposes two capture channels (left = processed audio, right = ASR
reference). Run the bench once per channel and wire the better-scoring one into
satellite.py — that settles the open channel question from hardware.md.

Usage (on the Pi):
    python wakeword_bench.py --list-devices                        # find the XVF3800 index
    python wakeword_bench.py --trials 20 --capture-channel left    # processed audio
    python wakeword_bench.py --trials 20 --capture-channel right   # ASR reference
"""

import argparse
import time

import sounddevice as sd
from channelpick import CHANNEL_INDEX, select_channel
from openwakeword.model import Model

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms — the frame size openWakeWord expects
CAPTURE_CHANNELS = 2  # the XVF3800 exposes two: left (processed) + right (ASR ref)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--model", default="hey_jarvis", help="openWakeWord model name or path")
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index (XVF3800)")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--window", type=float, default=4.0, help="seconds to listen per trial")
    ap.add_argument(
        "--capture-channel",
        choices=sorted(CHANNEL_INDEX),
        default="left",
        help="which XVF3800 channel to feed the wake-word model "
        "(left=processed audio, right=ASR reference); run both, wire the winner into satellite.py",
    )
    ap.add_argument(
        "--inference-framework",
        choices=["onnx", "tflite"],
        default="onnx",
        help="openWakeWord backend. Default onnx: works on both Mac and Pi "
        "(tflite-runtime has no wheel for the Pi's Python/ARM).",
    )
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    oww = Model(wakeword_models=[args.model], inference_framework=args.inference_framework)
    hits = 0
    peak_scores: list[float] = []

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CAPTURE_CHANNELS,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        device=args.device,
    ) as stream:
        for trial in range(1, args.trials + 1):
            oww.reset()
            input(f"\n[{trial}/{args.trials}] Press Enter, then say the wake word…")
            # Drain audio buffered while the prompt sat waiting for Enter, so this
            # trial only scores what's spoken AFTER the prompt — otherwise stale
            # frames (incl. a prior trial's wake word) cause false hits and delay
            # real detection, corrupting the >90% gate number.
            stream.read(stream.read_available)
            deadline = time.monotonic() + args.window
            peak = 0.0
            detected = False
            while time.monotonic() < deadline:
                frame, _ = stream.read(FRAME_SAMPLES)
                mono = select_channel(
                    frame, channel=args.capture_channel, n_channels=CAPTURE_CHANNELS
                )
                scores = oww.predict(mono)
                score = max(scores.values())
                peak = max(peak, score)
                if score >= args.threshold:
                    detected = True
                    break
            hits += detected
            peak_scores.append(peak)
            print(f"  {'DETECTED' if detected else 'missed  '}  peak score {peak:.3f}")

    rate = hits / args.trials
    # Label the result with the channel so back-to-back left/right runs are
    # distinguishable in the scrollback — comparing them is the whole point.
    print(f"\n=== [{args.capture_channel}] {hits}/{args.trials} detected ({rate:.0%}) ===")
    median_score = sorted(peak_scores)[len(peak_scores) // 2]
    print(
        f"peak scores: min {min(peak_scores):.3f}  "
        f"median {median_score:.3f}  max {max(peak_scores):.3f}"
    )
    print("baseline with bare mic was ~5%; theory validated if this is >90%")


if __name__ == "__main__":
    main()
