from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


class AudioSource(ABC):
    """Blocking mono float32 audio source.

    Every implementation must expose 16 kHz mono samples to the detector.
    ``read(frames)`` returns up to ``frames`` samples, or None at EOF.
    Live sources normally return exactly the requested frame count.
    """

    sample_rate: int = 16_000

    def __enter__(self) -> "AudioSource":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    @abstractmethod
    def read(self, frames: int) -> Optional[np.ndarray]:
        raise NotImplementedError
