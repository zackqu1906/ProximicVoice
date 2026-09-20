from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace

import numpy as np


@dataclass(frozen=True)
class StreamingDecoderConfig:
    threshold: float = 0.55
    minimum_class_confidence: float = 0.55
    observation_frames: int = 30
    event_distance_frames: int = 28
    local_mass_radius_frames: int = 6
    buffer_frames: int = 120


@dataclass(frozen=True)
class StreamingEvent:
    center: int
    label: int
    class_confidence: float
    emitted_after_frame: int
    event_mass: float

    @property
    def latency_frames(self) -> int:
        return self.emitted_after_frame - self.center


@dataclass(frozen=True)
class PeakEvidence:
    position: int
    mass: float
    label: int
    confidence: float
    maturity: float
    emitted: bool


@dataclass(frozen=True)
class DecodeResult:
    density: tuple[float, ...]
    evidence: tuple[PeakEvidence, ...]
    events: tuple[StreamingEvent, ...]


@dataclass(frozen=True)
class _Peak:
    center: int
    mass: float


class EvidencePoolDecoder:
    """Exact local copy of IMU-Data-Desktop's default evidence-pool decoder."""

    _EMPTY_LABEL = 0

    def __init__(self, config: StreamingDecoderConfig) -> None:
        self._config = config
        self.reset()

    @property
    def threshold(self) -> float:
        return self._config.threshold

    def set_threshold(self, threshold: float) -> None:
        self._config = replace(self._config, threshold=threshold)

    def _shift_left(self, values: np.ndarray, shift: int) -> None:
        if shift >= len(values):
            values.fill(0)
        elif shift > 0:
            values[:-shift] = values[shift:]
            values[-shift:] = 0

    def _find_peaks(self, density: np.ndarray) -> tuple[_Peak, ...]:
        left = np.empty_like(density)
        right = np.empty_like(density)
        left[0], left[1:] = -np.inf, density[:-1]
        right[-1], right[:-1] = -np.inf, density[1:]
        centers = np.flatnonzero(
            (density > left) & (density >= right) & (density > 0.0)
        )
        radius = self._config.local_mass_radius_frames
        candidates = (
            _Peak(
                self._buffer_start + int(center),
                float(
                    density[
                        max(0, center - radius) : center + radius + 1
                    ].sum(dtype=np.float64)
                ),
            )
            for center in centers
        )
        selected = []
        for peak in sorted(
            (peak for peak in candidates if peak.mass >= self.threshold),
            key=lambda peak: peak.mass,
            reverse=True,
        ):
            if all(
                abs(peak.center - accepted.center)
                >= self._config.event_distance_frames
                for accepted in selected
            ):
                selected.append(peak)
        return tuple(sorted(selected, key=lambda peak: peak.center))

    def _classify_peak(
        self,
        center: int,
        peak_centers: np.ndarray,
        gesture_ids: tuple[int, ...],
    ) -> tuple[int, float]:
        starts = np.asarray(
            [start for start, _, _ in self._window_history], dtype=np.int64
        )
        probabilities = np.asarray(
            [values for _, _, values in self._window_history], dtype=np.float32
        )
        left_context = peak_centers[:, None] - starts
        right_context = self._window_size - 1 - left_context
        inside = (left_context >= 0) & (right_context >= 0)
        context = np.where(
            inside,
            np.minimum(left_context, right_context) * (self._window_size + 1)
            + right_context,
            -1,
        )
        target = int(np.flatnonzero(peak_centers == center)[0])
        owned = np.flatnonzero(
            (context.argmax(axis=0) == target) & (context[target] >= 0)
        )
        if not len(owned):
            return self._EMPTY_LABEL, 0.0
        densities = np.asarray(
            [density for _, density, _ in self._window_history]
        )
        owned_probabilities = probabilities[owned]
        if probabilities.ndim == 3:
            bin_width = self._window_size // probabilities.shape[-1]
            positions = (center - starts[owned]) // bin_width
            owned_probabilities = owned_probabilities[
                np.arange(len(owned)), :, positions
            ]
        weights = np.maximum(
            densities[owned, center - starts[owned]], 0.0
        )
        nonempty = np.asarray(gesture_ids) != self._EMPTY_LABEL
        weights *= owned_probabilities[:, nonempty].sum(axis=1) ** 2.0
        total = float(weights.sum())
        if total <= 1e-12:
            return self._EMPTY_LABEL, 0.0
        pooled = (owned_probabilities * weights[:, None]).sum(axis=0) / total
        class_index = int(pooled.argmax())
        return gesture_ids[class_index], float(pooled[class_index])

    def reset(self) -> None:
        self._density_sum = np.zeros(
            self._config.buffer_frames, dtype=np.float32
        )
        self._density_count = np.zeros(
            self._config.buffer_frames, dtype=np.int32
        )
        self._density_mean = np.zeros(
            self._config.buffer_frames, dtype=np.float32
        )
        self._buffer_start = 0
        self._window_size = 0
        self._window_history: deque[
            tuple[int, np.ndarray, np.ndarray]
        ] = deque()
        self._emitted_centers: list[int] = []

    def update(
        self,
        frame_density: tuple[float, ...],
        probabilities: tuple[float, ...] | np.ndarray,
        gesture_ids: tuple[int, ...],
        window_start: int,
    ) -> DecodeResult:
        density = np.asarray(frame_density, dtype=np.float32)
        self._window_size = len(density)
        window_end = window_start + self._window_size
        next_buffer_start = max(
            0, window_end - self._config.buffer_frames
        )
        shift = next_buffer_start - self._buffer_start
        self._shift_left(self._density_sum, shift)
        self._shift_left(self._density_count, shift)
        self._buffer_start = next_buffer_start

        while (
            self._window_history
            and self._window_history[0][0] + self._window_size
            <= self._buffer_start
        ):
            self._window_history.popleft()
        self._window_history.append(
            (
                window_start,
                density.copy(),
                np.asarray(probabilities, dtype=np.float32),
            )
        )

        left = window_start - self._buffer_start
        self._density_sum[left : left + self._window_size] += density
        self._density_count[left : left + self._window_size] += 1
        self._density_mean.fill(0.0)
        np.divide(
            self._density_sum,
            self._density_count,
            out=self._density_mean,
            where=self._density_count > 0,
        )
        visible_density = self._density_mean[
            : window_end - self._buffer_start
        ]
        peaks = self._find_peaks(visible_density)
        self._emitted_centers = [
            center
            for center in self._emitted_centers
            if center
            >= self._buffer_start - self._config.event_distance_frames
        ]

        peak_centers = np.asarray(
            [peak.center for peak in peaks], dtype=np.int64
        )
        events = []
        evidence = []
        for peak in peaks:
            emitted = any(
                abs(peak.center - emitted_center)
                < self._config.event_distance_frames
                for emitted_center in self._emitted_centers
            )
            label, confidence = self._classify_peak(
                peak.center, peak_centers, gesture_ids
            )
            age = window_end - 1 - peak.center
            accepted = (
                not emitted
                and age >= self._config.observation_frames
                and label != self._EMPTY_LABEL
                and confidence >= self._config.minimum_class_confidence
            )
            evidence.append(
                PeakEvidence(
                    peak.center - self._buffer_start,
                    peak.mass,
                    label,
                    confidence,
                    min(1.0, age / self._config.observation_frames),
                    emitted or accepted,
                )
            )
            if not accepted:
                continue
            events.append(
                StreamingEvent(
                    peak.center,
                    label,
                    confidence,
                    window_end - 1,
                    peak.mass,
                )
            )
            self._emitted_centers.append(peak.center)

        return DecodeResult(
            tuple(float(value) for value in visible_density),
            tuple(evidence),
            tuple(events),
        )
