from __future__ import annotations

from pathlib import Path

import numpy as np


ASSET_DIR = Path(__file__).resolve().parent / "assets"


class LegacyDownsampler16kTo8k:
    """Reproduce resample.h for the only path used by ProxiMic: 16 kHz -> 8 kHz.

    The original C++ uses the resampy ``kaiser_best`` interpolation table that
    was exported into parameters.h. For an exact 2:1 ratio the fractional phase
    is always zero, so the original algorithm simplifies to the two weighted
    dot products below. Keeping the original coefficient table avoids library-
    version differences.
    """

    def __init__(self, interp_win_path: str | Path | None = None):
        path = Path(interp_win_path) if interp_win_path else ASSET_DIR / "interp_win.npy"
        self.interp_win = np.load(path).astype(np.float32, copy=False)
        if self.interp_win.ndim != 1 or self.interp_win.size != 32_769:
            raise ValueError("Unexpected interpolation table")
        self.precision = 512
        self.ratio = 0.5
        self.index_step = int(self.ratio * self.precision)  # 256

    def __call__(self, audio_16k: np.ndarray) -> np.ndarray:
        x = np.asarray(audio_16k, dtype=np.float32)
        if x.ndim != 1 or x.size != 16_000:
            raise ValueError(f"Expected 16000 samples, got shape {x.shape}")

        y = np.zeros(8_000, dtype=np.float32)
        win = self.interp_win
        step = self.index_step
        nwin = win.size

        # This is the exact 2:1 specialization of resample.h. Using slices keeps
        # the Python loop small; each sample is a vectorized float32 dot product.
        max_left_taps = nwin // step
        max_right_taps = (nwin - step) // step
        left_weights = win[0 : max_left_taps * step : step]
        right_weights = win[step : step + max_right_taps * step : step]

        for t in range(8_000):
            n = 2 * t

            i_max = min(n + 1, max_left_taps)
            if i_max:
                y[t] += np.dot(left_weights[:i_max], x[n::-1][:i_max])

            k_max = min(16_000 - n - 1, max_right_taps)
            if k_max:
                y[t] += np.dot(right_weights[:k_max], x[n + 1 : n + 1 + k_max])

        return y
