from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .base import AudioSource
from ..pcm import decode_pcm16le


class WavSource(AudioSource):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._wav: wave.Wave_read | None = None

    def open(self) -> None:
        self._wav = wave.open(str(self.path), "rb")
        if self._wav.getframerate() != 16_000:
            raise ValueError(
                f"WAV is {self._wav.getframerate()} Hz; this legacy model expects 16000 Hz. "
                "Resample the file to 16 kHz first."
            )
        if self._wav.getsampwidth() != 2:
            raise ValueError("Only PCM16 WAV input is supported")
        if self._wav.getcomptype() != "NONE":
            raise ValueError("Compressed WAV is not supported")
        if self._wav.getnchannels() not in (1, 2):
            raise ValueError("Only mono or stereo PCM16 WAV input is supported")

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def read(self, frames: int) -> np.ndarray | None:
        if self._wav is None:
            raise RuntimeError("WavSource is not open")
        channels = self._wav.getnchannels()
        data = self._wav.readframes(frames)
        if not data:
            return None
        x = decode_pcm16le(data)
        if channels == 2:
            x = x.reshape(-1, 2).mean(axis=1, dtype=np.float32)
        return x.astype(np.float32, copy=False)
