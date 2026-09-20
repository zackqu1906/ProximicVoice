"""Quantization-friendly lite Inception classifier with one event-density head."""

from __future__ import annotations

import torch
import torch.nn as nn


DEFAULT_WINDOW_SIZE = 60


class Tokenizer(nn.Module):
    """Apply a learned channel transform independently at every time step."""

    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.transform = nn.Sequential(
            nn.Linear(in_channels, out_channels),
            nn.SiLU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [B, T, C]
        return self.transform(x)


class DepthwiseSeparableConv2D(nn.Module):
    """Height-one depthwise convolution followed by pointwise channel mixing."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int,
        pw_groups: int = 1,
    ):
        super().__init__()
        if pw_groups <= 0:
            raise ValueError("pw_groups must be positive")
        if in_channels % pw_groups != 0 or out_channels % pw_groups != 0:
            raise ValueError(
                "pw_groups must divide both pointwise input and output channels, "
                f"got in_channels={in_channels}, out_channels={out_channels}, "
                f"pw_groups={pw_groups}"
            )
        self.depthwise = nn.Conv2d(
            in_channels,
            in_channels,
            kernel_size=(1, kernel_size),
            padding=(0, kernel_size // 2),
            groups=in_channels,
            bias=False,
        )
        self.pointwise = nn.Conv2d(
            in_channels,
            out_channels,
            1,
            groups=pw_groups,
            bias=False,
        )

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
        pw_after_concat: bool = True,
        pw_width: int | None = None,
        pw_groups: int = 1,
        pool_after_bottleneck: bool = True,
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

        self.pw_after_concat = bool(pw_after_concat)
        if pw_width is None:
            pw_width = int(filters)
        if pw_width <= 0:
            raise ValueError("pw_width must be positive")
        self.pw_width = int(pw_width)
        if pw_groups <= 0:
            raise ValueError("pw_groups must be positive")
        self.pw_groups = int(pw_groups)
        self.pool_after_bottleneck = bool(pool_after_bottleneck)

        if self.pw_after_concat:
            # 每个分支只做 DW，拼接后共用一次 PW。相比每个分支分别做 PW，
            # 该布局显著减少通道混合开销，也是 lite 模型的核心结构。
            self.convolutions = nn.ModuleList(
                [
                    nn.Conv2d(
                        branch_input,
                        branch_input,
                        kernel_size=(1, kernel),
                        padding=(0, kernel // 2),
                        groups=branch_input,
                        bias=False,
                    )
                    for kernel in kernel_sizes
                ]
            )
            pointwise_input_channels = branch_input * len(kernel_sizes)
            if (
                pointwise_input_channels % self.pw_groups != 0
                or self.pw_width % self.pw_groups != 0
            ):
                raise ValueError(
                    "pw_groups must divide both shared pointwise input and output "
                    f"channels, got in_channels={pointwise_input_channels}, "
                    f"out_channels={self.pw_width}, pw_groups={self.pw_groups}"
                )
            self.shared_pointwise = nn.Sequential(
                nn.Conv2d(
                    pointwise_input_channels,
                    self.pw_width,
                    1,
                    groups=self.pw_groups,
                    bias=False,
                ),
                nn.BatchNorm2d(self.pw_width),
                nn.ReLU(),
            )
        else:
            self.convolutions = nn.ModuleList(
                [
                    nn.Sequential(
                        DepthwiseSeparableConv2D(
                            branch_input,
                            filters,
                            kernel,
                            pw_groups=self.pw_groups,
                        ),
                        nn.BatchNorm2d(filters),
                        nn.ReLU(),
                    )
                    for kernel in kernel_sizes
                ]
            )
            self.shared_pointwise = None

        if pool_filters > 0:
            pool_input_channels = branch_input if self.pool_after_bottleneck else in_channels
            self.pool_branch = nn.Sequential(
                nn.MaxPool2d((1, 3), stride=(1, 1), padding=(0, 1)),
                nn.Conv2d(pool_input_channels, pool_filters, 1, bias=False),
                nn.BatchNorm2d(pool_filters),
                nn.ReLU(),
            )
        else:
            self.pool_branch = None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        bottleneck = self.bottleneck(x)
        branches = [convolution(bottleneck) for convolution in self.convolutions]
        if self.shared_pointwise is not None:
            branches = [self.shared_pointwise(torch.cat(branches, dim=1))]
        if self.pool_branch is not None:
            pool_input = bottleneck if self.pool_after_bottleneck else x
            branches.append(self.pool_branch(pool_input))
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


class EventDensityHead2D(nn.Module):
    """Predict raw event-density logits with a dense temporal convolution."""

    def __init__(
        self,
        input_channels: int,
        hidden_channels: int,
        kernel_size: int = 3,
    ) -> None:
        super().__init__()
        if kernel_size <= 0 or kernel_size % 2 == 0:
            raise ValueError("Density-head kernel_size must be a positive odd integer")
        self.net = nn.Sequential(
            nn.Conv2d(
                input_channels,
                hidden_channels,
                kernel_size=(1, kernel_size),
                padding=(0, kernel_size // 2),
                bias=False,
            ),
            nn.BatchNorm2d(hidden_channels),
            nn.ReLU(),
            nn.Conv2d(hidden_channels, 1, kernel_size=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Softplus belongs to the caller-side post-processing boundary so the
        # deployable model graph does not require EXP/LOG.
        return self.net(x).flatten(1)


class EventDWHead2D(nn.Module):
    """Predict raw event-density logits with a depthwise-separable convolution."""

    def __init__(
        self,
        input_channels: int,
        hidden_channels: int,
        kernel_size: int = 3,
    ) -> None:
        super().__init__()
        if kernel_size <= 0 or kernel_size % 2 == 0:
            raise ValueError("Density-head kernel_size must be a positive odd integer")
        self.net = nn.Sequential(
            nn.Conv2d(
                input_channels,
                input_channels,
                kernel_size=(1, kernel_size),
                padding=(0, kernel_size // 2),
                groups=input_channels,
                bias=False,
            ),
            nn.Conv2d(input_channels, hidden_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(hidden_channels),
            nn.ReLU(),
            nn.Conv2d(hidden_channels, 1, kernel_size=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).flatten(1)


class EventMultiScaleDWHead2D(nn.Module):
    """Fuse two lightweight temporal receptive fields for density prediction."""

    def __init__(
        self,
        input_channels: int,
        hidden_channels: int,
        kernel_size: int = 3,
    ) -> None:
        super().__init__()
        if kernel_size <= 0 or kernel_size % 2 == 0:
            raise ValueError("Density-head kernel_size must be a positive odd integer")
        wide_kernel_size = 2 * kernel_size + 1
        self.branches = nn.ModuleList(
            nn.Conv2d(
                input_channels,
                input_channels,
                kernel_size=(1, branch_kernel_size),
                padding=(0, branch_kernel_size // 2),
                groups=input_channels,
                bias=False,
            )
            for branch_kernel_size in (kernel_size, wide_kernel_size)
        )
        self.fuse = nn.Sequential(
            nn.Conv2d(2 * input_channels, hidden_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(hidden_channels),
            nn.ReLU(),
            nn.Conv2d(hidden_channels, 1, kernel_size=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        features = torch.cat([branch(x) for branch in self.branches], dim=1)
        return self.fuse(features).flatten(1)


class SwipeInceptionTimeDWLiteEventAware(nn.Module):
    """Lite height-one Inception classifier with one temporal density head."""

    EVENT_DENSITY_ATTACHMENTS = (
        "after_block2",
        "after_pool2",
        "after_block3",
        "after_block4",
    )
    EVENT_HEAD_TYPES = ("standard", "dw", "multi_scale_dw")

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        window_size: int = DEFAULT_WINDOW_SIZE,
        filters: int = 64,
        depth: int = 6,
        kernel_sizes: tuple[int, ...] | list[int] = (39, 19, 9),
        bottleneck_channels: int = 32,
        pool_filters: int | None = None,
        residual_every: int = 3,
        temporal_pool_after: tuple[int, ...] | list[int] = (),
        feature_mode: str = "raw",
        pooling: str = "max",
        input_dropout1d: float = 0.1,
        head_dropout: float = 0.2,
        classification_pool_bins: int = 1,
        pw_after_concat: bool = True,
        pw_width: int | None = None,
        pw_groups: int = 1,
        pool_after_bottleneck: bool = True,
        first_residual: bool = False,
        density_head_channels: int = 32,
        density_head_kernel_size: int = 3,
        event_density_attachment: str = "after_pool2",
        event_head_type: str = "standard",
        temporal_class_head: bool = False,
        temporal_class_residual: bool = False,
        temporal_class_kernel_size: int = 1,
        gyro_norm: bool = False,
        event_log_gyro_rms: bool = False,
    ):
        super().__init__()
        if gyro_norm:
            raise ValueError(
                "gyro_norm is an input pre-processing operation; configure it on "
                "SwipeCNNClassificationEventAwareTask instead of model_kwargs"
            )
        if event_log_gyro_rms:
            raise ValueError(
                "event_log_gyro_rms is not supported by the pure deployment model"
            )
        if depth < 4 or residual_every <= 0:
            raise ValueError("depth must be at least 4 and residual_every must be positive")
        if window_size <= 0 or window_size % 4 != 0:
            raise ValueError("window_size must be positive and divisible by four")
        if filters <= 0:
            raise ValueError("filters must be positive")
        if bottleneck_channels < 0:
            raise ValueError("bottleneck_channels must be non-negative")
        if pool_filters is None:
            pool_filters = int(filters)
        if pool_filters < 0:
            raise ValueError("pool_filters must be non-negative")
        if density_head_channels <= 0:
            raise ValueError("density_head_channels must be positive")
        if classification_pool_bins <= 0:
            raise ValueError("classification_pool_bins must be positive")
        if density_head_kernel_size <= 0 or density_head_kernel_size % 2 == 0:
            raise ValueError(
                "density_head_kernel_size must be a positive odd integer"
            )
        if temporal_class_kernel_size <= 0 or temporal_class_kernel_size % 2 == 0:
            raise ValueError(
                "temporal_class_kernel_size must be a positive odd integer"
            )
        if event_density_attachment not in self.EVENT_DENSITY_ATTACHMENTS:
            raise ValueError(
                "event_density_attachment must be one of "
                f"{self.EVENT_DENSITY_ATTACHMENTS}, got {event_density_attachment!r}"
            )
        if event_head_type not in self.EVENT_HEAD_TYPES:
            raise ValueError(
                "event_head_type must be one of "
                f"{self.EVENT_HEAD_TYPES}, got {event_head_type!r}"
            )
        if feature_mode not in ("raw", "enc", "enc_open"):
            raise ValueError(f"Unsupported feature_mode {feature_mode!r}")
        if pooling != "max":
            raise ValueError("The lite model uses fixed max pooling; set pooling='max'")

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
        if 2 not in temporal_pool_after or any(block < 2 for block in temporal_pool_after):
            raise ValueError(
                "The 30-bin density attachments require the first temporal pool after block 2, "
                f"got temporal_pool_after={temporal_pool_after}"
            )
        if 3 in temporal_pool_after:
            raise ValueError(
                "Block 3 must retain the 30-bin temporal resolution, "
                f"got temporal_pool_after={temporal_pool_after}"
            )

        self.feature_mode = feature_mode
        self.pooling = pooling
        self.residual_every = int(residual_every)
        self.pw_after_concat = bool(pw_after_concat)
        if pw_width is None:
            pw_width = int(filters)
        if pw_width <= 0:
            raise ValueError("pw_width must be positive")
        self.pw_width = int(pw_width)
        if pw_groups <= 0:
            raise ValueError("pw_groups must be positive")
        self.pw_groups = int(pw_groups)
        self.pool_after_bottleneck = bool(pool_after_bottleneck)
        self.first_residual = bool(first_residual)
        self.tokenizer = (
            Tokenizer(input_dim, input_dim)
            if feature_mode in ("enc", "enc_open")
            else None
        )
        self.temporal_pool_after = temporal_pool_after
        self.temporal_pool = nn.MaxPool2d((1, 2), stride=(1, 2))
        self.event_density_attachment = event_density_attachment
        self.event_head_type = event_head_type
        self.temporal_class_head_enabled = bool(temporal_class_head)
        self.temporal_class_residual = bool(temporal_class_residual)
        if self.temporal_class_residual and not self.temporal_class_head_enabled:
            raise ValueError(
                "temporal_class_residual requires temporal_class_head=true"
            )

        feature_dim = int(input_dim)
        convolution_channels = (
            self.pw_width
            if self.pw_after_concat
            else int(filters) * len(kernel_sizes)
        )
        output_channels = convolution_channels + int(pool_filters)
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
                    pw_after_concat=self.pw_after_concat,
                    pw_width=self.pw_width,
                    pw_groups=self.pw_groups,
                    pool_after_bottleneck=self.pool_after_bottleneck,
                )
            )
            channels = output_channels
            if self.first_residual and (index + 1) % self.residual_every == 0:
                self.shortcuts[str(index)] = ResidualShortcut2D(
                    residual_channels,
                    channels,
                )
                residual_channels = channels

        # density_60/density_30 是监督层级名称；实际 bin 数随输入窗口变化。
        # after_block2 位于第一次时序池化前，其余 attachment 使用半分辨率层级。
        self.window_size = int(window_size)
        pooled_length = self.window_size
        for _ in temporal_pool_after:
            pooled_length //= 2
        if pooled_length % int(classification_pool_bins) != 0:
            raise ValueError(
                "classification_pool_bins must divide the final temporal length, "
                f"got bins={classification_pool_bins}, length={pooled_length}"
            )
        self.classification_pool_bins = int(classification_pool_bins)
        classification_bin_width = pooled_length // self.classification_pool_bins
        self.pool = nn.MaxPool2d(
            kernel_size=(1, classification_bin_width),
            stride=(1, classification_bin_width),
        )
        self.head = nn.Sequential(
            nn.Dropout(float(head_dropout)),
            nn.Linear(channels * self.classification_pool_bins, output_dim),
        )
        self.event_density_target_length = (
            self.window_size
            if self.event_density_attachment == "after_block2"
            else self.window_size // 2
        )
        self.event_density_target_key = (
            "density_60"
            if self.event_density_attachment == "after_block2"
            else "density_30"
        )
        event_head_class = {
            "standard": EventDensityHead2D,
            "dw": EventDWHead2D,
            "multi_scale_dw": EventMultiScaleDWHead2D,
        }[self.event_head_type]
        self.event_head = event_head_class(
            output_channels,
            int(density_head_channels),
            int(density_head_kernel_size),
        )
        self.temporal_class_head = (
            nn.Conv2d(
                output_channels,
                output_dim,
                kernel_size=(1, int(temporal_class_kernel_size)),
                padding=(0, int(temporal_class_kernel_size) // 2),
            )
            if self.temporal_class_head_enabled
            else None
        )

    def _features(self, x: torch.Tensor) -> torch.Tensor:
        if self.feature_mode == "raw":
            return x
        return self.tokenizer(x)

    def forward_features(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Return classification features and the selected temporal density map."""
        # [B, T, C] -> [B, C, 1, T]。contiguous() 是 lite 实现针对部分 CUDA
        # pooling kernel 的必要处理，避免 height-one strided view 地址未对齐。
        x = self._features(x).transpose(1, 2).unsqueeze(2).contiguous()
        x = self.input_dropout(x)
        residual = x
        density_features = None

        for index, module in enumerate(self.modules_list):
            x = module(x)
            block_number = index + 1
            if block_number % self.residual_every == 0:
                key = str(index)
                if self.first_residual:
                    x = torch.relu(x + self.shortcuts[key](residual))
                elif block_number > self.residual_every:
                    x = torch.relu(x + residual)
                residual = x

            # attachment 总是在该 block 后、潜在时序池化前捕获。
            if self.event_density_attachment == f"after_block{block_number}":
                density_features = x
            if block_number in self.temporal_pool_after:
                x = self.temporal_pool(x)
                residual = self.temporal_pool(residual)
                if (
                    block_number == 2
                    and self.event_density_attachment == "after_pool2"
                ):
                    density_features = x

        assert density_features is not None
        classification_features = self.pool(x).flatten(1)
        return classification_features, density_features

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        classification_features, density_features = self.forward_features(x)
        class_logits = self.head(classification_features)
        density_logits = self.event_head(density_features)
        outputs = {"class_logits": class_logits}
        if self.temporal_class_head is not None:
            event_class_logits = self.temporal_class_head(
                density_features
            ).squeeze(2)  # [B, classes, 30]
            if self.temporal_class_residual:
                event_class_logits = event_class_logits + class_logits.unsqueeze(-1)
            outputs["event_class_logits"] = event_class_logits
        outputs["event_density_logits"] = density_logits  # [B, 30]
        return outputs


SwipeInceptionDWLiteEventAware = SwipeInceptionTimeDWLiteEventAware

# Auto-generated by snapshot_model_source; keep this at the end of the snapshot.
def build_model(state_dict):
    """Construct the snapshotted ``SwipeInceptionTimeDWLiteEventAware`` and load ``state_dict``."""
    model = SwipeInceptionTimeDWLiteEventAware(
        filters=64,
        depth=6,
        kernel_sizes=[15, 7, 3],
        bottleneck_channels=32,
        pool_filters=16,
        pw_after_concat=True,
        pw_width=64,
        pw_groups=1,
        pool_after_bottleneck=False,
        first_residual=False,
        residual_every=2,
        temporal_pool_after=[2, 4],
        feature_mode='raw',
        pooling='max',
        input_dropout1d=0.1,
        head_dropout=0.2,
        classification_pool_bins=1,
        event_density_attachment='after_block4',
        density_head_channels=96,
        density_head_kernel_size=3,
        event_head_type='dw',
        temporal_class_head=False,
        temporal_class_residual=False,
        temporal_class_kernel_size=1,
        input_dim=6,
        output_dim=12,
    )
    model.load_state_dict(state_dict, strict=True)
    return model
