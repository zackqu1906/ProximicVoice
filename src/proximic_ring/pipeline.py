from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .features import LegacyFeatureExtractor
from .model import ProxiMicModel
from .resample import LegacyDownsampler16kTo8k


@dataclass(frozen=True)
class InferenceResult:
    logits: tuple[float, float]
    score: float


class InferencePipeline(Protocol):
    def infer_window(self, audio_16k: np.ndarray) -> InferenceResult: ...


class LegacyInferencePipeline:
    def __init__(
        self,
        model: ProxiMicModel | None = None,
        downsampler: LegacyDownsampler16kTo8k | None = None,
        features: LegacyFeatureExtractor | None = None,
    ):
        self.model = model or ProxiMicModel()
        self.downsampler = downsampler or LegacyDownsampler16kTo8k()
        self.features = features or LegacyFeatureExtractor()

    def infer_window(self, audio_16k: np.ndarray) -> InferenceResult:
        audio_8k = self.downsampler(audio_16k)
        feat = self.features.extract(audio_8k)
        logits, score = self.model.infer(feat)
        return InferenceResult(logits=(float(logits[0]), float(logits[1])), score=score)
