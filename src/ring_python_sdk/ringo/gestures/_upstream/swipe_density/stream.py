from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .decoder import DecodeResult, EvidencePoolDecoder, StreamingDecoderConfig
from .model import SwipeDensityModel, WindowInference


@dataclass(frozen=True)
class StreamInference:
    window: WindowInference
    decode: DecodeResult
    inference_count: int
    segment_id: int


class SwipeDensityStream:
    maximum_gap_seconds = 0.015

    def __init__(
        self,
        model: SwipeDensityModel,
        decoder_config: StreamingDecoderConfig,
    ) -> None:
        self.model = model
        self.decoder_config = decoder_config
        self._segment_id = -1
        self.reset()

    def reset(self) -> None:
        self._window: deque[tuple[float, ...]] = deque(
            maxlen=self.model.window_size
        )
        self._frame_count = 0
        self._last_timestamp_s: float | None = None
        self._decoder = EvidencePoolDecoder(self.decoder_config)
        self.inference_count = 0
        self._segment_id += 1

    def push_frame(
        self, frame: np.ndarray, timestamp_s: float
    ) -> StreamInference | None:
        if self._last_timestamp_s is not None:
            delta = timestamp_s - self._last_timestamp_s
            if delta <= 0 or delta > self.maximum_gap_seconds:
                self.reset()
        self._last_timestamp_s = timestamp_s
        values = np.asarray(frame, dtype=np.float32)
        if values.shape != (6,):
            raise ValueError(f"Expected one six-axis IMU frame, got {values.shape}")
        self._window.append(tuple(float(value) for value in values))
        self._frame_count += 1
        if len(self._window) < self.model.window_size:
            return None
        window_start = self._frame_count - self.model.window_size
        if window_start % self.model.stride != 0:
            return None

        inference = self.model.predict([tuple(self._window)])[0]
        decode = self._decoder.update(
            inference.frame_density,
            inference.probabilities,
            self.model.gesture_ids,
            window_start,
        )
        self.inference_count += 1
        return StreamInference(
            inference,
            decode,
            self.inference_count,
            self._segment_id,
        )
