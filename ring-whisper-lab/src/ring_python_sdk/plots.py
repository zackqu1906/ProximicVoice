"""Optional matplotlib plot backends (registered by the host app)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_ImuPlotFactory: Callable[..., Any] | None = None
_AudioPlotFactory: Callable[..., Any] | None = None


def register_plot_factories(
    *,
    imu: Callable[..., Any] | None = None,
    audio: Callable[..., Any] | None = None,
) -> None:
    """Register constructors for IMU / audio live plots (receiver TUI)."""
    global _ImuPlotFactory, _AudioPlotFactory
    if imu is not None:
        _ImuPlotFactory = imu
    if audio is not None:
        _AudioPlotFactory = audio


def create_imu_plot(**kwargs: Any) -> Any:
    if _ImuPlotFactory is None:
        raise RuntimeError(
            "IMU live plot backend not registered; "
            "call ring_python_sdk.plots.register_plot_factories(...) "
            "or install receiver plot helpers"
        )
    return _ImuPlotFactory(**kwargs)


def create_audio_plot(**kwargs: Any) -> Any:
    if _AudioPlotFactory is None:
        raise RuntimeError(
            "Audio live plot backend not registered; "
            "call ring_python_sdk.plots.register_plot_factories(...) "
            "or install receiver plot helpers"
        )
    return _AudioPlotFactory(**kwargs)
