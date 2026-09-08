from __future__ import annotations

from typing import Iterable, List

import numpy as np

from .config import DetectorConfig
from .events import DetectionEvent, DetectorStats, Stage1Event, Stage2Event
from .pipeline import InferencePipeline, LegacyInferencePipeline


class _CircularAudioBuffer:
    def __init__(self, size: int):
        self.data = np.zeros(size, dtype=np.float32)
        self.pos = 0

    def append(self, samples: np.ndarray) -> None:
        x = np.asarray(samples, dtype=np.float32)
        n = x.size
        if n >= self.data.size:
            self.data[:] = x[-self.data.size :]
            self.pos = 0
            return
        end = self.pos + n
        if end <= self.data.size:
            self.data[self.pos : end] = x
        else:
            first = self.data.size - self.pos
            self.data[self.pos :] = x[:first]
            self.data[: end - self.data.size] = x[first:]
        self.pos = end % self.data.size

    def ordered(self) -> np.ndarray:
        if self.pos == 0:
            return self.data.copy()
        return np.concatenate((self.data[self.pos :], self.data[: self.pos])).astype(np.float32, copy=False)


class ProxiMicDetector:
    """ProxiMic two-stage detector with faster active-session evidence.

    The first Stage2 uses a 0.3 s delay and the original one-second model window.
    Once the session controller confirms activation, the same rolling one-second
    window is evaluated every 0.2 s until that session ends.

    Arbitrary input block sizes are accepted, but internally they are re-chunked
    to 320 samples so Stage 1 preserves the WearOS 20 ms decision cadence.
    """

    def __init__(
        self,
        config: DetectorConfig | None = None,
        pipeline: InferencePipeline | None = None,
    ):
        self.config = (config or DetectorConfig()).validate()
        self.pipeline = pipeline or LegacyInferencePipeline()
        self.stats = DetectorStats()
        self._buffer = _CircularAudioBuffer(self.config.context_samples)
        self._pending = np.empty(0, dtype=np.float32)
        self._now_sample = 0
        self._stage2_due_sample: int | None = None
        self._continuation_mode = False
        self._last_stage1_heartbeat_sample: int | None = None

    @property
    def now_seconds(self) -> float:
        return self._now_sample / self.config.input_sample_rate

    def reset(self) -> None:
        self.stats = DetectorStats()
        self._buffer = _CircularAudioBuffer(self.config.context_samples)
        self._pending = np.empty(0, dtype=np.float32)
        self._now_sample = 0
        self._stage2_due_sample = None
        self._continuation_mode = False
        self._last_stage1_heartbeat_sample = None

    def set_continuation_mode(self, enabled: bool) -> None:
        """Select the active-session Stage2 cadence.

        The ASR session controller owns the session boundary. Cancelling a
        pending active cadence here prevents detector evidence from leaking
        into the next utterance.
        """
        normalized = bool(enabled)
        if normalized == self._continuation_mode:
            return
        self._continuation_mode = normalized
        self._last_stage1_heartbeat_sample = None
        self._stage2_due_sample = (
            self._now_sample + self.config.stage2_active_interval_samples
            if normalized
            else None
        )

    def feed(self, samples: np.ndarray | Iterable[float]) -> List[DetectionEvent]:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return []
        if not np.all(np.isfinite(x)):
            raise ValueError("Audio contains NaN or infinity")

        self._pending = np.concatenate((self._pending, x))
        events: List[DetectionEvent] = []
        n = self.config.chunk_samples

        while self._pending.size >= n:
            chunk = self._pending[:n]
            self._pending = self._pending[n:]
            events.extend(self._process_chunk(chunk))
        return events

    def _process_chunk(self, chunk: np.ndarray) -> List[DetectionEvent]:
        cfg = self.config
        self._buffer.append(chunk)
        self._now_sample += chunk.size
        self.stats.input_samples += chunk.size
        events: List[DetectionEvent] = []

        max_amp = float(np.max(np.abs(chunk)))

        if self._continuation_mode and max_amp > cfg.stage1_threshold:
            # Periodic Stage2 no longer depends on a fresh Stage1 trigger, but
            # the ASR controller still needs a bounded Stage1 heartbeat for its
            # true-silence fallback. Emit at most once per active interval.
            heartbeat_due = (
                self._last_stage1_heartbeat_sample is None
                or self._now_sample - self._last_stage1_heartbeat_sample
                >= cfg.stage2_active_interval_samples
            )
            if heartbeat_due:
                self.stats.stage1_triggers += 1
                self._last_stage1_heartbeat_sample = self._now_sample
                events.append(
                    Stage1Event(
                        sample_index=self._now_sample,
                        time_s=self._now_sample / cfg.input_sample_rate,
                        max_amplitude=max_amp,
                    )
                )

        if self._stage2_due_sample is None:
            if self._continuation_mode:
                self._stage2_due_sample = (
                    self._now_sample + cfg.stage2_active_interval_samples
                )
            elif max_amp > cfg.stage1_threshold:
                self.stats.stage1_triggers += 1
                self._stage2_due_sample = self._now_sample + cfg.stage2_delay_samples
                events.append(
                    Stage1Event(
                        sample_index=self._now_sample,
                        time_s=self._now_sample / cfg.input_sample_rate,
                        max_amplitude=max_amp,
                    )
                )
        elif self._now_sample >= self._stage2_due_sample:
            self._stage2_due_sample = (
                self._now_sample + cfg.stage2_active_interval_samples
                if self._continuation_mode
                else None
            )
            window = self._buffer.ordered()
            result = self.pipeline.infer_window(window)
            self.stats.stage2_runs += 1
            activated = result.score > cfg.stage2_threshold
            if activated:
                self.stats.activations += 1

            events.append(
                Stage2Event(
                    sample_index=self._now_sample,
                    time_s=self._now_sample / cfg.input_sample_rate,
                    window_start_s=(self._now_sample - cfg.context_samples) / cfg.input_sample_rate,
                    window_end_s=self._now_sample / cfg.input_sample_rate,
                    score=float(result.score),
                    logits=(float(result.logits[0]), float(result.logits[1])),
                    activated=activated,
                )
            )

        return events
