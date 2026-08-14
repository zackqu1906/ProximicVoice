from __future__ import annotations

import numpy as np


def decode_pcm16le(data: bytes) -> np.ndarray:
    """Decode signed 16-bit little-endian PCM to mono float32 samples in [-1, 1)."""
    if len(data) % 2:
        raise ValueError("PCM16 byte payload must have an even number of bytes")
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / np.float32(32768.0)
