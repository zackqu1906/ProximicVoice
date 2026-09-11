"""Depthwise-separable InceptionTime classifier for IMU gesture windows.

Copied from dBHz01/ai-ring, feat/ringo, checkpoint/swipe/cls/model.py.
"""

from __future__ import annotations

import torch
import torch.nn as nn

class Tokenizer(nn.Module):
    """Apply a learned channel transform independently at every time step.

    Input and output use [batch, time, channels] layout.
    """

    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.transform = nn.Sequential(
            nn.Linear(in_channels, out_channels),
            nn.SiLU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.transform(x)


class DepthwiseSeparableConv2D(nn.Module):
    """Height-one depthwise convolution followed by pointwise channel mixing."""

    def __init__(self, in_channels: int, out_channels: int, kernel_size: int):
        super().__init__()
        self.depthwise = nn.Conv2d(
            in_channels,
            in_channels,
            kernel_size=(1, kernel_size),
            padding=(0, kernel_size // 2),
            groups=in_channels,
            bias=False,
        )
        self.pointwise = nn.Conv2d(in_channels, out_channels, 1, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.pointwise(self.depthwise(x))


class DepthwiseInceptionModule2D(nn.Module):
    def __init__(
        self,
        in_channels: int,
        filters: int,
        kernel_sizes: tuple[int, ...],
        bottleneck_channels: int,
        pool_filters: int,
    ):
        super().__init__()
        if any(kernel <= 0 or kernel % 2 == 0 for kernel in kernel_sizes):
            raise ValueError(f"kernel_sizes must be positive odd integers, got {kernel_sizes}")
        if in_channels > 1 and bottleneck_channels > 0:
            self.bottleneck = nn.Conv2d(in_channels, bottleneck_channels, 1, bias=False)
            branch_input = bottleneck_channels
        else:
            self.bottleneck = nn.Identity()
            branch_input = in_channels
        self.convolutions = nn.ModuleList(
            [
                nn.Sequential(
                    DepthwiseSeparableConv2D(branch_input, filters, kernel),
                    nn.BatchNorm2d(filters),
                    nn.ReLU(),
                )
                for kernel in kernel_sizes
            ]
        )
        if pool_filters > 0:
            self.pool_branch = nn.Sequential(
                nn.MaxPool2d((1, 3), stride=(1, 1), padding=(0, 1)),
                nn.Conv2d(in_channels, pool_filters, 1, bias=False),
                nn.BatchNorm2d(pool_filters),
                nn.ReLU(),
            )
        else:
            self.pool_branch = None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        bottleneck = self.bottleneck(x)
        branches = [convolution(bottleneck) for convolution in self.convolutions]
        if self.pool_branch is not None:
            branches.append(self.pool_branch(x))
        return torch.cat(branches, dim=1)


class ResidualShortcut2D(nn.Module):
    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        if in_channels == out_channels:
            self.net = nn.Identity()
        else:
            self.net = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 1, bias=False),
                nn.BatchNorm2d(out_channels),
            )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class SwipeInceptionTimeDW(nn.Module):
    """InceptionTime variant implemented with height-one 2D operators."""

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        filters: int = 64,
        depth: int = 6,
        kernel_sizes: tuple[int, ...] | list[int] = (39, 19, 9),
        bottleneck_channels: int = 32,
        pool_filters: int | None = None,
        residual_every: int = 3,
        temporal_pool_after: tuple[int, ...] | list[int] = (),
        feature_mode: str = "raw_centered_diff",
        pooling: str = "avg_max",
        input_dropout1d: float = 0.1,
        head_dropout: float = 0.2,
        tokenizer_path: str | None = None,
    ):
        super().__init__()
        if depth <= 0 or residual_every <= 0:
            raise ValueError("depth and residual_every must be positive")
        if filters <= 0:
            raise ValueError("filters must be positive")
        if bottleneck_channels < 0:
            raise ValueError("bottleneck_channels must be non-negative")
        if pool_filters is None:
            pool_filters = int(filters)
        if pool_filters < 0:
            raise ValueError("pool_filters must be non-negative")
        if feature_mode not in ("raw", "enc"):
            raise ValueError(f"Unsupported feature_mode {feature_mode!r}")

        kernel_sizes = tuple(int(value) for value in kernel_sizes)
        if not kernel_sizes:
            raise ValueError("kernel_sizes must not be empty")
        temporal_pool_after = tuple(int(value) for value in temporal_pool_after)
        if len(set(temporal_pool_after)) != len(temporal_pool_after):
            raise ValueError("temporal_pool_after must not contain duplicate block indices")
        if any(value <= 0 or value >= int(depth) for value in temporal_pool_after):
            raise ValueError(
                f"temporal_pool_after values must be between 1 and depth - 1, got {temporal_pool_after}"
            )

        self.feature_mode = feature_mode
        self.pooling = pooling
        self.tokenizer = Tokenizer(input_dim, input_dim) if feature_mode == "enc" else None
        self.temporal_pool_after = temporal_pool_after
        self.temporal_pool = nn.MaxPool2d((1, 2), stride=(1, 2))
        feature_dim = {
            "raw": int(input_dim),
            "enc": int(input_dim),
        }[feature_mode]
        output_channels = int(filters) * len(kernel_sizes) + int(pool_filters)
        self.input_dropout = nn.Dropout2d(float(input_dropout1d))
        self.modules_list = nn.ModuleList()
        self.shortcuts = nn.ModuleDict()
        channels = feature_dim
        residual_channels = channels
        for index in range(int(depth)):
            self.modules_list.append(
                DepthwiseInceptionModule2D(
                    channels,
                    filters=int(filters),
                    kernel_sizes=kernel_sizes,
                    bottleneck_channels=int(bottleneck_channels),
                    pool_filters=int(pool_filters),
                )
            )
            channels = output_channels
            if (index + 1) % int(residual_every) == 0:
                self.shortcuts[str(index)] = ResidualShortcut2D(residual_channels, channels)
                residual_channels = channels

        pooled_dim = channels
        self.pool = nn.AdaptiveMaxPool2d((1, 1))
        self.head = nn.Sequential(
            nn.Dropout(float(head_dropout)),
            nn.Linear(pooled_dim, output_dim),
        )

    def _features(self, x: torch.Tensor) -> torch.Tensor:
        if self.feature_mode == "raw":
            return x
        if self.feature_mode == "enc":
            return self.tokenizer(x)

    def forward_features(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, T, C]
        x = self._features(x)
        x = x.transpose(1, 2).unsqueeze(2)  # [B, C, 1, T]
        x = self.input_dropout(x)
        residual = x
        for index, module in enumerate(self.modules_list):
            x = module(x)
            key = str(index)
            if key in self.shortcuts:
                x = torch.relu(x + self.shortcuts[key](residual))
                residual = x
            if index + 1 in self.temporal_pool_after:
                x = self.temporal_pool(x)
                residual = self.temporal_pool(residual)
        return self.pool(x).flatten(1)  # [B, C]

    def forward_with_features(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        features = self.forward_features(x)
        return self.head(features), features

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.forward_features(x))


# Shorter alias for configuration files and interactive use.
SwipeInceptionDW = SwipeInceptionTimeDW
