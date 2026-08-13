"""Unit tests for the wake-word bench channel picker.

The live audio loop (wakeword_bench.py) imports openwakeword, which this repo
installs only on the Pi (it's in requirements.txt, not requirements-mac.txt),
so the Mac suite can't import the module — and even where it can, the loop needs
a real mic and a person speaking, so it isn't unit-testable anywhere. The
channel-selection logic is therefore extracted into channelpick.py, which
depends only on numpy — it's the part most likely to be wrong (deinterleaving
the XVF3800's two capture channels), so it must be testable without audio
hardware. A separate py_compile smoke test (test_wakeword_bench_smoke.py) guards
the wakeword_bench module against name-level breakage on the Mac.
"""

import channelpick as cp
import numpy as np


def _interleave(left: list[int], right: list[int]) -> bytes:
    """Build an int16 interleaved buffer [L0,R0,L1,R1,...] like a 2-channel
    sounddevice read delivers, as raw bytes (what stream.read returns)."""
    stereo = np.empty(len(left) * 2, dtype=np.int16)
    stereo[0::2] = np.array(left, dtype=np.int16)
    stereo[1::2] = np.array(right, dtype=np.int16)
    return stereo.tobytes()


def test_select_left_channel_returns_only_left_samples() -> None:
    buf = _interleave(left=[10, 11, 12], right=[90, 91, 92])
    out = cp.select_channel(buf, channel="left", n_channels=2)
    assert out.tolist() == [10, 11, 12]


def test_select_right_channel_returns_only_right_samples() -> None:
    buf = _interleave(left=[10, 11, 12], right=[90, 91, 92])
    out = cp.select_channel(buf, channel="right", n_channels=2)
    assert out.tolist() == [90, 91, 92]


def test_select_channel_returns_int16_for_openwakeword() -> None:
    # openWakeWord.predict expects an int16 array; the picker must preserve dtype.
    buf = _interleave(left=[1, 2], right=[3, 4])
    out = cp.select_channel(buf, channel="left", n_channels=2)
    assert out.dtype == np.int16


def test_single_channel_passthrough() -> None:
    # n_channels=1 (bare mic / already-mono device): no deinterleave, all samples.
    mono = np.array([5, 6, 7, 8], dtype=np.int16).tobytes()
    out = cp.select_channel(mono, channel="left", n_channels=1)
    assert out.tolist() == [5, 6, 7, 8]
