"""Pick one capture channel out of an interleaved multi-channel audio buffer.

Extracted from wakeword_bench so it can be unit-tested without audio hardware:
the bench itself needs a real mic (and imports openwakeword, which this repo
installs only on the Pi), but this deinterleaving is the part most likely to be
wrong — the XVF3800 exposes two channels (left = processed audio, right = ASR
reference) and openWakeWord must be fed exactly one. numpy-only, no audio deps.
"""

import numpy as np

# Named channels → column index in the interleaved frame. The XVF3800's channel
# order (left = processed, right = ASR reference) is documented in hardware.md;
# if a board enumerates them reversed, this map is the one line to flip.
CHANNEL_INDEX = {"left": 0, "right": 1}


def select_channel(buffer: bytes, *, channel: str, n_channels: int) -> np.ndarray:
    """Return the int16 samples for one channel of a raw interleaved read.

    `buffer` is what sounddevice's stream.read delivers: raw bytes of int16
    samples interleaved as [c0_0, c1_0, c0_1, c1_1, ...] across n_channels.
    With n_channels == 1 the buffer is already mono and is returned as-is.
    """
    samples = np.frombuffer(buffer, dtype=np.int16)
    if n_channels == 1:
        return samples
    index = CHANNEL_INDEX[channel]
    # reshape interleaved -> (frames, n_channels), take the channel column.
    return samples.reshape(-1, n_channels)[:, index]
