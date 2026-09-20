from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import torch

from .snapshot_model import build_model


DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parent
    / "assets"
    / "density-pw64-dh96-swipe3-rot-0913"
)
GESTURE_IDS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13)
GESTURE_LABELS = (
    "empty",
    "swipe-up",
    "swipe-down",
    "swipe-left",
    "swipe-right",
    "swipe-tap",
    "snap",
    "clench",
    "index-pinch",
    "middle-pinch",
    "circle-clockwise",
    "circle-counterclockwise",
)


@dataclass(frozen=True)
class WindowInference:
    probabilities: tuple[float, ...]
    frame_density: tuple[float, ...]


class SwipeDensityModel:
    """Local deployment of IMU-Data-Desktop's default Swipe Density model."""

    window_size = 60
    stride = 20
    sample_rate_hz = 200
    gesture_ids = GESTURE_IDS
    gesture_labels = GESTURE_LABELS

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.model_source = self.model_dir / "swipe_cnn_classification_best.pt"
        state_dict = torch.load(
            self.model_source, map_location="cpu", weights_only=True
        )
        self._network = build_model(state_dict).eval()

    @torch.inference_mode()
    def predict(self, windows) -> tuple[WindowInference, ...]:
        inputs = torch.as_tensor(windows, dtype=torch.float32)
        expected_shape = (len(windows), self.window_size, 6)
        if tuple(inputs.shape) != expected_shape:
            raise ValueError(
                f"Expected IMU windows shape {expected_shape}, got {tuple(inputs.shape)}"
            )

        gyroscope = inputs[..., 3:6]
        gyro_rms = gyroscope.square().mean(dim=(1, 2), keepdim=True).sqrt()
        inputs = inputs.clone()
        inputs[..., 3:6] = gyroscope / gyro_rms.clamp_min(1e-6)

        output = self._network(inputs)
        probabilities = torch.softmax(output["class_logits"], dim=1)
        density = torch.nn.functional.softplus(
            output["event_density_logits"]
        )
        if density.shape[1] != 30:
            raise RuntimeError(
                f"Event density length mismatch: expected=30 actual={density.shape[1]}"
            )
        frame_density = torch.repeat_interleave(density / 2, 2, dim=1)
        probability_rows = probabilities.cpu().tolist()
        density_rows = frame_density.cpu().tolist()
        return tuple(
            WindowInference(
                tuple(float(value) for value in probability_row),
                tuple(float(value) for value in density_rows[row_index]),
            )
            for row_index, probability_row in enumerate(probability_rows)
        )
