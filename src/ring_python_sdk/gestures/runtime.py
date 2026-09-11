"""Streaming window/vote/cooldown logic adapted from ai-ring desktop/swipe_runtime.py."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from ring_python_sdk.imu.processor import ImuSample

from .classifier import GESTURE_NAMES, GestureClassifier, GesturePrediction
from .preprocess import ImuGestureAdapter


@dataclass(frozen=True)
class GestureEvent:
    class_id: int
    name: str
    confidence: float
    probabilities: tuple[float, ...]
    timestamp_ms: float


class GestureRecognizer:
    """Recognize swipe, tap and snap from one 200 Hz ring stream.

    Use ``on_sample`` with RingSession raw/delta callbacks, or ``feed`` with
    already adapted [accel m/s², gyro rad/s] samples. Calls are synchronous;
    each ring/stream needs its own instance. Call ``reset`` between captures.
    """

    def __init__(
        self,
        *,
        on_gesture: Callable[[GestureEvent], None] | None = None,
        on_prediction: Callable[[GesturePrediction, float], None] | None = None,
        classifier: GestureClassifier | None = None,
        adapter: ImuGestureAdapter | None = None,
        sample_hz: float = 200.0,
        step_frames: int = 5,
        stable_window_seconds: float = 0.10,
        positive_ratio: float = 1.0,
        reset_delay_frames: int = 10,
        packet_timestamp_tolerance_ms: float = 50.0,
    ) -> None:
        if not math.isfinite(sample_hz) or sample_hz <= 0:
            raise ValueError("sample_hz must be finite and positive")
        if not isinstance(step_frames, int) or step_frames <= 0:
            raise ValueError("step_frames must be a positive integer")
        if not math.isfinite(stable_window_seconds) or stable_window_seconds <= 0:
            raise ValueError("stable_window_seconds must be finite and positive")
        if not math.isfinite(positive_ratio) or not 0 < positive_ratio <= 1:
            raise ValueError("positive_ratio must be in (0, 1]")
        if not isinstance(reset_delay_frames, int) or reset_delay_frames < 0:
            raise ValueError("reset_delay_frames must be a non-negative integer")
        if not math.isfinite(packet_timestamp_tolerance_ms) or packet_timestamp_tolerance_ms < 0:
            raise ValueError("packet_timestamp_tolerance_ms must be finite and non-negative")
        self.classifier = classifier if classifier is not None else GestureClassifier()
        self.adapter = adapter if adapter is not None else ImuGestureAdapter(sample_hz=sample_hz)
        if self.adapter.sample_hz != sample_hz:
            raise ValueError("adapter.sample_hz must match sample_hz")
        self.on_gesture = on_gesture
        self.on_prediction = on_prediction
        self.prediction_count = 0
        self.prediction_counts = [0] * len(GESTURE_NAMES)
        self.gesture_counts = [0] * len(GESTURE_NAMES)
        self.timestamp_jitter_count = 0
        self.max_timestamp_jitter_ms = 0.0
        self.packet_timestamp_tolerance_ms = float(packet_timestamp_tolerance_ms)
        self.reset_count = 0
        self.sample_hz = sample_hz
        self.step_frames = step_frames
        self.reset_delay_frames = reset_delay_frames
        queue_length = max(1, round(stable_window_seconds * sample_hz / step_frames))
        self._threshold = max(1, math.ceil(positive_ratio * queue_length))
        self._samples: deque[np.ndarray] = deque(maxlen=self.classifier.window_size)
        self._history: deque[int] = deque(maxlen=queue_length)
        self.reset()

    def reset(self) -> None:
        """Discard windows, pending votes, cooldown and motion compensation history."""
        self.reset_count += 1
        self._samples.clear()
        self._history.clear()
        self._frames_since_infer = 0
        self._cooldown = 0
        self._last_timestamp: float | None = None
        self._last_sample_index: int | None = None
        self._last_packet_seq: int | None = None
        self.last_prediction: GesturePrediction | None = None
        self.adapter.reset()

    def _check_time(self, timestamp_ms: float, *, packet_boundary: bool = False) -> None:
        if not math.isfinite(timestamp_ms):
            raise ValueError("timestamp_ms must be finite")
        if self._last_timestamp is not None:
            # Device uptime is uint32 milliseconds; wrapping is not a disconnect.
            elapsed = (timestamp_ms - self._last_timestamp) % (1 << 32)
            if elapsed <= 0 or elapsed > 1.5 * 1000.0 / self.sample_hz:
                # With MIC active, firmware packet-tail timestamps jitter even
                # though packet sequence and all sample indexes are continuous.
                # Allow at most one default 10-frame packet (50 ms) of anchor
                # jitter at a contiguous packet boundary. Never bridge lost or
                # reordered packets, sample gaps, within-packet jumps or stalls.
                signed_elapsed = (elapsed + (1 << 31)) % (1 << 32) - (1 << 31)
                jitter_ms = abs(signed_elapsed - 1000.0 / self.sample_hz)
                if packet_boundary and jitter_ms <= self.packet_timestamp_tolerance_ms:
                    self.timestamp_jitter_count += 1
                    self.max_timestamp_jitter_ms = max(self.max_timestamp_jitter_ms, jitter_ms)
                    return
                self.reset()

    def on_sample(self, sample: ImuSample) -> GestureEvent | None:
        """Callback for ``session.imu_on(on_sample=recognizer.on_sample)``."""
        if self._last_sample_index is not None and (
            sample.sample_index != self._last_sample_index + 1
            or sample.packet_seq not in (
                self._last_packet_seq,
                (self._last_packet_seq + 1) & 0xFFFF,
            )
        ):
            self.reset()
        self._check_time(
            sample.uptime_ms,
            packet_boundary=(
                self._last_packet_seq is not None
                and sample.packet_seq == ((self._last_packet_seq + 1) & 0xFFFF)
            ),
        )
        values = self.adapter(sample)
        self._last_sample_index = sample.sample_index
        self._last_packet_seq = sample.packet_seq
        return self._append(values, sample.uptime_ms)

    def feed(
        self, values: Sequence[float] | np.ndarray, *, timestamp_ms: float | None = None
    ) -> GestureEvent | None:
        """Feed one model-frame sample; omitted timestamps advance by one sample period."""
        row = np.asarray(values, dtype=np.float32)
        if row.shape != (6,) or not np.isfinite(row).all():
            raise ValueError("values must contain six finite model-frame IMU values")
        if timestamp_ms is None:
            timestamp_ms = (
                0.0 if self._last_timestamp is None
                else (self._last_timestamp + 1000.0 / self.sample_hz) % (1 << 32)
            )
        self._check_time(timestamp_ms)
        return self._append(row.copy(), timestamp_ms)

    def _append(self, values: np.ndarray, timestamp_ms: float) -> GestureEvent | None:
        self._last_timestamp = timestamp_ms
        self._samples.append(values)
        self._frames_since_infer += 1
        if self._cooldown > 0:
            self._cooldown -= 1
            if self._cooldown == 0:
                # Preserve ai-ring's post-trigger half-window refill and timing.
                self._samples.clear()
                self._samples.extend([values] * (self.classifier.window_size // 2 - 1))
                self._frames_since_infer = 0
                self._history.clear()
            return None
        if (
            len(self._samples) < self.classifier.window_size
            or self._frames_since_infer < self.step_frames
        ):
            return None
        self._frames_since_infer = 0
        prediction = self.classifier.predict(np.asarray(self._samples, dtype=np.float32))
        self.last_prediction = prediction
        self.prediction_count += 1
        self.prediction_counts[prediction.class_id] += 1
        if self.on_prediction is not None:
            self.on_prediction(prediction, timestamp_ms)
        self._history.append(prediction.class_id)
        if len(self._history) < self._history.maxlen:
            return None
        for class_id in sorted(self.classifier.class_ids):
            if class_id == 0 or self._history.count(class_id) < self._threshold:
                continue
            event = GestureEvent(
                class_id=class_id,
                name=GESTURE_NAMES[class_id],
                confidence=prediction.probabilities[class_id],
                probabilities=prediction.probabilities,
                timestamp_ms=timestamp_ms,
            )
            self._history.clear()
            self._frames_since_infer = 0
            self._cooldown = self.reset_delay_frames
            self.gesture_counts[class_id] += 1
            if self.on_gesture is not None:
                self.on_gesture(event)
            return event
        return None
