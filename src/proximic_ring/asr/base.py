from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


class ASRBackend(Protocol):
    """Minimal contract implemented by every ASR backend.

    Proximity/session code only knows this interface. A backend may run a local
    model, call a remote HTTP service, or wrap any future ASR implementation.
    """

    backend_name: str
    model_name: str

    def transcribe(self, audio_16k: np.ndarray) -> str: ...


@dataclass(frozen=True)
class ASRResult:
    """Normalized result emitted by :class:`ASRWorker`."""

    backend: str
    model: str
    text: str
    latency_s: float
    audio_duration_s: float
    sample_rate: int = 16_000
    error: str | None = None

    @property
    def rtf(self) -> float:
        """Real-time factor: inference seconds / audio seconds."""
        if self.audio_duration_s <= 0:
            return 0.0
        return self.latency_s / self.audio_duration_s
