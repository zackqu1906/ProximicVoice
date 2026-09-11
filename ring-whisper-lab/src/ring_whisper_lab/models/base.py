from typing import Protocol
import numpy as np


class StreamingEnhancer(Protocol):
    """Contract for future single-ring enhancement models, not an implementation.

    Preserve amplitude calibration. Declare buffering/algorithmic delay so the
    host can align enhanced audio with detector events. Reset between sessions.
    """
    sample_rate: int
    algorithmic_delay_samples: int

    def reset(self) -> None: ...

    def process(self, pcm_16k: np.ndarray) -> np.ndarray: ...

    def flush(self) -> np.ndarray: ...
