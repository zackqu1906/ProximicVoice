from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class DetectorConfig:
    """Runtime constants reproduced from the WearOS implementation.

    The pretrained checkpoint and feature extractor are tied to the 16 kHz ->
    8 kHz path. Thresholds and timing can be changed, but sample rates and the
    one-second context should only be changed together with model validation or
    retraining.
    """

    input_sample_rate: int = 16_000
    chunk_samples: int = 320                 # 20 ms at 16 kHz
    context_samples: int = 16_000             # 1 s circular buffer
    model_sample_rate: int = 8_000
    model_input_samples: int = 8_000          # 1 s at 8 kHz

    stage1_threshold: float = 0.30
    stage2_delay_s: float = 0.50
    stage2_threshold: float = 1.00

    def validate(self) -> "DetectorConfig":
        if self.input_sample_rate != 16_000:
            raise ValueError("The legacy ProxiMic detector expects 16 kHz input.")
        if self.model_sample_rate != 8_000 or self.model_input_samples != 8_000:
            raise ValueError("The supplied ProxiMic model expects exactly 1 s at 8 kHz.")
        if self.context_samples != self.input_sample_rate:
            raise ValueError("The legacy detector context must be exactly 1 second.")
        if self.chunk_samples <= 0:
            raise ValueError("chunk_samples must be positive")
        if self.stage2_delay_s < 0:
            raise ValueError("stage2_delay_s cannot be negative")
        return self

    @property
    def chunk_seconds(self) -> float:
        return self.chunk_samples / self.input_sample_rate

    @property
    def stage2_delay_samples(self) -> int:
        return int(self.stage2_delay_s * self.input_sample_rate)

    def with_overrides(self, **kwargs) -> "DetectorConfig":
        return replace(self, **kwargs).validate()
