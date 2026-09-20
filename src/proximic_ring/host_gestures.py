"""Application wiring for the ringo SDK's CPU density gesture model.

The vendored SDK owns model preparation, inference and evidence decoding. This
module preserves application names, worker diagnostics and bounded MIC packet
clock jitter handling. It never enables firmware gesture recognition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import sys

import numpy as np

from ring_python_sdk.ringo.imu import ImuAdapter
from ring_python_sdk.ringo.gestures import (
    GESTURE_IDS, GESTURE_LABELS, GestureRecognizer as DensityRecognizer,
)
from ring_python_sdk.ringo.gestures.model import DEFAULT_MODEL_DIR


MODEL_PATH = DEFAULT_MODEL_DIR / "swipe_cnn_classification_best.pt"
MODEL_NAME = DEFAULT_MODEL_DIR.name
SDK_REVISION = "d7353047ad92006143c772e765c3f17a72c6290b"
# Keep previously saved action assignments valid. Pinch classes stay distinct.
GESTURE_NAMES = tuple("tap" if name == "swipe-tap" else name for name in GESTURE_LABELS)


@dataclass(frozen=True)
class GesturePrediction:
    class_id: int
    name: str
    confidence: float
    probabilities: tuple[float, ...]
    class_ids: tuple[int, ...] = GESTURE_IDS


@dataclass(frozen=True)
class GestureEvent(GesturePrediction):
    timestamp_ms: float = 0.0
    raw: object | None = None


def _prediction(probabilities) -> GesturePrediction:
    index = int(np.argmax(probabilities))
    return GesturePrediction(
        GESTURE_IDS[index], GESTURE_NAMES[index], float(probabilities[index]),
        tuple(float(value) for value in probabilities),
    )


class GestureClassifier:
    """Single-window warmup/diagnostics around the unmodified density model."""

    class_ids = GESTURE_IDS
    class_names = GESTURE_NAMES
    window_size = 60

    def __init__(self, model=None):
        if model is None:
            from ring_python_sdk.ringo.gestures import SwipeDensityModel
            model = SwipeDensityModel()
        self.model = model
        self._torch = sys.modules.get("torch")

    def predict(self, values) -> GesturePrediction:
        window = np.asarray(values, dtype=np.float32)
        if window.shape != (self.window_size, 6) or not np.isfinite(window).all():
            raise ValueError("IMU window must contain 60 x 6 finite values")
        return _prediction(self.model.predict(window[None, ...])[0].probabilities)


class GestureRecognizer:
    """Serial SDK sample consumer for GestureWorker, with lifetime counters."""

    gesture_names = GESTURE_NAMES
    sample_hz = 200.0

    def __init__(
        self, *, model=None, on_gesture: Callable | None = None,
        on_prediction: Callable | None = None, packet_timestamp_tolerance_ms: float = 50.0,
    ):
        if not math.isfinite(packet_timestamp_tolerance_ms) or packet_timestamp_tolerance_ms < 0:
            raise ValueError("packet_timestamp_tolerance_ms must be finite and non-negative")
        self.classifier = GestureClassifier(model)
        self.native = DensityRecognizer(model=self.classifier.model)
        self.adapter = ImuAdapter()
        self.on_gesture = on_gesture
        self.on_prediction = on_prediction
        self.packet_timestamp_tolerance_ms = float(packet_timestamp_tolerance_ms)
        self.prediction_count = 0
        self.prediction_counts = [0] * len(GESTURE_NAMES)
        self.gesture_counts = [0] * len(GESTURE_NAMES)
        self.reset_count = 0
        self.timestamp_jitter_count = 0
        self.max_timestamp_jitter_ms = 0.0
        self.reset()

    def reset(self):
        self.native.reset()
        self.adapter.reset()
        self.last_prediction = None
        self._last_sample = None
        self._model_timestamp_ms = None
        self.reset_count += 1

    def on_sample(self, sample) -> tuple[GestureEvent, ...]:
        if not math.isfinite(sample.uptime_ms):
            raise ValueError("timestamp_ms must be finite")
        values = self.adapter(sample)
        if not np.isfinite(values).all():
            raise ValueError("IMU sample must contain six finite physical values")
        previous = self._last_sample
        elapsed = None
        if previous is not None:
            contiguous = (
                sample.sample_index == previous.sample_index + 1
                and sample.packet_seq in (previous.packet_seq, (previous.packet_seq + 1) & 0xFFFF)
            )
            elapsed = (sample.uptime_ms - previous.uptime_ms + (1 << 31)) % (1 << 32) - (1 << 31)
            boundary = sample.packet_seq == ((previous.packet_seq + 1) & 0xFFFF)
            jitter = abs(elapsed - 5.0)
            if not contiguous:
                self.reset()
            elif boundary and (elapsed <= 0 or elapsed > 7.5) and jitter <= self.packet_timestamp_tolerance_ms:
                # MIC traffic can shift whole packet tails despite contiguous
                # sequence/sample numbers. Adjust only the inference clock;
                # report the original unwrapped device time in every event.
                elapsed = 5.0
                self.timestamp_jitter_count += 1
                self.max_timestamp_jitter_ms = max(self.max_timestamp_jitter_ms, jitter)
            elif elapsed <= 0 or elapsed > 15.0:
                self.reset()
        message = self.adapter.to_message(sample)
        device_ms = message["local_timestamp"] * 1000.0
        model_ms = (
            device_ms if previous is None or self._model_timestamp_ms is None
            else self._model_timestamp_ms + elapsed
        )
        self._last_sample = sample
        return self._feed(values, model_ms, device_ms)

    def feed(self, values, *, timestamp_ms: float | None = None) -> tuple[GestureEvent, ...]:
        """Feed host-axis m/s² and rad/s; use on_sample for SDK physical samples."""
        if timestamp_ms is None:
            timestamp_ms = 0.0 if self._model_timestamp_ms is None else self._model_timestamp_ms + 5.0
        self._last_sample = None
        return self._feed(values, timestamp_ms, timestamp_ms)

    def _feed(self, values, model_ms, device_ms) -> tuple[GestureEvent, ...]:
        row = np.asarray(values, dtype=np.float32)
        if row.shape != (6,) or not np.isfinite(row).all():
            raise ValueError("values must contain six finite host-frame IMU values")
        if not math.isfinite(model_ms):
            raise ValueError("timestamp_ms must be finite")
        before = self.native.last_inference
        if self._model_timestamp_ms is not None:
            delta = model_ms / 1000.0 - self._model_timestamp_ms / 1000.0
            if delta <= 0 or delta > self.native.stream.maximum_gap_seconds:
                self.reset_count += 1
                self.last_prediction = None
        events = self.native.feed(row, model_ms / 1000.0)
        inference = self.native.last_inference
        self._model_timestamp_ms = model_ms
        if inference is not None and inference is not before:
            prediction = _prediction(inference.window.probabilities)
            self.last_prediction = prediction
            self.prediction_count += 1
            self.prediction_counts[GESTURE_IDS.index(prediction.class_id)] += 1
            if self.on_prediction is not None:
                self.on_prediction(prediction, device_ms)
        mapped = tuple(
            GestureEvent(
                event.class_id, GESTURE_NAMES[GESTURE_IDS.index(event.class_id)],
                event.confidence, event.probabilities, event.class_ids, device_ms, event,
            ) for event in events
        )
        for event in mapped:
            self.gesture_counts[GESTURE_IDS.index(event.class_id)] += 1
            if self.on_gesture is not None:
                self.on_gesture(event)
        return mapped
