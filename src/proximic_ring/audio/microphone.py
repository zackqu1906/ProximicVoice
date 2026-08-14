from __future__ import annotations

import numpy as np

from .base import AudioSource


class MicrophoneSource(AudioSource):
    """Use an OS-visible audio input device (USB audio / Bluetooth mic / built-in mic)."""

    def __init__(self, device: str | int | None = None):
        self.device = device
        self._stream = None

    def open(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError('Install microphone support with: pip install -e ".[mic]"') from exc
        sd.check_input_settings(
            device=self.device,
            samplerate=16_000,
            channels=1,
            dtype="float32",
        )
        self._stream = sd.InputStream(
            device=self.device,
            samplerate=16_000,
            channels=1,
            dtype="float32",
            blocksize=320,
        )
        self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, frames: int) -> np.ndarray | None:
        if self._stream is None:
            raise RuntimeError("MicrophoneSource is not open")
        data, overflowed = self._stream.read(frames)
        if overflowed:
            # Keep streaming but make the problem visible.
            print("[warning] microphone input overflow; samples may have been dropped")
        return np.asarray(data[:, 0], dtype=np.float32)


def list_input_devices() -> list[tuple[int, str, float, int]]:
    try:
        import sounddevice as sd # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise RuntimeError('Install microphone support with: pip install -e ".[mic]"') from exc
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"], float(d["default_samplerate"]), int(d["max_input_channels"])))
    return out
