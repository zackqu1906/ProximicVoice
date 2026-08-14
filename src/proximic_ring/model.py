from __future__ import annotations

from pathlib import Path
from typing import Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


ASSET_DIR = Path(__file__).resolve().parent / "assets"


class CnnNet8(nn.Module):
    """The exact network definition present in get_para.py."""

    def __init__(self, in_channels: int = 20, out_channels: int = 2):
        super().__init__()
        self.conv1 = nn.Conv1d(in_channels, 50, kernel_size=3)
        self.batchnorm1 = nn.BatchNorm1d(50)
        self.pool1 = nn.MaxPool1d(kernel_size=3)
        self.conv2 = nn.Conv1d(50, 100, kernel_size=3)
        self.batchnorm2 = nn.BatchNorm1d(100)
        self.fc1 = nn.Linear(100, 20)
        self.fc2 = nn.Linear(20, out_channels)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.pool1(F.relu(self.batchnorm1(self.conv1(x))))
        x = torch.max(F.relu(self.batchnorm2(self.conv2(x))), dim=2).values
        x = x.view(-1, 100)
        x = F.relu(self.fc1(x))
        return self.fc2(x)


class ProxiMicModel:
    def __init__(self, checkpoint_path: str | Path | None = None, device: str = "cpu"):
        self.device = torch.device(device)
        self.net = CnnNet8(20, 2).to(self.device)
        path = Path(checkpoint_path) if checkpoint_path else ASSET_DIR / "speech-xiaomi.model"

        # weights_only exists on recent PyTorch; fallback keeps compatibility.
        try:
            state = torch.load(path, map_location=self.device, weights_only=True)
        except TypeError:
            state = torch.load(path, map_location=self.device)
        self.net.load_state_dict(state, strict=True)
        self.net.eval()

    @torch.inference_mode()
    def infer(self, features: np.ndarray) -> Tuple[np.ndarray, float]:
        x = np.asarray(features, dtype=np.float32)
        if x.shape != (20, 201):
            raise ValueError(f"Expected feature shape (20, 201), got {x.shape}")
        tensor = torch.from_numpy(np.ascontiguousarray(x)).unsqueeze(0).to(self.device)
        logits_t = self.net(tensor)[0]
        logits = logits_t.detach().cpu().numpy().astype(np.float32)
        score = float(logits[0] - logits[1])
        return logits, score
