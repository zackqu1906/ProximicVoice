from __future__ import annotations

from pathlib import Path

import numpy as np


ASSET_DIR = Path(__file__).resolve().parent / "assets"


class LegacyFeatureExtractor:
    """Python reproduction of fft.h::filter_bank_feature().

    Input:  8000 float32 samples at 8 kHz (1 second)
    Output: float32 array shaped (20, 201)

    It reproduces:
      * n_fft = sr/50 = 160
      * hop = n_fft/4 = 40
      * periodic Hann window
      * centered STFT with reflect padding
      * magnitude (not power) spectrum
      * amplitude_to_db with amin=1e-5 and top_db=80
      * the exact 20x81 filter bank exported in parameters.h
    """

    def __init__(self, filter_bank_path: str | Path | None = None):
        path = Path(filter_bank_path) if filter_bank_path else ASSET_DIR / "filter_bank.npy"
        self.filter_bank = np.load(path).astype(np.float32, copy=False)
        if self.filter_bank.shape != (20, 81):
            raise ValueError(f"Expected filter bank shape (20, 81), got {self.filter_bank.shape}")

        n_fft = 160
        n = np.linspace(0.0, n_fft - 1, n_fft, dtype=np.float32)
        self.window = (
            np.float32(0.5)
            * (np.float32(1.0) - np.cos(n * np.float32(2.0 * np.pi / n_fft)))
        ).astype(np.float32)

    def extract(self, audio_8k: np.ndarray) -> np.ndarray:
        y = np.asarray(audio_8k, dtype=np.float32)
        if y.ndim != 1 or y.size != 8_000:
            raise ValueError(f"Expected one second / 8000 samples, got shape {y.shape}")

        n_fft = 160
        hop = 40
        pad = n_fft // 2

        padded = np.pad(y, (pad, pad), mode="reflect")
        frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft)[::hop]
        if frames.shape != (201, 160):
            raise RuntimeError(f"Unexpected STFT frame shape {frames.shape}")

        windowed = (frames * self.window[None, :]).astype(np.float32, copy=False)
        spectrum = np.fft.rfft(windowed, n=n_fft, axis=1)
        magnitude = np.abs(spectrum).astype(np.float32).T  # (81, 201)

        amin = np.float32(1e-5)
        db = (np.float32(20.0) * np.log10(np.maximum(magnitude, amin))).astype(np.float32)
        floor = np.float32(np.max(db) - np.float32(80.0))
        db = np.maximum(db, floor).astype(np.float32, copy=False)

        features = (self.filter_bank @ db).astype(np.float32)
        if features.shape != (20, 201):
            raise RuntimeError(f"Unexpected feature shape {features.shape}")
        return np.ascontiguousarray(features)
