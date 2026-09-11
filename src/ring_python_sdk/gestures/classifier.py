"""CPU inference for the bundled seven-class IMU gesture model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import ArrayLike


GESTURE_NAMES = (
    "empty", "swipe-up", "swipe-down", "swipe-left", "swipe-right", "tap", "snap"
)


@dataclass(frozen=True)
class GesturePrediction:
    class_id: int
    name: str
    confidence: float
    probabilities: tuple[float, ...]


class GestureClassifier:
    """Classify 60 model-frame IMU samples (acceleration m/s², gyro rad/s).

    Columns are ``ax, ay, az, gx, gy, gz``, after ai-ring mount compensation.
    Pass SDK physical callbacks through ``ImuGestureAdapter`` first. A custom
    checkpoint must match the bundled architecture and seven-class ordering.
    """

    class_ids = (0, 1, 2, 3, 4, 5, 6)
    class_names = GESTURE_NAMES
    window_size = 60

    def __init__(self, checkpoint_path: str | Path | None = None):
        try:
            import torch
        except ImportError as exc:
            raise ImportError(
                "Gesture classification requires PyTorch. "
                "Install it with: pip install 'ring-python-sdk[gestures]'"
            ) from exc
        from ._model import SwipeInceptionTimeDW

        self._torch = torch
        assets = Path(__file__).with_name("assets")
        config = json.loads((assets / "swipe.json").read_text(encoding="utf-8"))
        self.zscore_mean = np.asarray(config["mean"], dtype=np.float32)
        self.zscore_std = np.asarray(config["std"], dtype=np.float32)
        checkpoint = assets / "swipe.pt" if checkpoint_path is None else Path(checkpoint_path)
        state_dict = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if isinstance(state_dict, dict) and "state_dict" in state_dict:
            state_dict = state_dict["state_dict"]
        self.model = SwipeInceptionTimeDW(**config["model_kwargs"]).to("cpu")
        self.model.load_state_dict(state_dict, strict=True)
        self.model.eval()

    def prepare(self, values: ArrayLike) -> np.ndarray:
        """Validate and normalize one 60×6 window without changing the input."""
        values = np.asarray(values, dtype=np.float32)
        if values.shape != (self.window_size, 6):
            raise ValueError(
                f"IMU window must have shape ({self.window_size}, 6), got {values.shape}"
            )
        if not np.isfinite(values).all():
            raise ValueError("IMU window must contain only finite values")
        return ((values - self.zscore_mean) / self.zscore_std).astype(np.float32, copy=False)

    def predict(self, values: ArrayLike) -> GesturePrediction:
        """Return the top class and probabilities in ``class_ids`` order."""
        prepared = self.prepare(values)
        with self._torch.inference_mode():
            logits = self.model(self._torch.from_numpy(prepared).unsqueeze(0))
            probabilities = self._torch.softmax(logits[0], dim=0).cpu().numpy()
        class_id = int(np.argmax(probabilities))
        return GesturePrediction(
            class_id=class_id,
            name=self.class_names[class_id],
            confidence=float(probabilities[class_id]),
            probabilities=tuple(float(value) for value in probabilities),
        )
