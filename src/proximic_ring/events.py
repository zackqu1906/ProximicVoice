from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, Union


@dataclass(frozen=True)
class Stage1Event:
    sample_index: int
    time_s: float
    max_amplitude: float


@dataclass(frozen=True)
class Stage2Event:
    sample_index: int
    time_s: float
    window_start_s: float
    window_end_s: float
    score: float
    logits: Tuple[float, float]
    activated: bool


DetectionEvent = Union[Stage1Event, Stage2Event]


@dataclass
class DetectorStats:
    input_samples: int = 0
    stage1_triggers: int = 0
    stage2_runs: int = 0
    activations: int = 0
